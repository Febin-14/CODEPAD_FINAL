# Project models for User and Task
from typing import Optional
from datetime import datetime
from pydantic import BaseModel


class DeveloperSession(BaseModel):
    """Stored in MongoDB for developer login/logout time tracking."""
    username: str
    login_at: datetime
    logout_at: Optional[datetime] = None
    duration_minutes: Optional[float] = None  # set when session is closed


class User(BaseModel):
    username: str
    password: str
    role: str
    description: str

class Project(BaseModel):
    title: str
    description: Optional[str] = None
    assigned_developers: list[str] = []  # list of usernames
    status: Optional[str] = "active"  # 'active' or 'completed'

class Task(BaseModel):
    title: str
    type: str  # 'frontend' or 'backend'
    project_id: Optional[str] = None
    assigned_to: Optional[str] = None  # developer username
    description: Optional[str] = None
    status: Optional[str] = "new"
    priority: Optional[str] = "medium"  # 'low', 'medium', or 'high'
    code: Optional[str] = None  # Store submitted code
    comments: Optional[str] = None  # Manager's review comments

class CodeSubmission(BaseModel):
    task_id: str
    code: str


class RunPythonRequest(BaseModel):
    code: str

class TaskReview(BaseModel):
    task_id: str
    comments: Optional[str] = None  # Required if rejecting

class ChatMessage(BaseModel):
    sender_username: str
    sender_role: str  # 'manager' or 'developer'
    message: str
    created_at: Optional[str] = None  # ISO timestamp, set server-side

class CodeHistory(BaseModel):
    """Stored in MongoDB to keep track of previous versions of code for a task."""
    task_id: str
    code: str
    timestamp: datetime
    username: str
