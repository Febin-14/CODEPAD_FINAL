from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.models.db import connect, get_db, mongodb
from app.routes import auth
from app.routes.auth import migrate_usernames
from app.services.workload import compute_project_progress, enrich_developers_with_workload
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

# Import WebSocket endpoints
from app.routes import websocket_router

app = FastAPI()

# Mount static files
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

# Set up templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

app.include_router(auth.router)
app.include_router(websocket_router.router)


def _approved_developer_query(extra_filter: dict | None = None) -> dict:
    clauses = [
        {"role": {"$ne": "manager"}},
        {"$or": [{"approval_status": "approved"}, {"approval_status": {"$exists": False}}]},
    ]
    if extra_filter:
        clauses.append(extra_filter)
    return {"$and": clauses}


def _pending_developer_query() -> dict:
    return {"$and": [{"role": {"$ne": "manager"}}, {"approval_status": "pending"}]}


async def _get_current_user_profile(db, username: str, fallback_role: str = ""):
    user = await db.users.find_one({"username": username}) or {}
    role = user.get("role") or fallback_role
    approval_status = (user.get("approval_status") or "approved").strip().lower()
    if role == "manager":
        projects_completed = await db.projects.count_documents({"status": "completed"})
        projects_active = await db.projects.count_documents({"status": {"$ne": "completed"}})
        tasks_done = await db.tasks.count_documents({"status": "done"})
    else:
        projects_completed = await db.projects.count_documents({
            "assigned_developers": username,
            "status": "completed",
        })
        projects_active = await db.projects.count_documents({
            "assigned_developers": username,
            "status": {"$ne": "completed"},
        })
        tasks_done = await db.tasks.count_documents({
            "assigned_to": username,
            "status": "done",
        })
    return {
        "username": username,
        "role": role,
        "description": user.get("description") or "No description available yet.",
        "photo_url": user.get("photo_url") or "",
        "projects_completed": projects_completed,
        "projects_active": projects_active,
        "tasks_done": tasks_done,
        "approval_status": approval_status,
    }

@app.on_event("startup")
async def startup_db_client():
    connect()
    db = get_db()
    # Migrate legacy dev1/dev2 usernames first
    await migrate_usernames(db)
    # Seed users if empty
    if await db.users.count_documents({}) == 0:
        from app.routes.auth import seed_users
        await db.users.insert_many(seed_users)

@app.on_event("shutdown")
async def shutdown_db_client():
    mongodb.client.close()

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    username = request.cookies.get("username")
    role = request.cookies.get("role")
    if not username or not role:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Please log in."})
    
    db = get_db()
    user_profile = await _get_current_user_profile(db, username, role)
    if role != "manager" and user_profile.get("approval_status") != "approved":
        response = RedirectResponse(url="/?error=pending_approval", status_code=302)
        response.delete_cookie("username")
        response.delete_cookie("role")
        return response
    tasks_cursor = db.tasks.find({})
    tasks = await tasks_cursor.to_list(length=1000)

    projects_cursor = db.projects.find({})
    projects = await projects_cursor.to_list(length=500)

    tasks_by_project: dict[str, list[dict]] = {}
    for task in tasks:
        project_id = task.get("project_id")
        if not project_id:
            continue
        tasks_by_project.setdefault(project_id, []).append(task)
    
    # Link tasks to their projects for easy display
    project_map = {str(p["_id"]): p for p in projects}
    for task in tasks:
        p_id = task.get("project_id")
        if p_id and p_id in project_map:
            task["project"] = project_map[p_id]
        else:
            task["project"] = None
    
    if role == "manager":
        github_repo = (os.getenv("GITHUB_REPO") or "").strip()
        # Fetch all developers to assign to projects
        developers_cursor = db.users.find(_approved_developer_query())
        developers = await developers_cursor.to_list(length=100)
        developers = await enrich_developers_with_workload(db, developers)
        developer_lookup = {developer["username"]: developer for developer in developers}
        pending_cursor = db.users.find(_pending_developer_query()).sort("_id", -1)
        pending_developers = await pending_cursor.to_list(length=50)
        enriched_projects: list[dict] = []
        for project in projects:
            project_copy = dict(project)
            project_progress = compute_project_progress(project_copy, tasks_by_project.get(str(project_copy["_id"]), []))
            project_copy.update(project_progress)
            project_copy["assigned_developer_details"] = [
                developer_lookup.get(
                    username,
                    {
                        "username": username,
                        "availability_label": "Unknown",
                        "availability_detail": "Developer details unavailable",
                        "is_free": False,
                    },
                )
                for username in project_copy.get("assigned_developers", [])
            ]
            enriched_projects.append(project_copy)
        # Show all projects (including completed) in the overview
        return templates.TemplateResponse(
            "manager_dashboard.html",
            {
                "request": request,
                "username": username,
                "tasks": tasks,
                "projects": enriched_projects,
                "developers": developers,
                "pending_developers": pending_developers,
                "github_repo": github_repo,
                "user_profile": user_profile,
            }
        )
    else:
        # Get tasks assigned to this developer
        my_tasks = [task for task in tasks if task.get("assigned_to") == username]
        
        # Get projects this developer is assigned to
        my_projects = [p for p in projects if username in p.get("assigned_developers", [])]
        
        return templates.TemplateResponse(
            "developer_dashboard.html",
            {"request": request, "username": username, "tasks": my_tasks, "projects": my_projects, "user_profile": user_profile}
        )


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    """Manager view: details for a single project, with tasks and new-task form."""
    username = request.cookies.get("username")
    role = request.cookies.get("role")
    if not username or not role:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Please log in."})
    if role != "manager":
        return RedirectResponse(url="/dashboard", status_code=302)

    db = get_db()
    from bson import ObjectId  # local import to avoid circular at module import time in some contexts
    user_profile = await _get_current_user_profile(db, username, role)

    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        return RedirectResponse(url="/dashboard", status_code=302)

    tasks_cursor = db.tasks.find({"project_id": project_id})
    project_tasks = await tasks_cursor.to_list(length=200)
    project.update(compute_project_progress(project, project_tasks))

    github_repo = (os.getenv("GITHUB_REPO") or "").strip()
    # Developers for assignment dropdowns
    developers_cursor = db.users.find(_approved_developer_query())
    developers = await developers_cursor.to_list(length=100)
    developers = await enrich_developers_with_workload(db, developers)
    developer_lookup = {developer["username"]: developer for developer in developers}
    assigned_developer_details = [
        developer_lookup.get(
            username,
            {
                "username": username,
                "availability_label": "Unknown",
                "availability_detail": "Developer details unavailable",
                "is_free": False,
            },
        )
        for username in project.get("assigned_developers", [])
    ]

    return templates.TemplateResponse(
        "project_detail.html",
        {
            "request": request,
            "username": username,
            "project": project,
            "tasks": project_tasks,
            "developers": developers,
            "assigned_developer_details": assigned_developer_details,
            "github_repo": github_repo,
            "user_profile": user_profile,
        },
    )


@app.get("/developer_analytics", response_class=HTMLResponse)
async def developer_analytics_page(request: Request):
    """Developer analytics screen – manager only."""
    username = request.cookies.get("username")
    role = request.cookies.get("role")
    if not username or not role:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Please log in."})
    if role != "manager":
        return RedirectResponse(url="/dashboard", status_code=302)
    db = get_db()
    user_profile = await _get_current_user_profile(db, username, role)
    return templates.TemplateResponse(
        "developer_analytics.html",
        {"request": request, "username": username, "user_profile": user_profile}
    )
