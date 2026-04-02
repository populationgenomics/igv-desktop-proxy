from collections.abc import AsyncGenerator, Generator
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from server.main import app
from server.services.rate_limit_store import RateLimitRedisClient
from server.services.rate_limiter import DownloadRateLimiter
from server.utils.connections import get_httpx_client, get_redis_client


@pytest.fixture
def mock_redis_client() -> MagicMock:
    """Return a MagicMock that mimics RateLimitRedisClient."""
    redis_client = MagicMock(spec=RateLimitRedisClient)

    redis_client.get = AsyncMock(return_value=None)
    redis_client.set = AsyncMock(return_value=True)
    redis_client.deduct = AsyncMock(return_value=512_000)
    redis_client.refund = AsyncMock(return_value=1)
    redis_client.release_lock = AsyncMock(return_value=1)
    redis_client.aclose = AsyncMock(return_value=None)
    return redis_client


@pytest_asyncio.fixture
async def mock_httpx_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Return httpx.AsyncClient with a mock transport."""
    transport = httpx.MockTransport(handler=lambda _request: httpx.Response(HTTPStatus.OK.value))
    client = httpx.AsyncClient(transport=transport)
    yield client
    await client.aclose()


@pytest.fixture
def mock_proxy_api(
    mock_redis_client: MagicMock,
    mock_httpx_client: httpx.AsyncClient,
) -> Generator[TestClient, None, None]:
    """Return a starlette TestClient with mock values."""
    app.dependency_overrides[get_redis_client] = lambda: mock_redis_client
    app.dependency_overrides[get_httpx_client] = lambda: mock_httpx_client
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def mock_download_rate_limiter() -> MagicMock:
    """Return a MagicMock that mimics DownloadRateLimiter."""
    return MagicMock(spec=DownloadRateLimiter)
