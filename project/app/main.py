from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.models.db import connect, get_db, mongodb
from app.routes import auth
from app.routes.auth import migrate_usernames
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
    tasks_cursor = db.tasks.find({})
    tasks = await tasks_cursor.to_list(length=100)
    
    projects_cursor = db.projects.find({})
    projects = await projects_cursor.to_list(length=100)
    
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
        developers_cursor = db.users.find({"role": {"$ne": "manager"}})
        developers = await developers_cursor.to_list(length=100)
        # Show all projects (including completed) in the overview
        return templates.TemplateResponse(
            "manager_dashboard.html",
            {
                "request": request,
                "username": username,
                "tasks": tasks,
                "projects": projects,
                "developers": developers,
                "github_repo": github_repo,
            }
        )
    else:
        # Get tasks assigned to this developer
        my_tasks = [task for task in tasks if task.get("assigned_to") == username]
        
        # Get projects this developer is assigned to
        my_projects = [p for p in projects if username in p.get("assigned_developers", [])]
        
        return templates.TemplateResponse(
            "developer_dashboard.html",
            {"request": request, "username": username, "tasks": my_tasks, "projects": my_projects}
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

    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        return RedirectResponse(url="/dashboard", status_code=302)

    tasks_cursor = db.tasks.find({"project_id": project_id})
    project_tasks = await tasks_cursor.to_list(length=200)

    github_repo = (os.getenv("GITHUB_REPO") or "").strip()
    # Developers for assignment dropdowns
    developers_cursor = db.users.find({"role": {"$ne": "manager"}})
    developers = await developers_cursor.to_list(length=100)

    return templates.TemplateResponse(
        "project_detail.html",
        {
            "request": request,
            "username": username,
            "project": project,
            "tasks": project_tasks,
            "developers": developers,
            "github_repo": github_repo,
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
    return templates.TemplateResponse(
        "developer_analytics.html",
        {"request": request, "username": username}
    )

