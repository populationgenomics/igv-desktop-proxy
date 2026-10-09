import os

import httpx
import redis.asyncio as redis
from fastapi import Request

from server.services.access_list import IgvProxyAccessList
from server.services.proxy_credentials import ProxyCredentials
from server.services.rate_limit_store import RateLimitRedisClient
from server.utils.constants import (
    IGV_PROXY_CONFIG_PROJECT_ENV,
    REDIS_DEFAULT_CONFIGS,
)


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


def get_access_list(request: Request) -> IgvProxyAccessList:
    """Retrieve the shared IgvProxyAccessList instance."""
    if request.app.state.access_list is None:
        raise RuntimeError('Access list not initialized.')
    return request.app.state.access_list


def get_proxy_credentials(request: Request) -> ProxyCredentials:
    """Retrieve the shared ProxyCredentials instance."""
    if request.app.state.proxy_credentials is None:
        raise RuntimeError('Proxy credentials not initialized.')
    return request.app.state.proxy_credentials


def read_config_project_id() -> str:
    """Return the GCP project holding the igv-proxy-config secret. Required, not inferred from ADC."""
    project_id = os.environ.get(IGV_PROXY_CONFIG_PROJECT_ENV, '').strip()
    if not project_id:
        raise RuntimeError(f'{IGV_PROXY_CONFIG_PROJECT_ENV} must be set to the project holding igv-proxy-config.')
    return project_id


def create_redis_pool() -> redis.ConnectionPool:
    """Create a Redis connection pool with retry logic.

    Redis pool is created to handle a single redis instance:
    https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html
    """
    redis_host = os.environ.get('REDIS_HOST', REDIS_DEFAULT_CONFIGS['host'])
    env_port = os.environ.get('REDIS_PORT')
    redis_port = int(env_port) if env_port else REDIS_DEFAULT_CONFIGS['port']
    redis_password = os.environ.get('REDIS_PASSWORD')
    redis_cert_path = os.environ.get('REDIS_CERT_PATH')

    scheme = 'rediss' if redis_cert_path else 'redis'
    if redis_password:
        url = f'{scheme}://:{redis_password}@{redis_host}:{redis_port}/0'
    else:
        url = f'{scheme}://{redis_host}:{redis_port}/0'

    kwargs = {
        'url': url,
        'max_connections': 20,
        'decode_responses': True,
        'timeout': 5,
    }

    if redis_cert_path:
        kwargs['ssl_ca_certs'] = redis_cert_path

    return redis.BlockingConnectionPool.from_url(**kwargs)
