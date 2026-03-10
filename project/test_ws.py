import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8000/api/run_python_ws"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({"code": "print('hello world')"}))
        while True:
            try:
                message = await websocket.recv()
                print(f"Received: {message}")
                data = json.loads(message)
                if data.get("type") in ["exit", "error", "timeout"]:
                    break
            except Exception as e:
                print(f"Error receiving: {e}")
                break

asyncio.run(test_ws())
