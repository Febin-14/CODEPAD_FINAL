import asyncio
import sys
import subprocess
import threading

async def main():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    loop = asyncio.get_running_loop()
    
    process = subprocess.Popen(
        [sys.executable, "-c", "print('hello world')"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    
    async def stream_output(stream, name):
        while True:
            chunk = await loop.run_in_executor(None, stream.read, 4096)
            if not chunk:
                break
            print(f"{name}: {chunk.decode()}")

    await asyncio.gather(
        stream_output(process.stdout, "stdout"),
        stream_output(process.stderr, "stderr")
    )
    process.wait()

asyncio.run(main())
