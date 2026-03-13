from fastapi import APIRouter, File, Form, Request, UploadFile, status, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse
from app.models.schemas import User, Task, Project, CodeSubmission, TaskReview, DeveloperSession, RunPythonRequest, CodeHistory, GitHubConfigUpdate
from app.models.db import get_db
from app.services.assignment import assign_task_to_developer
from app.services.history_service import save_version
from app.services.workload import choose_project_auto_assignees, enrich_developers_with_workload
from bson import ObjectId
import os
import re
import base64
import io
import zipfile
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import httpx
import subprocess
import tempfile
import sys
import asyncio
import signal

load_dotenv()

router = APIRouter()

# GitHub config (optional: if set, approved task code is pushed to repo instead of local archive)
# Support .env with or without spaces around =
GITHUB_REPO = (os.getenv("GITHUB_REPO") or os.getenv("GITHUB_REPO ") or "").strip()
GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN ") or "").strip()
GITHUB_SETTINGS_DOC_ID = "github_integration"
PROFILE_PHOTO_DIR = Path(__file__).resolve().parent.parent / "static" / "uploads" / "profile_photos"
MAX_PROFILE_PHOTO_BYTES = 2 * 1024 * 1024
ALLOWED_PROFILE_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _require_manager(request: Request) -> str:
    username = request.cookies.get("username")
    role = request.cookies.get("role")
    if not username or role != "manager":
        raise HTTPException(status_code=401, detail="Only managers can access this action")
    return username


def _approved_developer_query(extra_filter: dict | None = None) -> dict:
    clauses = [
        {"role": {"$ne": "manager"}},
        {"$or": [{"approval_status": "approved"}, {"approval_status": {"$exists": False}}]},
    ]
    if extra_filter:
        clauses.append(extra_filter)
    return {"$and": clauses}


def _approval_status(user: dict | None) -> str:
    if not user:
        return "missing"
    return (user.get("approval_status") or "approved").strip().lower()


def _serialize_pending_developer(user: dict) -> dict:
    created_at = user.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    return {
        "username": user.get("username", ""),
        "role": user.get("role", ""),
        "description": user.get("description", ""),
        "created_at": created_at,
    }


def _env_github_config() -> dict:
    repo = (GITHUB_REPO or "").strip()
    token = (GITHUB_TOKEN or "").strip()
    source = "env" if repo and token else "unset"
    return {"repo": repo, "token": token, "source": source}


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_permission_hint(response: httpx.Response) -> str:
    accepted_permissions = (response.headers.get("X-Accepted-GitHub-Permissions") or "").strip()
    granted_scopes = (response.headers.get("X-OAuth-Scopes") or "").strip()
    parts: list[str] = []
    if accepted_permissions:
        parts.append(f"GitHub reports this endpoint requires: {accepted_permissions}.")
    if granted_scopes:
        parts.append(f"Current token scopes: {granted_scopes}.")
    return " ".join(parts)


def _github_error_detail(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    message = payload.get("message")
    if message:
        detail = f"{fallback}: {message}"
        if "resource not accessible by personal access token" in message.lower():
            hint = (
                "Use a classic personal access token with the `repo` scope, "
                "or a fine-grained personal access token with repository "
                "`Contents` permission set to `Read and write`. "
                "If this is an organization repository, the token may also need "
                "organization approval or SSO authorization."
            )
            permission_hint = _github_permission_hint(response)
            if permission_hint:
                hint = f"{hint} {permission_hint}"
            return f"{detail}. {hint}"
        return detail
    return fallback


def _ensure_classic_token_has_repo_scope(response: httpx.Response) -> None:
    granted_scopes = {
        scope.strip()
        for scope in (response.headers.get("X-OAuth-Scopes") or "").split(",")
        if scope.strip()
    }
    if granted_scopes and "repo" not in granted_scopes:
        raise HTTPException(
            status_code=400,
            detail=(
                "The GitHub token can read the repository but does not have write scope. "
                "Use a classic personal access token with the `repo` scope, "
                "or a fine-grained personal access token with repository "
                "`Contents` permission set to `Read and write`."
            ),
        )


async def _get_saved_github_settings(db):
    return await db.app_settings.find_one({"_id": GITHUB_SETTINGS_DOC_ID}) or {}


async def _get_effective_github_config(db) -> dict:
    saved = await _get_saved_github_settings(db)
    if saved:
        return {
            "repo": (saved.get("repo") or "").strip(),
            "token": (saved.get("token") or "").strip(),
            "source": "database",
            "updated_at": saved.get("updated_at"),
            "updated_by": saved.get("updated_by"),
        }
    return {
        **_env_github_config(),
        "updated_at": None,
        "updated_by": None,
    }


async def _validate_github_config(repo: str, token: str) -> tuple[str, str, str, str]:
    repo = (repo or "").strip().strip("/")
    token = (token or "").strip()
    if not repo or "/" not in repo:
        raise HTTPException(status_code=400, detail="Repository must use the format owner/repo")
    if not token:
        raise HTTPException(status_code=400, detail="GitHub token is required")

    owner, repo_name = repo.split("/", 1)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo_name}",
            headers=_github_headers(token),
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=_github_error_detail(response, "Unable to access the GitHub repository"),
        )
    data = response.json()
    _ensure_classic_token_has_repo_scope(response)
    full_name = (data.get("full_name") or repo).strip()
    default_branch = data.get("default_branch") or "main"
    return owner, repo_name, full_name, default_branch


