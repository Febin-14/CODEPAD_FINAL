from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import Request

MONGO_DETAILS = "mongodb://localhost:27017"

class MongoDB:
    client: AsyncIOMotorClient = None

mongodb = MongoDB()

def connect():
    mongodb.client = AsyncIOMotorClient(MONGO_DETAILS)
    return mongodb.client

def get_db():
    return mongodb.client['task_delegation']
