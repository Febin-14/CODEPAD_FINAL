import asyncio
import os
from app.services.assignment import assign_task_to_developer
from app.models.db import connect, get_db
from dotenv import load_dotenv

load_dotenv()

async def test_assign():
    connect() # initialize the client
    db = get_db()
    
    developers_cursor = db.users.find({"role": {"$ne": "manager"}})
    developers = await developers_cursor.to_list(length=100)
    developers = [
        {"username": u["username"], "role": u.get("role", ""), "description": u.get("description", "")}
        for u in developers
    ]
    
    print("API KEY:", bool(os.environ.get("OPENAI_API_KEY")))
    
    assigned, type_ = assign_task_to_developer("web", "create a web app", developers)
    print("ASSIGNED:", assigned, type_)

    assigned, type_ = assign_task_to_developer("backend page", "describe requirements.", developers)
    print("ASSIGNED BACKEND PAGE:", assigned, type_)

if __name__ == "__main__":
    asyncio.run(test_assign())
