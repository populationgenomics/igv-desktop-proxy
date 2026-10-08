from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from server.main import app
from server.services.access_list import IgvProxyAccessList
from server.services.proxy_credentials import ProxyCredentials
from server.services.rate_limit_store import RateLimitRedisClient
from server.services.rate_limiter import DownloadRateLimiter
from server.utils.connections import (
    get_access_list,
    get_httpx_client,
    get_proxy_credentials,
    get_redis_client,
)
from tests.helpers import PROXY_TOKEN, ProxyTransport

CONFIG_PROJECT = 'cpg-igv-proxy-test'


@pytest.fixture
def proxy_transport() -> ProxyTransport:
    """Return the mock transport backing the proxy's httpx client."""
    return ProxyTransport()


@pytest.fixture
def mock_redis_client() -> MagicMock:
    """Return a MagicMock that mimics RateLimitRedisClient."""
    redis_client = MagicMock(spec=RateLimitRedisClient)

    redis_client.get = AsyncMock(return_value=None)
    redis_client.set = AsyncMock(return_value=True)
    redis_client.deduct_if_balance = AsyncMock(return_value=512_000)
    redis_client.refund = AsyncMock(return_value=1)
    redis_client.release_lock = AsyncMock(return_value=1)
    redis_client.increment_download_stats = AsyncMock(return_value=None)
    redis_client.aclose = AsyncMock(return_value=None)
    return redis_client


@pytest_asyncio.fixture
async def mock_httpx_client(proxy_transport: ProxyTransport) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Return httpx.AsyncClient with a mock transport."""
    client = httpx.AsyncClient(transport=proxy_transport)
    yield client
    await client.aclose()


@pytest.fixture
def mock_access_list() -> MagicMock:
    """Return a MagicMock that mimics IgvProxyAccessList, allowing everything by default."""
    access_list = MagicMock(spec=IgvProxyAccessList)
    access_list.load = AsyncMock(return_value=None)
    access_list.is_allowed = AsyncMock(return_value=True)
    access_list.aclose = AsyncMock(return_value=None)
    return access_list


@pytest.fixture
def mock_proxy_credentials() -> MagicMock:
    """Return a MagicMock that mimics ProxyCredentials."""
    proxy_credentials = MagicMock(spec=ProxyCredentials)
    proxy_credentials.get_token = AsyncMock(return_value=PROXY_TOKEN)
    return proxy_credentials


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the deployment configuration the app requires at startup."""
    monkeypatch.setenv('IGV_PROXY_CONFIG_PROJECT', CONFIG_PROJECT)


@pytest.fixture
def mock_proxy_api(
    proxy_env: None,  # noqa: ARG001  # applied for its side effect: the app's required env vars
    mock_redis_client: MagicMock,
    mock_httpx_client: httpx.AsyncClient,
    mock_access_list: MagicMock,
    mock_proxy_credentials: MagicMock,
) -> Generator[TestClient, None, None]:
    """Return a starlette TestClient with mock values.

    Services are patched at construction too: lifespan builds them before overrides apply, and
    the real ones would reach for ADC and Secret Manager.
    """
    app.dependency_overrides[get_redis_client] = lambda: mock_redis_client
    app.dependency_overrides[get_httpx_client] = lambda: mock_httpx_client
    app.dependency_overrides[get_access_list] = lambda: mock_access_list
    app.dependency_overrides[get_proxy_credentials] = lambda: mock_proxy_credentials

    with (
        patch('server.main.IgvProxyAccessList', return_value=mock_access_list),
        patch('server.main.ProxyCredentials', return_value=mock_proxy_credentials),
        TestClient(app) as client,
    ):
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
def mock_download_rate_limiter() -> MagicMock:
    """Return a MagicMock that mimics DownloadRateLimiter."""
    limiter = MagicMock(spec=DownloadRateLimiter)
    limiter.refund = AsyncMock(return_value=None)
    limiter.record_download_stats = AsyncMock(return_value=None)
    return limiter
