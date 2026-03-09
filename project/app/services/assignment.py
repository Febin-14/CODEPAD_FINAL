"""
Uses OpenAI to assign a task to the best-matching developer based on
task title/requirements and each developer's name, role, and description.
"""
import os
from typing import Optional

from openai import OpenAI


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
        return None, "backend"

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # Fallback: simple keyword match so app works without API key
        return _fallback_assign(task_title, task_requirements or "", developers)

    client = OpenAI(api_key=api_key)

    developers_text = "\n".join(
        f"- Username: {d['username']}, Role: {d['role']}, Description: {d.get('description', '')}"
        for d in developers
    )

    prompt = f"""You are a task assignment system. Given a task and a list of developers, choose exactly one developer who is the best fit. Consider role and description only; do not favor by name.

Task title: {task_title}
Task requirements: {task_requirements or "Not specified"}

Developers (username, role, description):
{developers_text}

Respond with exactly one line in this format:
ASSIGN: <username>
TYPE: <frontend or backend>

Use TYPE "frontend" if the task is primarily UI/frontend work, "backend" otherwise. Use the exact username from the list."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip()
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
    frontend_keywords = ("frontend", "ui", "html", "css", "javascript", "react", "page", "layout", "button", "form")
    is_frontend = any(k in text for k in frontend_keywords)

    for d in developers:
        role = (d.get("role") or "").lower()
        if is_frontend and "frontend" in role:
            return d["username"], "frontend"
        if not is_frontend and "backend" in role:
            return d["username"], "backend"

    # Default: first developer and infer type from their role
    first = developers[0]
    role = (first.get("role") or "").lower()
    task_type = "frontend" if "frontend" in role else "backend"
    return first["username"], task_type