async def _upsert_github_file(
    client: httpx.AsyncClient,
    owner: str,
    repo_name: str,
    branch: str,
    path: str,
    content_b64: str,
    message: str,
    token: str,
) -> tuple[bool, str | None]:
    headers = _github_headers(token)
    contents_url = f"https://api.github.com/repos/{owner}/{repo_name}/contents/{path}"
    existing = await client.get(contents_url, headers=headers, params={"ref": branch})
    payload = {
        "message": message,
        "content": content_b64,
        "branch": branch,
    }

    if existing.status_code == 200:
        sha = existing.json().get("sha")
        if sha:
            payload["sha"] = sha
    elif existing.status_code != 404:
        return False, _github_error_detail(existing, f"Unable to prepare {path} for upload")

    result = await client.put(contents_url, headers=headers, json=payload)
    if result.status_code not in (200, 201):
        return False, _github_error_detail(result, f"Unable to push {path} to GitHub")
    return True, None


def _slug_for_task(title: str, task_id: str) -> str:
    """Sanitize task title + short id into a safe directory name."""
    slug = re.sub(r"[^\w\s-]", "", (title or "Untitled").lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-") or "task"
    short_id = str(task_id)[-6:] if len(str(task_id)) >= 6 else str(task_id)
    return f"{slug}-{short_id}"


async def _push_task_to_github(task: dict, repo: str, token: str) -> tuple[str | None, str | None, str | None]:
    """
    Push task code and metadata to GitHub repo as a new directory under 'tasks/<slug>/'.
    Returns (github_path, branch, error) on success/failure.
    """
    if not repo or not token:
        return None, None, "GitHub is not connected"

    owner, repo_name, _, branch = await _validate_github_config(repo, token)
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

    async with httpx.AsyncClient(timeout=30.0) as client:
        code_ok, code_error = await _upsert_github_file(
            client,
            owner,
            repo_name,
            branch,
            f"{dir_path}/code.{file_ext}",
            base64.b64encode(code.encode("utf-8")).decode("ascii"),
            f"Sync task code: {task_title}",
            token,
        )
        if not code_ok:
            return None, None, code_error or "Unable to push task code to GitHub"

        metadata_ok, metadata_error = await _upsert_github_file(
            client,
            owner,
            repo_name,
            branch,
            f"{dir_path}/metadata.txt",
            base64.b64encode(metadata_content.encode("utf-8")).decode("ascii"),
            f"Sync task metadata: {task_title}",
            token,
        )
        if not metadata_ok:
            return None, None, metadata_error or "Unable to push task metadata to GitHub"

    return dir_path, branch, None


async def _push_project_to_github(
    project: dict,
    tasks: list[dict],
    repo: str,
    token: str,
) -> tuple[str | None, str | None, int, str | None]:
    """
    Push a completed project's code into a stable directory under 'projects/<slug>/'.
    Returns (github_path, branch, pushed_file_count, error).
    """
    if not repo or not token:
        return None, None, 0, "GitHub is not connected"

    code_tasks = [task for task in tasks if (task.get("code") or "").strip()]
    if not code_tasks:
        return None, None, 0, "This project does not have any code files to push"

    owner, repo_name, _, branch = await _validate_github_config(repo, token)
    project_id_str = str(project["_id"])
    project_title = project.get("title", "Untitled")
    base_dir = (
        (project.get("github_path") or "").strip().strip("/")
        or f"projects/{_slug_for_task(project_title, project_id_str)}"
    )
    exported_at = datetime.now().isoformat()

    readme_lines = [
        f"# {project_title}",
        "",
        f"Project ID: {project_id_str}",
        f"Status: {project.get('status') or 'completed'}",
        f"Exported At: {exported_at}",
    ]
    if project.get("description"):
        readme_lines.extend(["", "Description:", project["description"]])
    assigned_developers = project.get("assigned_developers") or []
    if assigned_developers:
        readme_lines.append("")
        readme_lines.append("Assigned Developers:")
        readme_lines.extend([f"- {username}" for username in assigned_developers])
    readme_lines.append("")
    readme_lines.append("Included Tasks:")
    for task in code_tasks:
        readme_lines.append(
            f"- {task.get('title', 'Untitled')} ({task.get('type', 'unknown')}) - "
            f"{task.get('assigned_to', 'Unassigned')}"
        )
    readme_content = "\n".join(readme_lines)

    async with httpx.AsyncClient(timeout=30.0) as client:
        readme_ok, readme_error = await _upsert_github_file(
            client,
            owner,
            repo_name,
            branch,
            f"{base_dir}/README.md",
            base64.b64encode(readme_content.encode("utf-8")).decode("ascii"),
            f"Sync completed project: {project_title}",
            token,
        )
        if not readme_ok:
            return None, None, 0, readme_error or "Unable to push project summary to GitHub"

        pushed_count = 0
        for task in code_tasks:
            task_id_str = str(task["_id"])
            task_slug = _slug_for_task(task.get("title", "Untitled"), task_id_str)
            task_type = task.get("type") or "backend"
            file_ext = "html" if task_type == "frontend" else "py"
            code_dir = "frontend" if task_type == "frontend" else "backend"
            code_path = f"{base_dir}/{code_dir}/{task_slug}.{file_ext}"
            code_ok, code_error = await _upsert_github_file(
                client,
                owner,
                repo_name,
                branch,
                code_path,
                base64.b64encode((task.get("code") or "").encode("utf-8")).decode("ascii"),
                f"Sync completed project code: {project_title} / {task.get('title', 'Untitled')}",
                token,
            )
            if not code_ok:
                return None, None, pushed_count, code_error or "Unable to push project code to GitHub"

            metadata_lines = [
                f"Task ID: {task_id_str}",
                f"Title: {task.get('title', 'N/A')}",
                f"Type: {task_type}",
                f"Assigned To: {task.get('assigned_to', 'N/A')}",
                f"Status: {task.get('status', 'N/A')}",
                f"Framework: {task.get('framework') or 'N/A'}",
                f"Exported At: {exported_at}",
            ]
            if task.get("description"):
                metadata_lines.extend(["", "Description:", task["description"]])
            metadata_content = "\n".join(metadata_lines)
            metadata_ok, metadata_error = await _upsert_github_file(
                client,
                owner,
                repo_name,
                branch,
                f"{base_dir}/task-metadata/{task_slug}.txt",
                base64.b64encode(metadata_content.encode("utf-8")).decode("ascii"),
                f"Sync completed project metadata: {project_title} / {task.get('title', 'Untitled')}",
                token,
            )
            if not metadata_ok:
                return None, None, pushed_count, metadata_error or "Unable to push project metadata to GitHub"

            pushed_count += 1

    return base_dir, branch, pushed_count, None

# Seed data for MongoDB
seed_users = [
    {
        "username": "manager",
        "password": "manager123",
        "role": "manager",
        "description": "Project manager with experience in agile methodologies and team leadership.",
        "photo_url": None,
        "approval_status": "approved",
    },
    {
        "username": "frontend",
        "password": "dev123",
        "role": "frontend developer",
        "description": "Frontend developer with expertise in HTML, CSS, JavaScript, and React.",
        "photo_url": None,
        "approval_status": "approved",
    },
    {
        "username": "backend",
        "password": "dev123",
        "role": "backend developer",
        "description": "Backend developer with expertise in Python, FastAPI, and MongoDB.",
        "photo_url": None,
        "approval_status": "approved",
    },
]

async def migrate_usernames(db):
    """Rename legacy dev1 -> frontend and dev2 -> backend across all collections."""
    renames = [("dev1", "frontend"), ("dev2", "backend")]
    for old, new in renames:
        user = await db.users.find_one({"username": old})
        if user:
            await db.users.update_one({"username": old}, {"$set": {"username": new}})
            await db.tasks.update_many({"assigned_to": old}, {"$set": {"assigned_to": new}})
            await db.projects.update_many(
                {"assigned_developers": old},
                {"$set": {"assigned_developers.$[elem]": new}},
                array_filters=[{"elem": {"$eq": old}}]
            )
            await db.chat_messages.update_many({"sender_username": old}, {"$set": {"sender_username": new}})
            await db.developer_sessions.update_many({"username": old}, {"$set": {"username": new}})
            print(f"[migrate] Renamed user '{old}' -> '{new}' in all collections.")

@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...)):
    db = get_db()
    user = await db.users.find_one({"username": username})
    
    if user and user["password"] == password:
        # Validate that the selected role matches the user's actual role
        if role != user["role"]:
            return RedirectResponse(url="/?error=role_mismatch", status_code=status.HTTP_302_FOUND)

        if user["role"] != "manager" and _approval_status(user) != "approved":
            return RedirectResponse(url="/?error=pending_approval", status_code=status.HTTP_302_FOUND)

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

    is_manager = role == "manager"
        
    new_user = {
        "username": username,
        "password": password,
        "role": role,
        "description": description,
        "photo_url": None,
        "approval_status": "approved" if is_manager else "pending",
        "created_at": datetime.utcnow(),
    }
    
    await db.users.insert_one(new_user)
    success_flag = "registered" if is_manager else "pending_approval"
    return RedirectResponse(url=f"/?success={success_flag}", status_code=status.HTTP_302_FOUND)


