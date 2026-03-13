"""
Uses OpenAI to assign a task to the best-matching developer based on
task title/requirements and each developer's name, role, and description.
"""
import os
from typing import Optional

import google.generativeai as genai

from app.services.workload import developer_sort_key, normalize_specialty


def assign_task_to_developer(
    task_title: str,
    task_requirements: Optional[str],
    developers: list[dict],
) -> tuple[Optional[str], str]:
    """
    Returns (assigned_username, task_type).
    task_type is "frontend" or "backend" for UI/archive compatibility.
    If no developers or API fails, returns (None, "backend").
    """
    if not developers:
        _, task_type = _fallback_assign(task_title, task_requirements or "", [])
        return None, task_type

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        # Fallback: simple keyword match so app works without API key
        return _fallback_assign(task_title, task_requirements or "", developers)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash', generation_config={"temperature": 0.0})

    developers_text = "\n".join(
        (
            f"- Username: {d['username']}, Role: {d['role']}, "
            f"Free now: {'yes' if d.get('is_free') else 'no'}, "
            f"Active tasks: {d.get('active_task_count', 0)}, "
            f"Description: {d.get('description', '')}"
        )
        for d in developers
    )

    prompt = f"""You are a task assignment system. Given a task and a list of developers, choose exactly one developer who is the best fit. Consider role, description, and current availability. When multiple developers can do the work, prefer the one with fewer active tasks. Do not favor by name.

Task title: {task_title}
Task requirements: {task_requirements or "Not specified"}

Developers (username, role, description):
{developers_text}

Respond with exactly one line in this format:
ASSIGN: <username>
TYPE: <frontend or backend>

Use TYPE "frontend" if the task is primarily UI/frontend work, "backend" otherwise. Use the exact username from the list."""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        username = None
        task_type = "backend"
        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("ASSIGN:"):
                username = line.split(":", 1)[1].strip()
            elif line.upper().startswith("TYPE:"):
                t = line.split(":", 1)[1].strip().lower()
                if t in ("frontend", "backend"):
                    task_type = t
        if username and any(d["username"] == username for d in developers):
            return username, task_type
    except Exception:
        pass
    return _fallback_assign(task_title, task_requirements or "", developers)


def _fallback_assign(
    task_title: str, task_requirements: str, developers: list[dict]
) -> tuple[Optional[str], str]:
    """Assign by simple keyword match when OpenAI is not available."""
    text = (task_title + " " + (task_requirements or "")).lower()
    
    frontend_keywords = {"frontend", "ui", "html", "css", "javascript", "react", "page", "layout", "button", "form"}
    backend_keywords = {"backend", "api", "database", "sql", "mongo", "python", "fastapi", "server", "model"}

    has_frontend = any(k in text for k in frontend_keywords)
    has_backend = any(k in text for k in backend_keywords)
    
    is_frontend = has_frontend and not has_backend
    
    # Explicit override: if it says "backend", it is backend.
    if "backend" in text:
        is_frontend = False
    elif "frontend" in text:
        is_frontend = True

    task_type = "frontend" if is_frontend else "backend"

    if not developers:
        return None, task_type

    matching_developers = [
        developer
        for developer in developers
        if normalize_specialty(developer.get("role")) == task_type
    ]
    candidate_pool = matching_developers or developers
    best_developer = sorted(
        candidate_pool,
        key=lambda developer: developer_sort_key(developer, task_type),
    )[0]
    return best_developer["username"], task_type
