import logging
from contextlib import asynccontextmanager
from http import HTTPStatus

import httpx
import redis.asyncio as redis
import uvicorn
from fastapi import Depends, FastAPI, Request, Response

from server.services.gcs_streamer import GCSStreamer
from server.services.rate_limit_store import RateLimitRedisClient
from server.utils.connections import create_redis_pool, get_httpx_client, get_redis_client
from server.utils.constants import (
    FASTAPI_DEFAULT_CONFIGS,
    GCS_BASE_URL,
    HTTPX_CLIENT_TIMEOUT,
)
from server.utils.helpers import get_headers
from server.utils.validation import rate_limit_if_applicable, validate_and_parse_path, validate_auth

logging.getLogger().setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Define application lifespan."""
    _app.state.httpx_client = httpx.AsyncClient(timeout=HTTPX_CLIENT_TIMEOUT)
    _app.state.redis_client = RateLimitRedisClient(
        redis.Redis.from_pool(create_redis_pool()),
    )
    yield
    await _app.state.httpx_client.aclose()
    await _app.state.redis_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.api_route('/health', methods=['GET'])
async def health_check(_request: Request):
    """Return health check response."""
    return Response(status_code=HTTPStatus.OK, content='OK. Server is healthy.')


@app.api_route('/{full_path:path}', methods=['HEAD'])
async def proxy_handler_metadata(
    request: Request,
    full_path: str,
    httpx_client: httpx.AsyncClient = Depends(get_httpx_client),
):
    """Proxy requests to GCS. Rate limit logics are not applied."""
    bucket, object_path = validate_and_parse_path(full_path)
    headers = get_headers(request.headers)

    validate_auth(headers)

    target_url = httpx.URL(
        scheme='https',
        host=f'{bucket}.{GCS_BASE_URL}',
        path=f'/{object_path}',
    )

    streamer = GCSStreamer(httpx_client=httpx_client, rate_limiter=None)
    return await streamer.stream_from_gcs(
        method=request.method,
        target_url=target_url,
        headers=headers,
        query_params=request.query_params,
        bucket_name=bucket,
    )


@app.api_route('/{full_path:path}', methods=['GET'])
async def proxy_handler(
    request: Request,
    full_path: str,
    httpx_client: httpx.AsyncClient = Depends(get_httpx_client),
    redis_client: RateLimitRedisClient = Depends(get_redis_client),
):
    """Proxy requests to GCS."""
    bucket, object_path = validate_and_parse_path(full_path)
    headers = get_headers(request.headers)

    user_token = validate_auth(headers)
    range_header = headers.get('Range')

    rate_limiter = await rate_limit_if_applicable(
        object_path=object_path,
        range_header=range_header,
        user_token=user_token,
        httpx_client=httpx_client,
        redis_client=redis_client,
    )

    target_url = httpx.URL(
        scheme='https',
        host=f'{bucket}.{GCS_BASE_URL}',
        path=f'/{object_path}',
    )

    streamer = GCSStreamer(httpx_client=httpx_client, rate_limiter=rate_limiter)
    return await streamer.stream_from_gcs(
        method=request.method,
        target_url=target_url,
        headers=headers,
        query_params=request.query_params,
        bucket_name=bucket,
    )


if __name__ == '__main__':
    logging.info('##### Started reverse proxying server #####')

    uvicorn.run(
        'server.main:app',
        host=FASTAPI_DEFAULT_CONFIGS.get('host'),
        port=FASTAPI_DEFAULT_CONFIGS.get('port'),
    )
