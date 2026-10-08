import time
from datetime import UTC, datetime

from server.services.rate_limit_store import RateLimitRedisClient
from server.utils.constants import STATS_PREFIX, SUB_PREFIX


class DownloadRateLimiter:
    """Implements fixed-window download rate limiting for an already-identified user."""

    def __init__(
        self,
        redis_client: RateLimitRedisClient,
        user_sub: str,
        request_bytes: int,
    ) -> None:
        """Initialize rate limiter client and request specifics."""
        self.redis_client = redis_client

        self.user_sub = user_sub
        self.request_bytes = request_bytes  # bytes; IGV typically requests 512 KB per request (CRAM, BAM)

    async def evaluate_download_limits(self) -> bool:
        """Deduct the requested byte size from the user's download budget if quota not exceeded."""
        now = time.time()

        sub_key = f'{SUB_PREFIX}:{self.user_sub}'
        remaining_bytes = await self.redis_client.deduct_if_balance(sub_key, self.request_bytes, now)

        return remaining_bytes >= 0

    async def record_download_stats(self, bucket: str) -> None:
        """Record successfully streamed bytes into the daily download stats."""
        date_str = datetime.now(UTC).strftime('%Y-%m-%d')
        stats_key = f'{STATS_PREFIX}:{self.user_sub}:{bucket}:{date_str}'
        await self.redis_client.increment_download_stats(stats_key=stats_key, bytes_count=self.request_bytes)

    async def refund(self) -> None:
        """Return the reserved bytes to the download budget after a failed GCS request."""
        now = time.time()
        key = f'{SUB_PREFIX}:{self.user_sub}'
        await self.redis_client.refund(key, self.request_bytes, now)
