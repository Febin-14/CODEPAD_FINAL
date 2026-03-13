from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import Request

import os

MONGO_DETAILS = os.getenv("MONGO_URI", "mongodb+srv://Febin_User:Panickan%407019@codepad.lvufrmj.mongodb.net/?appName=CodePad")

class MongoDB:
    client: AsyncIOMotorClient = None

mongodb = MongoDB()

def connect():
    mongodb.client = AsyncIOMotorClient(MONGO_DETAILS)
    return mongodb.client

def get_db():
    return mongodb.client['task_delegation']
