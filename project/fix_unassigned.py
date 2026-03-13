import asyncio
from app.models.db import connect, get_db
from app.services.assignment import assign_task_to_developer
from bson import ObjectId

async def fix_unassigned():
    connect()
    db = get_db()
    
    global_devs_cursor = db.users.find({"role": {"$ne": "manager"}})
    global_devs = await global_devs_cursor.to_list(length=100)
    developers = [
        {"username": u["username"], "role": u.get("role", ""), "description": u.get("description", "")}
        for u in global_devs
    ]

    tasks = await db.tasks.find().to_list(length=100)
    for t in tasks:
        # Check if project has specific devs
        project = await db.projects.find_one({"_id": ObjectId(t["project_id"])}) if t.get("project_id") else None
        assigned_devs = project.get("assigned_developers", []) if project else []
        
        pool = developers
        if assigned_devs:
            pool = [d for d in developers if d["username"] in assigned_devs]
        
        if not pool:
            pool = developers # fallback to global pool

        assigned_to, task_type = assign_task_to_developer(t["title"], t.get("description", ""), pool)
        
        # Only update if the assignment logic would actually change it now for tests
        if t.get("type") != task_type or t.get("assigned_to") != assigned_to:
            await db.tasks.update_one(
                {"_id": t["_id"]},
                {"$set": {"assigned_to": assigned_to, "type": task_type}}
            )
            print(f"Fixed task '{t['title']}' -> assigned_to: {assigned_to}, type: {task_type} (Previous: {t.get('assigned_to')}, {t.get('type')})")

if __name__ == "__main__":
    asyncio.run(fix_unassigned())