@router.post("/api/profile/photo")
async def upload_profile_photo(request: Request, photo: UploadFile = File(...)):
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not photo.filename:
        raise HTTPException(status_code=400, detail="Select an image to upload")

    ext = Path(photo.filename).suffix.lower()
    if ext not in ALLOWED_PROFILE_PHOTO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Use PNG, JPG, JPEG, GIF, or WEBP for profile photos")
    if not (photo.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    content = await photo.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")
    if len(content) > MAX_PROFILE_PHOTO_BYTES:
        raise HTTPException(status_code=400, detail="Profile photo must be 2 MB or smaller")

    db = get_db()
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    PROFILE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    safe_username = re.sub(r"[^a-zA-Z0-9_-]", "-", username).strip("-") or "user"
    version = int(datetime.utcnow().timestamp())
    filename = f"{safe_username}-{version}{ext}"
    file_path = PROFILE_PHOTO_DIR / filename
    file_path.write_bytes(content)

    previous_url = (user.get("photo_url") or "").split("?", 1)[0]
    if previous_url.startswith("/static/uploads/profile_photos/"):
        previous_name = Path(previous_url).name
        previous_path = PROFILE_PHOTO_DIR / previous_name
        if previous_path.exists() and previous_path != file_path:
            previous_path.unlink()

    photo_url = f"/static/uploads/profile_photos/{filename}?v={version}"
    await db.users.update_one({"username": username}, {"$set": {"photo_url": photo_url}})
    return JSONResponse(content={"success": True, "photo_url": photo_url})


@router.post("/api/developers/{username}/approve")
async def approve_developer(request: Request, username: str):
    manager_username = _require_manager(request)
    db = get_db()
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="Developer request not found")
    if user.get("role") == "manager":
        raise HTTPException(status_code=400, detail="Manager accounts do not go through developer approval")
    if _approval_status(user) == "approved":
        raise HTTPException(status_code=400, detail="This developer is already approved")
    if _approval_status(user) != "pending":
        raise HTTPException(status_code=400, detail="This developer is not waiting for approval")

    await db.users.update_one(
        {"username": username},
        {"$set": {
            "approval_status": "approved",
            "approved_at": datetime.utcnow(),
            "approved_by": manager_username,
        }},
    )
    return JSONResponse(content={
        "success": True,
        "message": f"{username} was approved and is now available for project assignment.",
    })


