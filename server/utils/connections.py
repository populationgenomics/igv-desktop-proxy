import os

import httpx
import redis.asyncio as redis
from fastapi import Request

from server.services.rate_limit_store import RateLimitRedisClient
from server.utils.constants import REDIS_DEFAULT_CONFIGS


def get_httpx_client(request: Request) -> httpx.AsyncClient:
    """Retrieve the shared httpx.AsyncClient instance."""
    if request.app.state.httpx_client is None:
        raise RuntimeError('Httpx client not initialized.')
    return request.app.state.httpx_client


def get_redis_client(request: Request) -> RateLimitRedisClient:
    """Retrieve the shared RateLimitRedisClient instance."""
    if request.app.state.redis_client is None:
        raise RuntimeError('Redis client not initialized.')
    return request.app.state.redis_client


def create_redis_pool() -> redis.ConnectionPool:
    """Create a Redis connection pool with retry logic.

    Redis pool is created to handle a single redis instance:
    https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html
    """
    redis_host = os.environ.get('REDIS_HOST', REDIS_DEFAULT_CONFIGS.get('host'))
    env_port = os.environ.get('REDIS_PORT')
    redis_port = int(env_port) if env_port else REDIS_DEFAULT_CONFIGS.get('port')

    url = f'redis://{redis_host}:{redis_port}/0'

    return redis.BlockingConnectionPool.from_url(url=url, max_connections=20, decode_responses=True, timeout=5)
