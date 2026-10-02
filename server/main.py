import logging
from contextlib import asynccontextmanager
from http import HTTPStatus

import httpx
import redis.asyncio as redis
import uvicorn
from fastapi import Depends, FastAPI, Request, Response

from server.services.access_list import IgvProxyAccessList
from server.services.gcs_streamer import GCSStreamer
from server.services.proxy_credentials import ProxyCredentials
from server.services.rate_limit_store import RateLimitRedisClient
from server.services.rate_limiter import DownloadRateLimiter
from server.utils.connections import (
    create_redis_pool,
    get_access_list,
    get_httpx_client,
    get_proxy_credentials,
    get_redis_client,
    read_config_project_id,
)
from server.utils.constants import (
    FASTAPI_DEFAULT_CONFIGS,
    GCS_BASE_URL,
    HTTPX_CLIENT_TIMEOUT,
)
from server.utils.helpers import build_gcs_headers, get_headers
from server.utils.validation import (
    authorize_bucket_access,
    rate_limit_if_applicable,
    resolve_request_bytes,
    validate_and_parse_path,
    validate_auth,
)

logging.getLogger().setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Define application lifespan.

    Configuration is read and the access map is fetched before the app serves anything, and any
    failure here is left to propagate. Cloud Run then fails the revision and keeps the previous one
    serving, so a missing env var, an unreadable secret or a missing IAM binding becomes a failed
    deploy rather than a 401 or a 503 for every user.
    """
    config_project_id = read_config_project_id()
    _app.state.proxy_credentials = ProxyCredentials()

    _app.state.access_list = IgvProxyAccessList(project_id=config_project_id)
    await _app.state.access_list.load()

    _app.state.httpx_client = httpx.AsyncClient(timeout=HTTPX_CLIENT_TIMEOUT)
    _app.state.redis_client = RateLimitRedisClient(
        redis.Redis.from_pool(create_redis_pool()),
    )
    yield
    await _app.state.httpx_client.aclose()
    await _app.state.redis_client.aclose()
    await _app.state.access_list.aclose()


app = FastAPI(lifespan=lifespan)


@app.api_route('/health', methods=['GET'])
async def health_check(_request: Request):
    """Return health check response."""
    return Response(status_code=HTTPStatus.OK, content='OK. Server is healthy.')


@app.api_route('/{full_path:path}', methods=['HEAD'])
async def proxy_handler_metadata(  # noqa: PLR0913  # each argument is an injected dependency, not a knob
    request: Request,
    full_path: str,
    httpx_client: httpx.AsyncClient = Depends(get_httpx_client),
    redis_client: RateLimitRedisClient = Depends(get_redis_client),
    access_list: IgvProxyAccessList = Depends(get_access_list),
    proxy_credentials: ProxyCredentials = Depends(get_proxy_credentials),
):
    """Proxy metadata requests to GCS. Rate limit logics are bypassed: a HEAD transfers no bytes.

    It is still authorized. This request is served with the proxy's own credentials, so without an
    authorize hop a HEAD would leak the existence and size of any object the service account can
    reach — for every bucket, to anyone holding any Google token.

    The DownloadRateLimiter built here exists purely to call resolve_user(), which is where
    identity resolution lives. It is deliberately not handed to GCSStreamer: that would call
    record_download_stats with request_bytes=0, writing zero-valued dl_stats keys that the nightly
    CSV exporter would emit as empty rows.
    """
    bucket, object_path = validate_and_parse_path(full_path)
    headers = get_headers(request.headers)

    user_token = validate_auth(headers)

    rate_limiter = DownloadRateLimiter(
        redis_client=redis_client,
        http_client=httpx_client,
        user_token=user_token,
        request_bytes=0,
    )
    user_email = await rate_limiter.resolve_user()
    await authorize_bucket_access(access_list, user_email, bucket)

    gcs_headers = build_gcs_headers(headers, await proxy_credentials.get_token())

    target_url = httpx.URL(
        scheme='https',
        host=f'{bucket}.{GCS_BASE_URL}',
        path=f'/{object_path}',
    )

    streamer = GCSStreamer(httpx_client=httpx_client, rate_limiter=None)
    return await streamer.stream_from_gcs(
        method=request.method,
        target_url=target_url,
        headers=gcs_headers,
        query_params=request.query_params,
        bucket_name=bucket,
    )


@app.api_route('/{full_path:path}', methods=['GET'])
async def proxy_handler(  # noqa: PLR0913  # each argument is an injected dependency, not a knob
    request: Request,
    full_path: str,
    httpx_client: httpx.AsyncClient = Depends(get_httpx_client),
    redis_client: RateLimitRedisClient = Depends(get_redis_client),
    access_list: IgvProxyAccessList = Depends(get_access_list),
    proxy_credentials: ProxyCredentials = Depends(get_proxy_credentials),
):
    """Proxy requests to GCS.

    Resolve who is calling, authorize them against the access map, then meter. Authorizing before
    metering matters: a refused request must never consume the caller's download budget.
    """
    bucket, object_path = validate_and_parse_path(full_path)
    headers = get_headers(request.headers)

    user_token = validate_auth(headers)
    range_header = headers.get('Range')

    request_bytes = resolve_request_bytes(object_path, range_header)

    rate_limiter = DownloadRateLimiter(
        redis_client=redis_client,
        http_client=httpx_client,
        user_token=user_token,
        request_bytes=request_bytes if request_bytes is not None else 0,
    )
    user_email = await rate_limiter.resolve_user()
    await authorize_bucket_access(access_list, user_email, bucket)

    # Before metering, not after: everything between the deduction and stream_from_gcs's own
    # try/except is budget the caller would lose for an hour if it raised. The token is cached, so
    # a request that goes on to 429 costs nothing here.
    gcs_headers = build_gcs_headers(headers, await proxy_credentials.get_token())

    metered_rate_limiter = await rate_limit_if_applicable(rate_limiter, request_bytes)

    target_url = httpx.URL(
        scheme='https',
        host=f'{bucket}.{GCS_BASE_URL}',
        path=f'/{object_path}',
    )

    streamer = GCSStreamer(httpx_client=httpx_client, rate_limiter=metered_rate_limiter)
    return await streamer.stream_from_gcs(
        method=request.method,
        target_url=target_url,
        headers=gcs_headers,
        query_params=request.query_params,
        bucket_name=bucket,
    )


if __name__ == '__main__':
    logging.info('##### Started reverse proxying server #####')

    uvicorn.run(
        'server.main:app',
        host=FASTAPI_DEFAULT_CONFIGS['host'],
        port=FASTAPI_DEFAULT_CONFIGS['port'],
    )