@router.get("/api/developers/pending_requests")
async def list_pending_developers(request: Request):
    _require_manager(request)
    db = get_db()
    cursor = db.users.find({
        "$and": [
            {"role": {"$ne": "manager"}},
            {"approval_status": "pending"},
        ]
    }).sort("created_at", -1)
    users = await cursor.to_list(length=50)
    return JSONResponse(content={
        "requests": [_serialize_pending_developer(user) for user in users]
    })


@router.post("/api/developers/{username}/reject")
async def reject_developer(request: Request, username: str):
    _require_manager(request)
    db = get_db()
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="Developer request not found")
    if user.get("role") == "manager":
        raise HTTPException(status_code=400, detail="Manager accounts do not go through developer approval")
    if _approval_status(user) != "pending":
        raise HTTPException(status_code=400, detail="Only pending developer requests can be rejected")

    await db.users.delete_one({"username": username})
    await db.developer_sessions.delete_many({"username": username})
    await db.chat_messages.delete_many({"sender_username": username})
    return JSONResponse(content={
        "success": True,
        "message": f"{username} was rejected and removed.",
    })

@router.post("/add_project")
async def add_project(request: Request, title: str = Form(...), description: str = Form(None)):
    db = get_db()

    form_data = await request.form()
    assign_mode = (form_data.get("assign_mode") or "manual").strip().lower()
    assigned_developers = form_data.getlist("assigned_developers")

    if assign_mode == "ai":
        developers_cursor = db.users.find(_approved_developer_query())
        developers = await developers_cursor.to_list(length=200)
        developers = await enrich_developers_with_workload(db, developers)
        assigned_developers = choose_project_auto_assignees(developers)

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

