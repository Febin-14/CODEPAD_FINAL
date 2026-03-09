from fastapi import APIRouter, Form, Request, status, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from app.models.schemas import User, Task, Project, CodeSubmission, TaskReview, DeveloperSession, RunPythonRequest
from app.models.db import get_db
from app.services.assignment import assign_task_to_developer
from bson import ObjectId
import os
import re
import base64
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import httpx
import subprocess
import tempfile
import sys

load_dotenv()

router = APIRouter()

# GitHub config (optional: if set, approved task code is pushed to repo instead of local archive)
# Support .env with or without spaces around =
GITHUB_REPO = (os.getenv("GITHUB_REPO") or os.getenv("GITHUB_REPO ") or "").strip()
GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN ") or "").strip()


def _slug_for_task(title: str, task_id: str) -> str:
    """Sanitize task title + short id into a safe directory name."""
    slug = re.sub(r"[^\w\s-]", "", (title or "Untitled").lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-") or "task"
    short_id = str(task_id)[-6:] if len(str(task_id)) >= 6 else str(task_id)
    return f"{slug}-{short_id}"


async def _push_task_to_github(task: dict) -> tuple[str | None, str | None]:
    """
    Push task code and metadata to GitHub repo as a new directory under 'tasks/<slug>/'.
    Returns (github_path, branch) on success, (None, None) on failure.
    """
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return None, None
    repo = GITHUB_REPO
    if "/" not in repo:
        return None, None
    owner, repo_name = repo.split("/", 1)
    task_id_str = str(task["_id"])
    task_title = task.get("title", "Untitled")
    file_ext = "html" if task.get("type") == "frontend" else "py"
    code = task.get("code") or ""
    slug = _slug_for_task(task_title, task_id_str)
    dir_path = f"tasks/{slug}"

    metadata_lines = [
        f"Task ID: {task_id_str}",
        f"Title: {task.get('title', 'N/A')}",
        f"Type: {task.get('type', 'N/A')}",
        f"Assigned To: {task.get('assigned_to', 'N/A')}",
        f"Completed At: {datetime.now().isoformat()}",
    ]
    if task.get("description"):
        metadata_lines.append(f"\nDescription:\n{task['description']}")
    metadata_content = "\n".join(metadata_lines)

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get default branch
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo_name}",
            headers=headers,
        )
        if r.status_code != 200:
            return None, None
        data = r.json()
        branch = data.get("default_branch") or "main"

        # Create code file
        code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
        code_path = f"{dir_path}/code.{file_ext}"
        r_code = await client.put(
            f"https://api.github.com/repos/{owner}/{repo_name}/contents/{code_path}",
            headers=headers,
            json={
                "message": f"Add task: {task_title}",
                "content": code_b64,
                "branch": branch,
            },
        )
        if r_code.status_code not in (200, 201):
            return None, None

        # Create metadata file
        meta_b64 = base64.b64encode(metadata_content.encode("utf-8")).decode("ascii")
        meta_path = f"{dir_path}/metadata.txt"
        r_meta = await client.put(
            f"https://api.github.com/repos/{owner}/{repo_name}/contents/{meta_path}",
            headers=headers,
            json={
                "message": f"Add metadata for task: {task_title}",
                "content": meta_b64,
                "branch": branch,
            },
        )
        if r_meta.status_code not in (200, 201):
            return None, None

    return dir_path, branch

# Seed data for MongoDB
seed_users = [
    {
        "username": "manager",
        "password": "manager123",
        "role": "manager",
        "description": "Project manager with experience in agile methodologies and team leadership."
    },
    {
        "username": "dev1",
        "password": "dev123",
        "role": "frontend developer",
        "description": "Frontend developer with expertise in HTML, CSS, JavaScript, and React."
    },
    {
        "username": "dev2",
        "password": "dev123",
        "role": "backend developer",
        "description": "Backend developer with expertise in Python, FastAPI, and MongoDB."
    },
]

