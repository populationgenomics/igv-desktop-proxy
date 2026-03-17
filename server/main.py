import logging
from contextlib import asynccontextmanager
from http import HTTPStatus

import httpx
import redis.asyncio as redis
import uvicorn
from fastapi import Depends, FastAPI, Request, Response

from server.services.connection_handler import create_redis_pool, get_httpx_client, get_redis_client
from server.services.gcs_service import GCSStreamer
from server.services.rate_limit_service import DownloadRateLimiter
from server.services.redis_rate_limit_service import RateLimitRedisClient
from server.utils.constants import (
    AUTH_HEADER_PARTS,
    CRAM_INDEX_FILE_EXTENSION,
    FASTAPI_DEFAULT_CONFIGS,
    FIRST_BYTE_RANGE,
    GCS_BASE_URL,
    HTTPX_CLIENT_TIMEOUT,
    REQUEST_URL_PARTS,
)
from server.utils.util import get_byte_range, get_headers

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
    await _app.state.redis_client.aclose()  # TODO what happens to the lua scripts on connection close


app = FastAPI(lifespan=lifespan)


@app.api_route('/health', methods=['GET'])
async def health_check(_request: Request):
    """Return health check response."""
    return Response(status_code=200, content='OK')


@app.api_route('/{full_path:path}', methods=['GET'])
async def proxy_handler(
    request: Request,
    full_path: str,
    httpx_client: httpx.AsyncClient = Depends(get_httpx_client),
    redis_client: RateLimitRedisClient = Depends(get_redis_client),
):
    """Proxy requests to GCS."""
    path_segments = full_path.split('/', 1)
    if len(path_segments) < REQUEST_URL_PARTS:
        return Response(status_code=HTTPStatus.BAD_REQUEST.value, content='Invalid path format. Use /bucket/path')

    headers = get_headers(request.headers)
    auth_header = headers.get('Authorization')
    range_header = headers.get('Range')

    if auth_header is None:
        return Response(status_code=HTTPStatus.BAD_REQUEST.value, content=HTTPStatus.BAD_REQUEST.description)

    user_token = auth_header.split(' ')
    if len(user_token) != AUTH_HEADER_PARTS:
        return Response(status_code=HTTPStatus.BAD_REQUEST.value, content=HTTPStatus.BAD_REQUEST.description)

    bucket = path_segments[0]
    object_path = path_segments[1]

    is_index_file = object_path.endswith(CRAM_INDEX_FILE_EXTENSION)
    rate_limiter = None

    if not is_index_file:
        if range_header is None:
            return Response(status_code=HTTPStatus.BAD_REQUEST.value, content=HTTPStatus.BAD_REQUEST.description)

        # For every zoom in we get two requests - one for the 'bytes=0-511999' and other for the actual range
        if range_header != FIRST_BYTE_RANGE:
            request_bytes = get_byte_range(range_header)

            # TODO save consumption per bucket
            rate_limiter = DownloadRateLimiter(
                redis_client=redis_client,
                http_client=httpx_client,
                user_token=user_token[1].strip(),
                request_bytes=request_bytes,
            )

            is_allowed = await rate_limiter.check_user_limit()
            if not is_allowed:
                return Response(
                    status_code=HTTPStatus.TOO_MANY_REQUESTS.value,
                    content=HTTPStatus.TOO_MANY_REQUESTS.description,
                )

    streamer = GCSStreamer(httpx_client=httpx_client)
    target_url = httpx.URL(scheme='https', host=f'{bucket}.{GCS_BASE_URL}', path=f'/{object_path}')
    gcs_response = await streamer.stream_from_gcs(
        method=request.method,
        target_url=target_url,
        headers=headers,
        query_params=request.query_params,
    )

    if rate_limiter and gcs_response.status_code >= HTTPStatus.BAD_REQUEST.value:
        await rate_limiter.refund()  # TODO can we handle this as a background task in streaming response

    return gcs_response


if __name__ == '__main__':
    logging.info('##### Started reverse proxying server #####')

    uvicorn.run(
        'server.main:app',
        host=FASTAPI_DEFAULT_CONFIGS.get('host'),
        port=FASTAPI_DEFAULT_CONFIGS.get('port'),
    )