@router.get("/api/projects/{project_id}/download")
async def download_project(project_id: str):
    """Download a zip of all code files in the completed project."""
    db = get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    tasks_cursor = db.tasks.find({"project_id": project_id})
    tasks = await tasks_cursor.to_list(length=1000)
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for task in tasks:
            if task.get("code"):
                ext = "html" if task.get("type") == "frontend" else "py"
                folder = "frontend/" if task.get("type") == "frontend" else "backend/"
                filename = f"{folder}{task.get('title', 'Untitled')}.{ext}"
                zip_file.writestr(filename, task.get("code", ""))
                
    zip_buffer.seek(0)
    project_slug = _slug_for_task(project.get("title", ""), str(project["_id"]))
    
    headers = {
        "Content-Disposition": f'attachment; filename="{project_slug}.zip"'
    }
    
    from fastapi.responses import Response
    return Response(content=zip_buffer.getvalue(), media_type="application/zip", headers=headers)

@router.post("/add_task")
async def add_task(request: Request, title: str = Form(...), description: str = Form(None), project_id: str = Form(...), priority: str = Form("medium"), framework: str = Form(None)):
    db = get_db()

    # Get the project to find the assigned developers
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    assigned_devs = project.get("assigned_developers", [])

    # Get all developers assigned to the project with role and description
    if assigned_devs:
        developers_cursor = db.users.find(_approved_developer_query({"username": {"$in": assigned_devs}}))
    else:
        # Fallback to all developers if none assigned to project
        developers_cursor = db.users.find(_approved_developer_query())

    developers = await developers_cursor.to_list(length=200)
    developers = [
        {"username": u["username"], "role": u.get("role", ""), "description": u.get("description", "")}
        for u in developers
    ]
    developers = await enrich_developers_with_workload(db, developers)

    assigned_to, task_type = assign_task_to_developer(title, description, developers)

    task = Task(title=title, type=task_type, project_id=project_id, assigned_to=assigned_to, description=description, priority=priority, framework=framework)
    await db.tasks.insert_one(task.dict())

    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@router.post("/api/edit_task/{task_id}")
