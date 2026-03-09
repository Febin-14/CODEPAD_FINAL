from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Request, UploadFile, File
from app.models.websocket_manager import websocket_manager, chat_room_manager
from app.models.db import get_db
from bson import ObjectId
from datetime import datetime
import json
import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()


def _get_cookies_from_scope(scope):
    """Extract cookie dict from ASGI scope headers."""
    headers = scope.get("headers") or []
    for name, value in headers:
        if name == b"cookie":
            cookies = {}
            for part in value.decode().strip().split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
            return cookies
    return {}

@router.websocket("/ws/developer/{task_id}")
async def websocket_developer(websocket: WebSocket, task_id: str):
    """WebSocket endpoint for developers to send code updates"""
    try:
        # Verify task exists and is in progress
        db = get_db()
        task = await db.tasks.find_one({"_id": ObjectId(task_id)})
        if not task or task.get("status") != "in_progress":
            await websocket.close(code=1008, reason="Task not found or not in progress")
            return
        
        await websocket_manager.connect_developer(websocket, task_id)
        
        # Send current code to watching managers
        if task.get("code"):
            await websocket_manager.broadcast_code_update(task_id, task["code"])
        
        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)
                
                if message.get("type") == "code_update":
                    code = message.get("code", "")
                    # Broadcast to all watching managers
                    await websocket_manager.broadcast_code_update(task_id, code)
                    
        except WebSocketDisconnect:
            await websocket_manager.disconnect_developer(task_id)
            
    except Exception as e:
        print(f"WebSocket developer error: {e}")
        try:
            await websocket.close()
        except:
            pass
        await websocket_manager.disconnect_developer(task_id)

@router.websocket("/ws/manager/{task_id}")
async def websocket_manager_view(websocket: WebSocket, task_id: str):
    """WebSocket endpoint for managers to view live code"""
    try:
        # Verify task exists and is in progress
        db = get_db()
        task = await db.tasks.find_one({"_id": ObjectId(task_id)})
        if not task:
            await websocket.close(code=1008, reason="Task not found")
            return
        
        # Only allow viewing if task is in_progress
        if task.get("status") != "in_progress":
            await websocket.close(code=1008, reason="Task is not in progress")
            return
        
        await websocket_manager.connect_manager(websocket, task_id)
        
        # Send current code immediately if available
        if task.get("code"):
            initial_message = json.dumps({
                "type": "code_update",
                "code": task["code"]
            })
            await websocket.send_text(initial_message)
        
        # Also send initial code if developer is currently connected
        if websocket_manager.has_developer_connection(task_id):
            await websocket.send_text(json.dumps({
                "type": "developer_active",
                "active": True
            }))
        
        try:
            while True:
                # Keep connection alive and wait for any messages
                data = await websocket.receive_text()
                # Managers don't send messages, just receive
                
        except WebSocketDisconnect:
            await websocket_manager.disconnect_manager(websocket, task_id)
            
    except Exception as e:
        print(f"WebSocket manager error: {e}")
        try:
            await websocket.close()
        except:
            pass
        await websocket_manager.disconnect_manager(websocket, task_id)


# -------- Group Chat Room --------

