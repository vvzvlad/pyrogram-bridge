import os
import asyncio
from pyrogram import Client

async def generate_session():
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    
    if not api_id or not api_hash:
        print("Error: Set TG_API_ID and TG_API_HASH environment variables")
        return

    async with Client(
        name=":memory:",
        api_id=int(api_id),
        api_hash=api_hash,
        in_memory=True
    ) as client:
        session = await client.export_session_string()
        print("\nYour session string:")
        print("=" * 40)
        print(session)
        print("=" * 40)
        print("\nUser session on ENV variable TG_SESSION_STRING in docker-compose.yml")

if __name__ == "__main__":
    asyncio.run(generate_session()) 