async def edit_task(task_id: str, title: str = Form(...), description: str = Form(None), project_id: str = Form(...), priority: str = Form("medium"), framework: str = Form(None)):
    db = get_db()
    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {
            "title": title,
            "description": description,
            "project_id": project_id,
            "priority": priority,
            "framework": framework
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
    # Save code history before updating the task
    await save_version(submission.task_id, submission.code, username)

    result = await db.tasks.update_one(
        {"_id": ObjectId(submission.task_id), "assigned_to": username},
        {"$set": {"code": submission.code, "status": "submitted_for_review", "comments": None}}
    )
    
    if result.matched_count == 1:
        print(f"--- Code Submitted for Task ID: {submission.task_id} ---")
        return JSONResponse(content={"success": True, "message": "Code submitted successfully for review"})
    
    # If no match, check if task exists at all to give better error
    task_exists = await db.tasks.find_one({"_id": ObjectId(submission.task_id)})
    if not task_exists:
        raise HTTPException(status_code=404, detail=f"Task {submission.task_id} not found")
    else:
        actual_owner = task_exists.get("assigned_to")
        raise HTTPException(status_code=403, detail=f"Task is assigned to {actual_owner}, not {username}")

@router.get("/get_task_code/{task_id}")
async def get_task_code(request: Request, task_id: str):
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if task:
        return {"code": task.get("code", "")}
    return {"code": ""}

# Timeout (seconds) for one-shot Python execution
RUN_PYTHON_TIMEOUT = 30
# Interactive terminal sessions should survive while the user is thinking/responding.
RUN_PYTHON_WS_IDLE_TIMEOUT = 600
RUN_PYTHON_WS_MAX_TIMEOUT = 1800


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


@router.websocket("/api/run_python_ws")
async def run_python_ws(websocket: WebSocket):
    """
    WebSocket endpoint for interactive Python execution.
    Expects the first message to be the code. Subsequent messages are stdin.
    """
    await websocket.accept()
    
    # Receive initial code
    try:
        data = await websocket.receive_json()
        code = (data.get("code") or "").strip()
    except Exception:
        await websocket.close(code=1003) # Unsupported Data
        return

    if not code:
        await websocket.send_json({"type": "exit", "code": 0})
        await websocket.close()
        return

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name

    process = None
    try:
        import subprocess
        process = subprocess.Popen(
            [sys.executable, tmp_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=None,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            bufsize=0
        )

        loop = asyncio.get_running_loop()
        session_started_at = loop.time()
        last_activity_at = session_started_at
        client_disconnected = asyncio.Event()

        def touch_activity():
            nonlocal last_activity_at
            last_activity_at = loop.time()

        async def stop_process():
            if not process or process.poll() is not None:
                return
            process.terminate()
            try:
                await asyncio.wait_for(loop.run_in_executor(None, process.wait), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()

        async def stream_output(stream, stream_type):
            while True:
                try:
                    # Using read1() if available (otherwise read) for non-blocking partial reads
                    read_func = getattr(stream, 'read1', stream.read)
                    chunk = await loop.run_in_executor(None, read_func, 4096)
                    if not chunk:
                        break
                    touch_activity()
                    await websocket.send_json({"type": "output", "stream": stream_type, "data": chunk.decode(errors="replace")})
                except Exception:
                    break

        async def handle_input():
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "input":
                        input_data = data.get("data", "")
                        if process.stdin:
                            try:
                                process.stdin.write(input_data.encode())
                                process.stdin.flush()
                                touch_activity()
                            except Exception:
                                pass
            except WebSocketDisconnect:
                client_disconnected.set()
            except Exception:
                client_disconnected.set()

        # Run stdout/stderr streaming and input handling concurrently
        output_tasks = [
            asyncio.create_task(stream_output(process.stdout, "stdout")),
            asyncio.create_task(stream_output(process.stderr, "stderr"))
        ]
        input_task = asyncio.create_task(handle_input())

        async def wait_for_process_exit():
            while True:
                if client_disconnected.is_set():
                    await stop_process()
                    return None

                returncode = process.poll()
                if returncode is not None:
                    return returncode

                now = loop.time()
                if now - session_started_at >= RUN_PYTHON_WS_MAX_TIMEOUT:
                    await stop_process()
                    await websocket.send_json({
                        "type": "timeout",
                        "message": f"Execution timed out after {RUN_PYTHON_WS_MAX_TIMEOUT // 60} minutes."
                    })
                    return None

                if now - last_activity_at >= RUN_PYTHON_WS_IDLE_TIMEOUT:
                    await stop_process()
                    await websocket.send_json({
                        "type": "timeout",
                        "message": f"Terminal session timed out after {RUN_PYTHON_WS_IDLE_TIMEOUT // 60} minutes of inactivity."
                    })
                    return None

                await asyncio.sleep(0.25)

        returncode = await wait_for_process_exit()

        # Ensure output streams finish reading (give them a little time)
        await asyncio.wait(output_tasks, timeout=1.0)
        input_task.cancel()

        if returncode is not None:
            await websocket.send_json({"type": "exit", "code": returncode})

    except Exception as e:
        import traceback
        traceback.print_exc()
        await websocket.send_json({"type": "error", "message": str(e) or repr(e)})
    finally:
        if process and getattr(process, 'poll', lambda: 1)() is None:
            process.kill()
        try:
            os.unlink(tmp_path)
        except:
            pass
        await websocket.close()

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

@router.get("/api/github/config")
async def get_github_config(request: Request):
    _require_manager(request)
    db = get_db()
    config = await _get_effective_github_config(db)
    return JSONResponse(content={
        "configured": bool(config.get("repo") and config.get("token")),
        "repo": config.get("repo") or "",
        "has_token": bool(config.get("token")),
        "source": config.get("source") or "unset",
        "updated_at": config["updated_at"].isoformat() if config.get("updated_at") else None,
        "updated_by": config.get("updated_by"),
    })


@router.post("/api/github/config")
async def save_github_config(request: Request, payload: GitHubConfigUpdate):
    username = _require_manager(request)
    db = get_db()
    existing = await _get_saved_github_settings(db)

    repo = (payload.repo or "").strip().strip("/")
    token = (payload.token or "").strip() or (existing.get("token") if existing else "")
    if not repo:
        raise HTTPException(status_code=400, detail="Repository is required")
    if not token:
        raise HTTPException(status_code=400, detail="GitHub token is required the first time you connect")

    _, _, full_name, default_branch = await _validate_github_config(repo, token)
    now = datetime.utcnow()
    await db.app_settings.update_one(
        {"_id": GITHUB_SETTINGS_DOC_ID},
        {
            "$set": {
                "repo": full_name,
                "token": token,
                "updated_at": now,
                "updated_by": username,
            }
        },
        upsert=True,
    )
    return JSONResponse(content={
        "success": True,
        "message": f"Connected to GitHub repository {full_name}",
        "repo": full_name,
        "default_branch": default_branch,
        "updated_at": now.isoformat(),
        "updated_by": username,
    })


@router.delete("/api/github/config")
async def delete_github_config(request: Request):
    _require_manager(request)
    db = get_db()
    await db.app_settings.delete_one({"_id": GITHUB_SETTINGS_DOC_ID})
    return JSONResponse(content={"success": True, "message": "GitHub connection removed"})


@router.post("/api/approve_task")
async def approve_task(request: Request, review: TaskReview):
    _require_manager(request)
    db = get_db()
    # Get task details before updating
    task = await db.tasks.find_one({"_id": ObjectId(review.task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # Push code to GitHub when marking as done (if configured)
    update_data = {"status": "done", "comments": "Approved by manager."}
    github_message = None
    github_config = await _get_effective_github_config(db)
    if task.get("code") and (github_config.get("repo") and github_config.get("token")):
        github_path, github_branch, github_error = await _push_task_to_github(
            task,
            github_config["repo"],
            github_config["token"],
        )
        if github_path:
            update_data["github_path"] = github_path
            update_data["github_branch"] = github_branch
            update_data["github_repo"] = github_config["repo"]
            github_message = "Code pushed to GitHub."
        elif github_error:
            github_message = f"GitHub push failed: {github_error}"
    elif task.get("code"):
        github_message = "GitHub is not connected."

    result = await db.tasks.update_one(
        {"_id": ObjectId(review.task_id)},
        {"$set": update_data}
    )

    if result.matched_count == 1:
        msg = "Task approved and marked as done"
        if github_message:
            msg += f" {github_message}"
        response = {"success": True, "message": msg}
        if update_data.get("github_path"):
            response["url"] = f"https://github.com/{update_data['github_repo']}/tree/{update_data['github_branch']}/{update_data['github_path']}"
        return JSONResponse(content=response)
    
    raise HTTPException(status_code=404, detail="Task not found")

@router.post("/api/reject_task")
async def reject_task(request: Request, review: TaskReview):
    _require_manager(request)
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


@router.post("/api/tasks/{task_id}/push_github")
async def push_task_to_github(request: Request, task_id: str):
    _require_manager(request)
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not (task.get("code") or "").strip():
        raise HTTPException(status_code=400, detail="This task does not have code to push")

    github_config = await _get_effective_github_config(db)
    if not (github_config.get("repo") and github_config.get("token")):
        raise HTTPException(status_code=400, detail="Connect GitHub on the manager dashboard first")

    github_path, github_branch, github_error = await _push_task_to_github(
        task,
        github_config["repo"],
        github_config["token"],
    )
    if not github_path:
        raise HTTPException(status_code=400, detail=github_error or "Failed to push code to GitHub")

    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {
            "github_path": github_path,
            "github_branch": github_branch,
            "github_repo": github_config["repo"],
        }}
    )
    url = f"https://github.com/{github_config['repo']}/tree/{github_branch}/{github_path}"
    return JSONResponse(content={
        "success": True,
        "message": "Code pushed to GitHub.",
        "url": url,
        "branch": github_branch,
    })


@router.post("/api/projects/{project_id}/push_github")
async def push_project_to_github(request: Request, project_id: str):
    _require_manager(request)
    db = get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if (project.get("status") or "").lower() != "completed":
        raise HTTPException(status_code=400, detail="Only completed projects can be pushed to GitHub")

    github_config = await _get_effective_github_config(db)
    if not (github_config.get("repo") and github_config.get("token")):
        raise HTTPException(status_code=400, detail="Connect GitHub on the manager dashboard first")

    tasks = await db.tasks.find({"project_id": project_id}).to_list(length=1000)
    github_path, github_branch, pushed_count, github_error = await _push_project_to_github(
        project,
        tasks,
        github_config["repo"],
        github_config["token"],
    )
    if not github_path:
        raise HTTPException(status_code=400, detail=github_error or "Failed to push completed project to GitHub")

    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {
            "github_path": github_path,
            "github_branch": github_branch,
            "github_repo": github_config["repo"],
        }},
    )
    url = f"https://github.com/{github_config['repo']}/tree/{github_branch}/{github_path}"
    return JSONResponse(content={
        "success": True,
        "message": f"Completed project pushed to GitHub ({pushed_count} file set{'s' if pushed_count != 1 else ''}).",
        "url": url,
        "branch": github_branch,
        "github_path": github_path,
        "pushed_count": pushed_count,
    })

@router.post("/api/save_task/{task_id}")
async def save_task(request: Request, task_id: str):
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id), "assigned_to": username})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")
    
    try:
        body = await request.json()
        code = body.get("code", None)
    except:
        code = None
    
    if code is not None:
        await db.tasks.update_one(
            {"_id": ObjectId(task_id)},
            {"$set": {"code": code}}
        )
        # Save code history when explicitly saving
        await save_version(task_id, code, username)
        return JSONResponse(content={"success": True, "message": "Progress saved successfully"})
    
    return JSONResponse(content={"success": False, "message": "No code provided to save"})

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
        # Save code history when pausing
        await save_version(task_id, code, username)
    
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

