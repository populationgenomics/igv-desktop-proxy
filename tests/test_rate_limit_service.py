from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from server.services.rate_limit_service import DownloadRateLimiter
from server.utils.constants import CPG_HOSTED_DOMAIN


class TestDownloadRateLimiter:
    """Tests for DownloadRateLimiter."""

    @pytest.fixture(autouse=True)
    def set_up(self, mock_redis_client: MagicMock, mock_httpx_client: httpx.AsyncClient):
        """Set up the download rate limiter class."""
        self.rate_limiter = DownloadRateLimiter(
            redis_client=mock_redis_client,
            http_client=mock_httpx_client,
            user_token='fake_token',  # noqa: S106
            request_bytes=512_000,
        )
        self.redis_client = mock_redis_client

    @pytest.mark.asyncio
    async def test_check_user_limit_success(self):
        """Test check_user_limit returns True after successful user identification and with in the download limits."""
        with (
            patch.object(self.rate_limiter, 'get_authenticated_user_id', return_value='user123'),
            patch.object(self.rate_limiter, 'evaluate_download_limits', return_value=True),
        ):
            result = await self.rate_limiter.check_user_limit()
            assert result is True

    @pytest.mark.asyncio
    async def test_check_user_limit_unauthenticated(self):
        """Test check_user_limit returns False on failed user identification."""
        with patch.object(self.rate_limiter, 'get_authenticated_user_id', return_value=None):
            result = await self.rate_limiter.check_user_limit()
            assert result is False

    @pytest.mark.asyncio
    async def test_fetch_user_info_success(self):
        """Test fetch_user_info returns user sub and hd."""
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.json.return_value = {'sub': 'user123', 'hd': CPG_HOSTED_DOMAIN}

        with patch.object(self.rate_limiter.http_client, 'get', return_value=mock_response):
            user_info = await self.rate_limiter.fetch_user_info()
            assert user_info == {'sub': 'user123', 'hd': CPG_HOSTED_DOMAIN}

    @pytest.mark.asyncio
    async def test_fetch_user_info_failure(self):
        """Test fetch_user_info returns None on failure."""
        with patch.object(self.rate_limiter.http_client, 'get', side_effect=httpx.HTTPError('Error Requesting')):
            user_info = await self.rate_limiter.fetch_user_info()
            assert user_info is None

    @pytest.mark.asyncio
    async def test_get_authenticated_user_id_cached(self):
        """Test get_authenticated_user_id returns user sub if already cached for user access token."""
        self.redis_client.get.return_value = b'user123'

        user_id = await self.rate_limiter.get_authenticated_user_id('hash123')
        assert user_id == b'user123'

    @pytest.mark.asyncio
    async def test_evaluate_download_limits_success(self):
        """Test evaluate_download_limits returns true when user with in the download limits."""
        self.rate_limiter.user_sub = 'user123'

        remaining_bytes = 500000
        self.redis_client.deduct_if_balance.return_value = remaining_bytes
        result = await self.rate_limiter.evaluate_download_limits()
        assert result is True
        assert self.rate_limiter.remaining_bytes == remaining_bytes

    @pytest.mark.asyncio
    async def test_evaluate_download_limits_exceeded(self):
        """Test evaluate_download_limits returns False when user has exhausted the download limits."""
        self.rate_limiter.user_sub = 'user123'
        self.redis_client.deduct_if_balance.return_value = -1  # cap exceeded
        result = await self.rate_limiter.evaluate_download_limits()
        assert result is False

    @pytest.mark.asyncio
    async def test_get_authenticated_user_id_retry_logic(self) -> None:
        """Test get_authenticated_user_id retry to acquire the lock before invoking userinfo endpoint."""
        user_info = {'sub': 'user_after_retry', 'hd': CPG_HOSTED_DOMAIN}

        with (
            patch('asyncio.sleep', new_callable=AsyncMock),
            patch.object(self.rate_limiter, 'fetch_user_info', return_value=user_info),
        ):
            self.redis_client.get.return_value = None

            # 1st attempt: lock fails
            # 2nd attempt: lock fails
            # 3rd attempt: lock succeeds, then it caches the result (called again for caching)
            self.redis_client.set.side_effect = [False, False, True, True]
            user_id = await self.rate_limiter.get_authenticated_user_id('hash_retry')
            assert user_id == 'user_after_retry'
            assert self.redis_client.set.call_count == 4  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_get_authenticated_user_id_retry_gets_cached_value(self):
        """Test get_authenticated_user_id gets cached value on retry."""
        # 1st attempt: cache miss
        # 2nd attempt: cache hit
        self.redis_client.get.side_effect = [None, b'user_from_other_request']
        self.redis_client.set.return_value = False

        with patch('asyncio.sleep', new_callable=AsyncMock), patch.object(self.rate_limiter, 'fetch_user_info'):
            user_id = await self.rate_limiter.get_authenticated_user_id('hash_concurrent')

            assert user_id == b'user_from_other_request'
            assert self.redis_client.get.call_count == 2  # noqa: PLR2004
            # called 1 time (on the first attempt to acquire lock, second returns early from cache)
            assert self.redis_client.set.call_count == 1

    @pytest.mark.asyncio
    async def test_get_authenticated_user_id_retry_on_fetch_user_info_fails(self):
        """Test get_authenticated_user_id retry on fetch user info fails."""
        # TODO add test case
