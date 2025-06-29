# sse_bus.py
import asyncio, json, redis.asyncio as redis
from core.config import settings

redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
CHANNEL = "sse-broadcast"

async def publish(event: dict):
    await redis_client.publish(CHANNEL, json.dumps(event))

async def listen():
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            yield json.loads(message["data"])