@router.get("/api/tasks/{task_id}/history")
async def get_task_history(request: Request, task_id: str):
    """Fetch code history for a task."""
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    db = get_db()
    cursor = db.code_history.find({"task_id": task_id}).sort("timestamp", -1)
    history = await cursor.to_list(length=100)
    
    # Format history for frontend
    formatted_history = []
    for entry in history:
        formatted_history.append({
            "code": entry["code"],
            "timestamp": entry["timestamp"].isoformat(),
            "username": entry["username"]
        })
        
    return {"history": formatted_history}

@router.post("/api/tasks/{task_id}/save-history")
async def save_task_history(request: Request, task_id: str):
    """Save the current code as a history entry when applying code from AI."""
    print(f"[DEBUG] save_task_history called with task_id: {task_id}")
    
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    db = get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id), "assigned_to": username})
    if not task:
        print(f"[DEBUG] Task not found for task_id: {task_id}, username: {username}")
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")
    
    try:
        body = await request.json()
        code = body.get("code", "")
    except Exception as e:
        print(f"[DEBUG] Error parsing request body: {e}")
        raise HTTPException(status_code=400, detail="Invalid request body")
    
    if not code:
        print(f"[DEBUG] Code is empty")
        raise HTTPException(status_code=400, detail="Code cannot be empty")
    
    print(f"[DEBUG] Saving code to history. Code length: {len(code)}, task_id: {task_id}, username: {username}")
    
    # Save the code version to history
    await save_version(task_id, code, username)
    
    print(f"[DEBUG] Code saved successfully to history")
    return JSONResponse(content={"success": True, "message": "Code saved to history"})

