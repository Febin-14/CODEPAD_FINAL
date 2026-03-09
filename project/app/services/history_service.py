from datetime import datetime
from app.models.db import get_db
from app.models.schemas import CodeHistory

async def save_version(task_id: str, code: str, username: str):
    """Saves a new version of code to the code_history collection."""
    db = get_db()
    
    # Optional: check if the new code is different from the last saved version to avoid duplicates
    last_version = await db.code_history.find_one(
        {"task_id": task_id},
        sort=[("timestamp", -1)]
    )
    
    if last_version and last_version.get("code") == code:
        return # No change, don't save duplicate
        
    history_entry = CodeHistory(
        task_id=task_id,
        code=code,
        timestamp=datetime.utcnow(),
        username=username
    )
    
    await db.code_history.insert_one(history_entry.dict())
