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
from server.services.user_identity import UserIdentityResolver
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
    apply_rate_limit,
    authorize_bucket_access,
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
    """Define application lifespan. Config or access map failures propagate, failing the deploy."""
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


class ProxyClients:
    """The shared clients every proxied request needs, injected by FastAPI."""

    def __init__(
        self,
        httpx_client: httpx.AsyncClient = Depends(get_httpx_client),
        redis_client: RateLimitRedisClient = Depends(get_redis_client),
        access_list: IgvProxyAccessList = Depends(get_access_list),
        proxy_credentials: ProxyCredentials = Depends(get_proxy_credentials),
    ) -> None:
        """Hold the injected clients."""
        self.httpx_client = httpx_client
        self.redis_client = redis_client
        self.access_list = access_list
        self.proxy_credentials = proxy_credentials

    async def authorize(self, user_token: str, bucket: str, headers: dict) -> tuple[dict[str, str], dict[str, str]]:
        """Refuse the caller unless they may read this bucket; return their identity and the GCS headers."""
        identity = await UserIdentityResolver(
            redis_client=self.redis_client,
            http_client=self.httpx_client,
            user_token=user_token,
        ).resolve()
        await authorize_bucket_access(self.access_list, identity['email'], bucket)

        gcs_headers = build_gcs_headers(headers, await self.proxy_credentials.get_token())
        return identity, gcs_headers


@app.api_route('/{full_path:path}', methods=['HEAD'])
async def proxy_handler_metadata(request: Request, full_path: str, clients: ProxyClients = Depends()):
    """Proxy metadata requests to GCS. Unmetered, but still authorized so a HEAD can't probe objects."""
    bucket, object_path = validate_and_parse_path(full_path)
    headers = get_headers(request.headers)

    user_token = validate_auth(headers)
    _, gcs_headers = await clients.authorize(user_token, bucket, headers)

    target_url = httpx.URL(
        scheme='https',
        host=f'{bucket}.{GCS_BASE_URL}',
        path=f'/{object_path}',
    )

    streamer = GCSStreamer(httpx_client=clients.httpx_client, rate_limiter=None)
    return await streamer.stream_from_gcs(
        method=request.method,
        target_url=target_url,
        headers=gcs_headers,
        query_params=request.query_params,
        bucket_name=bucket,
    )


@app.api_route('/{full_path:path}', methods=['GET'])
async def proxy_handler(request: Request, full_path: str, clients: ProxyClients = Depends()):
    """Proxy requests to GCS. Authorize before metering, so a refused request costs no budget."""
    bucket, object_path = validate_and_parse_path(full_path)
    headers = get_headers(request.headers)

    user_token = validate_auth(headers)
    range_header = headers.get('Range')

    request_bytes = resolve_request_bytes(object_path, range_header)

    # Fetches the GCS token before metering: a raise after the deduction would cost the caller budget
    identity, gcs_headers = await clients.authorize(user_token, bucket, headers)

    rate_limiter = DownloadRateLimiter(
        redis_client=clients.redis_client,
        user_sub=identity['sub'],
        request_bytes=request_bytes if request_bytes is not None else 0,
    )
    is_metered = await apply_rate_limit(rate_limiter, request_bytes)

    target_url = httpx.URL(
        scheme='https',
        host=f'{bucket}.{GCS_BASE_URL}',
        path=f'/{object_path}',
    )

    streamer = GCSStreamer(httpx_client=clients.httpx_client, rate_limiter=rate_limiter if is_metered else None)
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