@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint for group chat (managers + developers)."""
    cookies = _get_cookies_from_scope(websocket.scope)
    username = cookies.get("username")
    role = cookies.get("role")
    if not username or not role:
        await websocket.close(code=4001, reason="Not authenticated")
        return
    try:
        await chat_room_manager.connect(websocket, username, role)
        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                if msg.get("type") != "message":
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                db = get_db()
                now = datetime.utcnow()
                created_at = now.isoformat() + "Z"
                doc = {
                    "sender_username": username,
                    "sender_role": role,
                    "message": text,
                    "created_at": created_at,
                }
                await db.chat_messages.insert_one(doc)
                payload = {
                    "type": "message",
                    "sender_username": username,
                    "sender_role": role,
                    "message": text,
                    "created_at": created_at,
                }
                await chat_room_manager.broadcast(payload, exclude_websocket=websocket)
                # Also send to sender so their UI shows the message (with same format)
                try:
                    await websocket.send_text(json.dumps(payload))
                except Exception:
                    pass
        except WebSocketDisconnect:
            chat_room_manager.disconnect(websocket)
    except Exception as e:
        print(f"Chat WebSocket error: {e}")
        try:
            chat_room_manager.disconnect(websocket)
            await websocket.close()
        except Exception:
            pass


@router.get("/api/chat/messages")
async def get_chat_messages(request: Request, limit: int = 50, before: str = None):
    """Get recent chat messages for the group room."""
    if request.cookies.get("username") is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db = get_db()
    q = {}
    if before:
        q["created_at"] = {"$lt": before}
    cursor = db.chat_messages.find(q).sort("created_at", -1).limit(limit)
    messages = await cursor.to_list(length=limit)
    messages.reverse()
    return {"messages": [{"sender_username": m["sender_username"], "sender_role": m["sender_role"], "message": m["message"], "created_at": m["created_at"]} for m in messages]}


@router.post("/api/chat/ai")
async def chat_ai(request: Request):
    """Send a message to OpenAI GPT-4o and return the AI reply. Requires auth."""
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not body or "message" not in body:
        raise HTTPException(status_code=400, detail="Missing 'message' in body")
    text = (body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"reply": "AI chat is not configured. Please set OPENAI_API_KEY."}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        messages = []
        # If code context is provided (from in-editor AI assistant), add system prompt
        context = body.get("context")
        if context and isinstance(context, dict):
            code = context.get("code", "")
            language = context.get("language", "")
            task_title = context.get("task_title", "")
            system_msg = (
                "You are an expert coding assistant embedded in a code editor. "
                "The developer is working on a task and needs your help. "
                "Provide clear, concise answers with code examples when appropriate. "
                "Use markdown formatting with fenced code blocks."
            )
            if task_title:
                system_msg += f"\n\nTask: {task_title}"
            if language:
                system_msg += f"\nLanguage: {language}"
            if code:
                system_msg += f"\n\nCurrent code:\n```{language}\n{code}\n```"
            messages.append({"role": "system", "content": system_msg})

        messages.append({"role": "user", "content": text})

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=1024,
        )
        reply = (response.choices[0].message.content or "").strip()
        return {"reply": reply or "No response from AI."}
    except Exception as e:
        return {"reply": "Sorry, I couldn't process that. Error: " + str(e)[:200]}


@router.post("/api/chat/ai/voice")
async def chat_ai_voice(request: Request, audio: UploadFile = File(...)):
    """Accept voice recording: transcribe with Whisper, then get GPT-4o reply. Requires auth."""
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Accept any upload; Whisper handles format detection. Reject only if missing.

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"transcript": "", "reply": "AI voice chat is not configured. Please set OPENAI_API_KEY."}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        # Read uploaded file into bytes (Whisper accepts file-like or bytes)
        body = await audio.read()
        if len(body) < 100:
            return {"transcript": "", "reply": "Audio too short or empty. Please record again."}
        # Whisper expects a file with a known extension; pass as bytes with filename hint
        ext = "webm"  # browser MediaRecorder usually gives webm
        if audio.filename and "." in audio.filename:
            ext = audio.filename.rsplit(".", 1)[-1].lower()
        # openai SDK accepts a tuple (filename, file_content, content_type) for in-memory files
        import tempfile
        with tempfile.NamedTemporaryFile(suffix="." + ext, delete=False) as tmp:
            tmp.write(body)
            tmp.flush()
            tmp_path = tmp.name
        try:
            with open(tmp_path, "rb") as f:
                transcript_resp = client.audio.transcriptions.create(model="whisper-1", file=f)
            transcript = (transcript_resp.text or "").strip()
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        if not transcript:
            return {"transcript": "", "reply": "Could not understand the audio. Please try again or type your message."}
        # Get GPT-4o reply from transcript
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": transcript}],
            max_tokens=1024,
        )
        reply = (response.choices[0].message.content or "").strip()
        return {"transcript": transcript, "reply": reply or "No response from AI."}
    except Exception as e:
        return {"transcript": "", "reply": "Sorry, voice input failed. " + str(e)[:150]}