@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...)):
    db = get_db()
    user = await db.users.find_one({"username": username})
    
    if user and user["password"] == password:
        # Validate that the selected role matches the user's actual role
        if role != user["role"]:
            return RedirectResponse(url="/?error=role_mismatch", status_code=status.HTTP_302_FOUND)

        # Track developer login: close any previous open session, then start new session
        is_developer = user["role"] != "manager"
        if is_developer:
            now = datetime.utcnow()
            # Close previous open session (e.g. user closed browser without logging out)
            open_sessions = await db.developer_sessions.find({"username": username, "logout_at": None}).to_list(length=100)
            for s in open_sessions:
                duration_minutes = (now - s["login_at"]).total_seconds() / 60
                await db.developer_sessions.update_one(
                    {"_id": s["_id"]},
                    {"$set": {"logout_at": now, "duration_minutes": round(duration_minutes, 2)}}
                )
            session = DeveloperSession(username=username, login_at=now)
            await db.developer_sessions.insert_one(session.dict())

        response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        response.set_cookie(key="username", value=username)
        response.set_cookie(key="role", value=user["role"] if user["role"] == "manager" else "developer")
        return response
        
    return RedirectResponse(url="/?error=invalid", status_code=status.HTTP_302_FOUND)

@router.post("/register")
async def register(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...), description: str = Form(...)):
    db = get_db()
    existing_user = await db.users.find_one({"username": username})
    
    if existing_user:
        return RedirectResponse(url="/register?error=exists", status_code=status.HTTP_302_FOUND)
        
    new_user = {
        "username": username,
        "password": password,
        "role": role,
        "description": description
    }
    
    await db.users.insert_one(new_user)
    return RedirectResponse(url="/?success=registered", status_code=status.HTTP_302_FOUND)

@router.post("/add_project")
async def add_project(request: Request, title: str = Form(...), description: str = Form(None)):
    db = get_db()
    
    # Extract assigned developers from the form data list (can be multiple)
    form_data = await request.form()
    assigned_developers = form_data.getlist("assigned_developers")
    
    project = Project(title=title, description=description, assigned_developers=assigned_developers)
    await db.projects.insert_one(project.dict())
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@router.post("/api/edit_project/{project_id}")
async def edit_project(project_id: str, request: Request, title: str = Form(...), description: str = Form(None)):
    db = get_db()
    form_data = await request.form()
    assigned_developers = form_data.getlist("assigned_developers")
    
    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {
            "title": title,
            "description": description,
            "assigned_developers": assigned_developers
        }}
    )
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@router.post("/api/delete_project/{project_id}")
async def delete_project(project_id: str):
    db = get_db()
    # Delete the project
    await db.projects.delete_one({"_id": ObjectId(project_id)})
    # Delete all tasks associated with this project
    await db.tasks.delete_many(
        {"project_id": project_id}
    )
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@router.post("/api/projects/{project_id}/complete")
async def complete_project(project_id: str):
    """Mark a project as completed."""
    db = get_db()
    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"status": "completed"}}
    )
    return RedirectResponse(url=f"/projects/{project_id}", status_code=status.HTTP_302_FOUND)

@router.post("/add_task")
async def add_task(request: Request, title: str = Form(...), description: str = Form(None), project_id: str = Form(...), priority: str = Form("medium")):
    db = get_db()

    # Get the project to find the assigned developers
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    assigned_devs = project.get("assigned_developers", [])

    # Get all developers assigned to the project with role and description
    if assigned_devs:
        developers_cursor = db.users.find({"username": {"$in": assigned_devs}, "role": {"$ne": "manager"}})
        developers = await developers_cursor.to_list(length=100)
        developers = [
            {"username": u["username"], "role": u.get("role", ""), "description": u.get("description", "")}
            for u in developers
        ]
    else:
        developers = []

    assigned_to, task_type = assign_task_to_developer(title, description, developers)

    task = Task(title=title, type=task_type, project_id=project_id, assigned_to=assigned_to, description=description, priority=priority)
    await db.tasks.insert_one(task.dict())

    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@router.post("/api/edit_task/{task_id}")
async def edit_task(task_id: str, title: str = Form(...), description: str = Form(None), project_id: str = Form(...), priority: str = Form("medium")):
    db = get_db()
    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {
            "title": title,
            "description": description,
            "project_id": project_id,
            "priority": priority
        }}
    )
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@router.get("/my_tasks")
async def my_tasks(request: Request):
    username = request.cookies.get("username")
    db = get_db()
    tasks_cursor = db.tasks.find({"assigned_to": username})
    my_tasks = await tasks_cursor.to_list(length=100)
    
    # Convert ObjectId to string for JSON response
    for task in my_tasks:
        task["_id"] = str(task["_id"])
        
    return {"tasks": my_tasks}

@router.post("/api/start_task/{task_id}")
async def start_task(task_id: str):
    db = get_db()
    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {"status": "in_progress"}}
    )
    return {"success": True}

