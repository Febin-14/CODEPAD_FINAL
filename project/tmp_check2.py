import asyncio
import json
from bson import ObjectId
from app.models.db import connect, get_db

class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, ObjectId):
            return str(o)
        return super().default(o)

async def check_db():
    connect() # initialize the client
    db = get_db()
    
    users = await db.users.find().to_list(length=100)
    projects = await db.projects.find().to_list(length=100)
    tasks = await db.tasks.find().to_list(length=100)
    
    with open('tmp_db2.json', 'w') as f:
        json.dump({'users': users, 'projects': projects, 'tasks': tasks}, f, cls=JSONEncoder, indent=2)

if __name__ == "__main__":
    asyncio.run(check_db())
