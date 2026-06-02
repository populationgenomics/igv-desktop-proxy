from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import tenacity
from fastapi import HTTPException

from server.services.rate_limiter import DownloadRateLimiter


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
    async def test_check_user_limit_successfully(self):
        """Test check_user_limit returns True after successful user identification and within the download limits."""
        with (
            patch.object(self.rate_limiter, 'get_authenticated_user_id', return_value='user123'),
            patch.object(self.rate_limiter, 'evaluate_download_limits', return_value=True),
        ):
            result = await self.rate_limiter.check_user_limit()
            assert result is True

    @pytest.mark.asyncio
    async def test_check_user_limit_raise_error_on_user_identification_fails(self):
        """Test check_user_limit raise error on failed user identification."""
        with (
            patch.object(self.rate_limiter, 'get_authenticated_user_id', return_value=None),
            pytest.raises(HTTPException),
        ):
            await self.rate_limiter.check_user_limit()

    @pytest.mark.asyncio
    async def test_check_user_limit_raise_http_exception_on_retry_failure(self):
        """Test check_user_limit raises error get_authenticated_user_id exhausts retries."""
        with (
            patch.object(
                self.rate_limiter,
                'get_authenticated_user_id',
                side_effect=tenacity.RetryError(MagicMock()),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await self.rate_limiter.check_user_limit()
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_get_authenticated_user_id_cached(self):
        """Test get_authenticated_user_id returns user sub if already cached for user access token."""
        user_sub = 'user123'
        self.redis_client.get.return_value = 'user123'

        user_id = await self.rate_limiter.get_authenticated_user_id('hash123')
        assert user_id == user_sub

    @pytest.mark.asyncio
    async def test_get_authenticated_user_id_retry_logic(self) -> None:
        """Test get_authenticated_user_id retry  acquire the lock before invoking userinfo endpoint."""
        user_info = {'sub': 'user_after_retry'}

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
        self.redis_client.get.side_effect = [None, 'user_from_other_request']
        self.redis_client.set.return_value = False

        with patch('asyncio.sleep', new_callable=AsyncMock), patch.object(self.rate_limiter, 'fetch_user_info'):
            user_id = await self.rate_limiter.get_authenticated_user_id('hash_concurrent')

            assert user_id == 'user_from_other_request'
            assert self.redis_client.get.call_count == 2  # noqa: PLR2004
            # called 1 time (on the first attempt to acquire lock, second returns early from cache)
            assert self.redis_client.set.call_count == 1

    @pytest.mark.asyncio
    async def test_get_authenticated_user_id_raises_retry_error_on_exhaustion(self):
        """Test get_authenticated_user_id raises RetryError after all retry attempts return None."""
        self.redis_client.get.return_value = None  # no cache hit on any attempt
        self.redis_client.set.return_value = False  # lock never acquired → always returns None

        with patch('asyncio.sleep', new_callable=AsyncMock), pytest.raises(tenacity.RetryError):
            await self.rate_limiter.get_authenticated_user_id('hash_exhausted')

    @pytest.mark.asyncio
    async def test_fetch_user_info_success(self):
        """Test fetch_user_info returns user sub and hd."""
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.json.return_value = {'sub': 'user123'}

        with patch.object(self.rate_limiter.http_client, 'get', return_value=mock_response):
            user_info = await self.rate_limiter.fetch_user_info()
            assert user_info == {'sub': 'user123'}

    @pytest.mark.asyncio
    async def test_fetch_user_info_failure(self):
        """Test fetch_user_info returns None on failure."""
        with patch.object(self.rate_limiter.http_client, 'get', side_effect=httpx.HTTPError('Error Requesting')):
            user_info = await self.rate_limiter.fetch_user_info()
            assert user_info is None

    @pytest.mark.asyncio
    async def test_evaluate_download_limits_success(self):
        """Test evaluate_download_limits returns true when user with in the download limits."""
        self.rate_limiter.user_sub = 'user123'

        remaining_bytes = 500000
        self.redis_client.deduct_if_balance.return_value = remaining_bytes
        result = await self.rate_limiter.evaluate_download_limits()
        assert result is True

    @pytest.mark.asyncio
    async def test_evaluate_download_limits_exceeded(self):
        """Test evaluate_download_limits returns False when user has exhausted the download limits."""
        self.rate_limiter.user_sub = 'user123'
        self.redis_client.deduct_if_balance.return_value = -1  # cap exceeded
        result = await self.rate_limiter.evaluate_download_limits()
        assert result is False

    @pytest.mark.asyncio
    async def test_record_download_stats_calls_increment(self):
        """Test record_download_stats increments the correct Redis key."""
        self.rate_limiter.user_sub = 'user123'
        await self.rate_limiter.record_download_stats('test-bucket')
        self.redis_client.increment_download_stats.assert_called_once()
        call_args = self.redis_client.increment_download_stats.call_args
        assert call_args.kwargs['stats_key'].startswith('dl_stats:user123:test-bucket:')

    @pytest.mark.asyncio
    async def test_record_download_stats_skips_when_user_sub_is_none(self):
        """Test record_download_stats does nothing when user_sub is not set."""
        await self.rate_limiter.record_download_stats('test-bucket')
        self.redis_client.increment_download_stats.assert_not_called()

    @pytest.mark.asyncio
    async def test_refund_skips_when_user_sub_is_none(self):
        """Test refund does nothing when user_sub is not set."""
        # user_sub is None by default from the constructor
        await self.rate_limiter.refund()
        self.redis_client.refund.assert_not_called()

    @pytest.mark.asyncio
    async def test_refund_calls_redis_on_valid_user_sub(self):
        """Test refund returns bytes to the quota when user_sub is set."""
        self.rate_limiter.user_sub = 'user123'
        await self.rate_limiter.refund()
        self.redis_client.refund.assert_called_once()