@router.post("/submit_code")
async def submit_code(request: Request, submission: CodeSubmission):
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    db = get_db()
    result = await db.tasks.update_one(
        {"_id": ObjectId(submission.task_id), "assigned_to": username},
        {"$set": {"code": submission.code, "status": "submitted_for_review", "comments": None}}
    )
    
    if result.modified_count == 1:
        print(f"--- Code Submitted for Task ID: {submission.task_id} ---\n{submission.code}\n---------------------------------------------")
        return JSONResponse(content={"success": True, "message": "Code submitted successfully for review"})
    
    raise HTTPException(status_code=404, detail="Task not found or not assigned to you")

@router.get("/get_task_code/{task_id}")
async def get_task_code(request: Request, task_id: str):
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if task:
        return {"code": task.get("code", "")}
    return {"code": ""}


# Timeout (seconds) for running user Python code
RUN_PYTHON_TIMEOUT = 10


@router.post("/api/run_python")
async def run_python(request: Request, body: RunPythonRequest):
    """Run Python code in a subprocess and return stdout, stderr, and exit code. For backend developers only."""
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    code = (body.code or "").strip()
    if not code:
        return JSONResponse(content={"stdout": "", "stderr": "", "exit_code": 0, "timeout": False})
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name
        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                text=True,
                timeout=RUN_PYTHON_TIMEOUT,
                cwd=None,
                env={**os.environ},
            )
            return JSONResponse(content={
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "exit_code": result.returncode,
                "timeout": False,
            })
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    except subprocess.TimeoutExpired:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return JSONResponse(content={
            "stdout": "",
            "stderr": f"Execution timed out after {RUN_PYTHON_TIMEOUT} seconds.",
            "exit_code": -1,
            "timeout": True,
        })
    except Exception as e:
        return JSONResponse(content={
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "timeout": False,
        })

@router.get("/logout")
async def logout(request: Request):
    username = request.cookies.get("username")
    role = request.cookies.get("role")
    # Record developer logout time before clearing cookies
    if username and role == "developer":
        db = get_db()
        now = datetime.utcnow()
        open_session = await db.developer_sessions.find_one(
            {"username": username, "logout_at": None},
            sort=[("login_at", -1)]
        )
        if open_session:
            login_at = open_session["login_at"]
            duration_minutes = (now - login_at).total_seconds() / 60
            await db.developer_sessions.update_one(
                {"_id": open_session["_id"]},
                {"$set": {"logout_at": now, "duration_minutes": round(duration_minutes, 2)}}
            )
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("username")
    response.delete_cookie("role")
    return response

@router.post("/delete_task/{task_id}")
async def delete_task(task_id: str):
    db = get_db()
    await db.tasks.delete_one({"_id": ObjectId(task_id)})
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@router.post("/api/approve_task")
async def approve_task(request: Request, review: TaskReview):
    db = get_db()
    # Get task details before updating
    task = await db.tasks.find_one({"_id": ObjectId(review.task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # Push code to GitHub when marking as done (if configured)
    update_data = {"status": "done"}
    if task.get("code") and (GITHUB_REPO and GITHUB_TOKEN):
        github_path, github_branch = await _push_task_to_github(task)
        if github_path:
            update_data["github_path"] = github_path
            update_data["github_branch"] = github_branch
        # If push failed, still mark done but without github_path
    elif task.get("code") and not (GITHUB_REPO and GITHUB_TOKEN):
        # No GitHub config: still mark done; no archive (replaced by GitHub)
        pass

    result = await db.tasks.update_one(
        {"_id": ObjectId(review.task_id)},
        {"$set": update_data}
    )

    if result.modified_count == 1:
        msg = "Task approved and marked as done"
        if update_data.get("github_path"):
            msg += ". Code pushed to GitHub."
        return JSONResponse(content={"success": True, "message": msg})
    
    raise HTTPException(status_code=404, detail="Task not found")

@router.post("/api/reject_task")
async def reject_task(request: Request, review: TaskReview):
    if not review.comments or not review.comments.strip():
        raise HTTPException(status_code=400, detail="Comments are required when rejecting a task")
    
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(review.task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # Reassign to the same developer with comments and set status to in_progress
    result = await db.tasks.update_one(
        {"_id": ObjectId(review.task_id)},
        {"$set": {"status": "in_progress", "comments": review.comments}}
    )
    
    if result.modified_count == 1:
        return JSONResponse(content={"success": True, "message": "Task rejected and reassigned with comments"})
    
    raise HTTPException(status_code=404, detail="Task not found")

@router.post("/api/pause_task/{task_id}")
async def pause_task(request: Request, task_id: str):
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id), "assigned_to": username})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")
    
    # Only allow pausing if task is in_progress
    if task.get("status") != "in_progress":
        raise HTTPException(status_code=400, detail="Only in-progress tasks can be paused")
    
    # Get code from request body if provided (for saving current code)
    try:
        body = await request.json()
        code = body.get("code", None)
    except:
        code = None
    
    # Save code if provided, otherwise keep existing code
    update_data = {"status": "paused"}
    if code is not None:
        update_data["code"] = code
    
    result = await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": update_data}
    )
    
    if result.modified_count == 1:
        return JSONResponse(content={"success": True, "message": "Task paused successfully"})
    
    raise HTTPException(status_code=404, detail="Task not found")

