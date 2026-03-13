from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable


ACTIVE_TASK_STATUSES = {"new", "in_progress", "paused", "submitted_for_review"}


def normalize_specialty(role: str | None) -> str:
    role_value = (role or "").strip().lower()
    if "frontend" in role_value:
        return "frontend"
    if "backend" in role_value:
        return "backend"
    return "general"


def developer_sort_key(developer: dict, preferred_specialty: str | None = None) -> tuple:
    specialty = normalize_specialty(developer.get("role"))
    if preferred_specialty:
        if specialty == preferred_specialty:
            specialty_rank = 0
        elif specialty == "general":
            specialty_rank = 1
        else:
            specialty_rank = 2
    else:
        specialty_rank = 0
    return (
        specialty_rank,
        int(developer.get("active_task_count") or 0),
        int(developer.get("in_progress_task_count") or 0),
        int(developer.get("submitted_task_count") or 0),
        int(developer.get("total_task_count") or 0),
        (developer.get("username") or "").lower(),
    )


async def enrich_developers_with_workload(db, developers: Iterable[dict]) -> list[dict]:
    developer_list = [dict(dev) for dev in developers]
    usernames = [dev.get("username") for dev in developer_list if dev.get("username")]
    if not usernames:
        return developer_list

    tasks = await db.tasks.find({"assigned_to": {"$in": usernames}}).to_list(length=5000)
    open_sessions = await db.developer_sessions.find(
        {"username": {"$in": usernames}, "logout_at": None}
    ).to_list(length=1000)

    total_task_counts: Counter[str] = Counter()
    active_task_counts: Counter[str] = Counter()
    in_progress_task_counts: Counter[str] = Counter()
    submitted_task_counts: Counter[str] = Counter()
    done_task_counts: Counter[str] = Counter()

    for task in tasks:
        username = task.get("assigned_to")
        if not username:
            continue
        total_task_counts[username] += 1
        status = (task.get("status") or "new").strip().lower()
        if status in ACTIVE_TASK_STATUSES:
            active_task_counts[username] += 1
        if status == "in_progress":
            in_progress_task_counts[username] += 1
        elif status == "submitted_for_review":
            submitted_task_counts[username] += 1
        elif status == "done":
            done_task_counts[username] += 1

    online_users = {session.get("username") for session in open_sessions if session.get("username")}

    enriched_developers: list[dict] = []
    for developer in developer_list:
        username = developer.get("username")
        active_task_count = int(active_task_counts.get(username, 0))
        is_free = active_task_count == 0
        developer["specialty"] = normalize_specialty(developer.get("role"))
        developer["total_task_count"] = int(total_task_counts.get(username, 0))
        developer["active_task_count"] = active_task_count
        developer["in_progress_task_count"] = int(in_progress_task_counts.get(username, 0))
        developer["submitted_task_count"] = int(submitted_task_counts.get(username, 0))
        developer["done_task_count"] = int(done_task_counts.get(username, 0))
        developer["is_free"] = is_free
        developer["availability_label"] = "Free" if is_free else "Busy"
        developer["availability_detail"] = (
            "No active tasks"
            if is_free
            else f"{active_task_count} active task{'s' if active_task_count != 1 else ''}"
        )
        developer["is_online"] = username in online_users
        enriched_developers.append(developer)

    return enriched_developers


def choose_project_auto_assignees(developers: Iterable[dict]) -> list[str]:
    ranked_by_specialty: dict[str, list[dict]] = defaultdict(list)
    all_developers = list(developers)

    for developer in all_developers:
        ranked_by_specialty[normalize_specialty(developer.get("role"))].append(developer)

    selected_usernames: list[str] = []
    for specialty in ("frontend", "backend"):
        candidates = ranked_by_specialty.get(specialty) or []
        if not candidates:
            continue
        selected_usernames.append(
            sorted(candidates, key=lambda dev: developer_sort_key(dev, specialty))[0]["username"]
        )

    if selected_usernames:
        return selected_usernames

    return [
        developer["username"]
        for developer in sorted(all_developers, key=developer_sort_key)[:2]
        if developer.get("username")
    ]


def compute_project_progress(project: dict, tasks: Iterable[dict]) -> dict:
    task_list = list(tasks)
    total_tasks = len(task_list)
    done_tasks = sum(1 for task in task_list if (task.get("status") or "").lower() == "done")
    active_tasks = sum(
        1 for task in task_list if (task.get("status") or "new").strip().lower() in ACTIVE_TASK_STATUSES
    )
    review_tasks = sum(
        1 for task in task_list if (task.get("status") or "").strip().lower() == "submitted_for_review"
    )

    if (project.get("status") or "").strip().lower() == "completed":
        completion_percent = 100
    elif total_tasks == 0:
        completion_percent = 0
    else:
        completion_percent = round((done_tasks / total_tasks) * 100)

    return {
        "total_tasks": total_tasks,
        "done_tasks": done_tasks,
        "active_tasks": active_tasks,
        "review_tasks": review_tasks,
        "completion_percent": completion_percent,
    }
