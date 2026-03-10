import asyncio
import sys
import anyio

async def main():
    try:
        async with await anyio.open_process(
            [sys.executable, "-c", "print('hello world')"],
            stdin=anyio.subprocess.PIPE,
            stdout=anyio.subprocess.PIPE,
            stderr=anyio.subprocess.PIPE
        ) as process:
            stdout = await process.stdout.receive()
            print(f"Stdout: {stdout}")
    except Exception as e:
        print(f"Error: {e}")

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

asyncio.run(main())