@router.post("/api/resume_task/{task_id}")
async def resume_task(request: Request, task_id: str):
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id), "assigned_to": username})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")
    
    # Only allow resuming if task is paused
    if task.get("status") != "paused":
        raise HTTPException(status_code=400, detail="Only paused tasks can be resumed")
    
    result = await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {"status": "in_progress"}}
    )
    
    if result.modified_count == 1:
        return JSONResponse(content={"success": True, "message": "Task resumed successfully"})
    
    raise HTTPException(status_code=404, detail="Task not found")

@router.get("/api/developer_analytics")
async def get_developer_analytics(request: Request):
    """Returns developer analytics for manager view. Manager-only."""
    role = request.cookies.get("role")
    if role != "manager":
        raise HTTPException(status_code=403, detail="Only managers can view developer analytics")
    db = get_db()
    developers_cursor = db.users.find({"role": {"$ne": "manager"}})
    developers = await developers_cursor.to_list(length=100)
    result = []
    for dev in developers:
        username = dev["username"]
        # Task counts for this developer
        tasks = await db.tasks.find({"assigned_to": username}).to_list(length=1000)
        tasks_done = sum(1 for t in tasks if t.get("status") == "done")
        tasks_pending = sum(1 for t in tasks if t.get("status") in ("new", "in_progress", "paused", "submitted_for_review"))
        tasks_in_progress = sum(1 for t in tasks if t.get("status") == "in_progress")
        tasks_submitted = sum(1 for t in tasks if t.get("status") == "submitted_for_review")
        # Sessions: total time and recent sessions
        # Total work time = sum of duration_minutes for all sessions. Each session's duration is
        # (logout_at - login_at) in minutes, set when the developer logs out (or when they
        # log in again if they didn't log out). Open sessions (no logout_at) contribute 0.
        sessions_cursor = db.developer_sessions.find({"username": username}).sort("login_at", -1)
        all_sessions = await sessions_cursor.to_list(length=500)
        total_minutes = sum(s.get("duration_minutes") or 0 for s in all_sessions)
        session_count = len([s for s in all_sessions if s.get("logout_at") is not None])
        recent_sessions = []
        for s in all_sessions[:20]:
            login_at = s.get("login_at")
            logout_at = s.get("logout_at")
            duration = s.get("duration_minutes")
            recent_sessions.append({
                "login_at": login_at.isoformat() if login_at else None,
                "logout_at": logout_at.isoformat() if logout_at else None,
                "duration_minutes": duration,
            })
        result.append({
            "username": username,
            "role": dev.get("role", ""),
            "description": dev.get("description", ""),
            "tasks_completed": tasks_done,
            "tasks_pending": tasks_pending,
            "tasks_in_progress": tasks_in_progress,
            "tasks_submitted_for_review": tasks_submitted,
            "total_sessions": session_count,
            "total_work_minutes": round(total_minutes, 2),
            "recent_sessions": recent_sessions,
        })
    return {"developers": result}


@router.get("/api/task_github_url/{task_id}")
async def task_github_url(task_id: str):
    """Return the GitHub URL for a completed task's code (when pushed to repo)."""
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("status") != "done":
        raise HTTPException(status_code=400, detail="Only completed tasks have code on GitHub")
    path = task.get("github_path")
    if not path:
        raise HTTPException(
            status_code=404,
            detail="Code was not pushed to GitHub (repo/token may be unset or push failed)."
        )
    repo = (GITHUB_REPO or "").strip()
    if not repo:
        raise HTTPException(status_code=404, detail="GitHub repo not configured")
    branch = task.get("github_branch") or "main"
    url = f"https://github.com/{repo}/tree/{branch}/{path}"
    return JSONResponse(content={"url": url})