@router.get("/api/developer_analytics")
async def get_developer_analytics(request: Request):
    """Returns developer analytics for manager view. Manager-only."""
    role = request.cookies.get("role")
    if role != "manager":
        raise HTTPException(status_code=403, detail="Only managers can view developer analytics")
    db = get_db()
    developers_cursor = db.users.find(_approved_developer_query())
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
async def task_github_url(request: Request, task_id: str):
    """Return the GitHub URL for a completed task's code (when pushed to repo)."""
    if not request.cookies.get("username"):
        raise HTTPException(status_code=401, detail="Not authenticated")
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
    config = await _get_effective_github_config(db)
    repo = (task.get("github_repo") or config.get("repo") or "").strip()
    if not repo:
        raise HTTPException(status_code=404, detail="GitHub repo not configured")
    branch = task.get("github_branch") or "main"
    url = f"https://github.com/{repo}/tree/{branch}/{path}"
    return JSONResponse(content={"url": url})


@router.get("/api/project_github_url/{project_id}")
async def project_github_url(request: Request, project_id: str):
    """Return the GitHub URL for a completed project export."""
    if not request.cookies.get("username"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    db = get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    path = project.get("github_path")
    if not path:
        raise HTTPException(
            status_code=404,
            detail="Project was not pushed to GitHub yet."
        )
    config = await _get_effective_github_config(db)
    repo = (project.get("github_repo") or config.get("repo") or "").strip()
    if not repo:
        raise HTTPException(status_code=404, detail="GitHub repo not configured")
    branch = project.get("github_branch") or "main"
    url = f"https://github.com/{repo}/tree/{branch}/{path}"
    return JSONResponse(content={"url": url})
