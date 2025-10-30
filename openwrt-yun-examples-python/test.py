import asyncio
from yunbridge_client import Bridge

async def main():
    bridge = Bridge()
    await bridge.connect()
    print("Bridge connected")
    await bridge.disconnect()

if __name__ == "__main__":
    asyncio.run(main())