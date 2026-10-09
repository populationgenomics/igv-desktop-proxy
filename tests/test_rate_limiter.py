from unittest.mock import MagicMock

import pytest

from server.services.rate_limiter import DownloadRateLimiter


class TestDownloadRateLimiter:
    """Tests for DownloadRateLimiter."""

    @pytest.fixture(autouse=True)
    def set_up(self, mock_redis_client: MagicMock):
        """Set up the download rate limiter class."""
        self.rate_limiter = DownloadRateLimiter(
            redis_client=mock_redis_client,
            user_email='alice@example.com',
            request_bytes=512_000,
        )
        self.redis_client = mock_redis_client

    @pytest.mark.asyncio
    async def test_evaluate_download_limits_success(self):
        """Test evaluate_download_limits returns true when user with in the download limits."""
        remaining_bytes = 500000
        self.redis_client.deduct_if_balance.return_value = remaining_bytes
        result = await self.rate_limiter.evaluate_download_limits()
        assert result is True

    @pytest.mark.asyncio
    async def test_evaluate_download_limits_exceeded(self):
        """Test evaluate_download_limits returns False when user has exhausted the download limits."""
        self.redis_client.deduct_if_balance.return_value = -1  # cap exceeded
        result = await self.rate_limiter.evaluate_download_limits()
        assert result is False

    @pytest.mark.asyncio
    async def test_record_download_stats_calls_increment(self):
        """Test record_download_stats increments the correct Redis key."""
        await self.rate_limiter.record_download_stats('test-bucket')
        self.redis_client.increment_download_stats.assert_called_once()
        call_args = self.redis_client.increment_download_stats.call_args
        assert call_args.kwargs['stats_key'].startswith('dl_stats:alice@example.com:test-bucket:')

    @pytest.mark.asyncio
    async def test_refund_calls_redis(self):
        """Test refund returns bytes to the user's quota."""
        await self.rate_limiter.refund()
        self.redis_client.refund.assert_called_once()
