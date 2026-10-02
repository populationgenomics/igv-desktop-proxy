import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from server.services.proxy_credentials import ProxyCredentials


class FakeCredentials:
    """A stand-in for google.auth credentials that records how it was refreshed."""

    def __init__(self, lifetime_secs: float = 3600) -> None:
        """Start with no token, as google.auth credentials do before their first refresh."""
        self.token: str | None = None
        self.expiry: datetime | None = None
        self.lifetime_secs = lifetime_secs
        self.refresh_count = 0
        self.refresh_threads: list[int] = []

    def refresh(self, _request: object) -> None:
        """Mint a new token, synchronously, exactly as google.auth's transport does."""
        self.refresh_count += 1
        self.refresh_threads.append(threading.get_ident())
        self.token = f'proxy-token-{self.refresh_count}'
        # google.auth stores a naive UTC datetime here.
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=self.lifetime_secs)


class TestProxyCredentials:
    """Tests for ProxyCredentials."""

    @pytest.mark.asyncio
    async def test_returns_a_freshly_minted_token(self):
        """Test the first call refreshes and returns the token."""
        credentials = FakeCredentials()
        proxy_credentials = ProxyCredentials(credentials=credentials)

        assert await proxy_credentials.get_token() == 'proxy-token-1'

    @pytest.mark.asyncio
    async def test_reuses_a_token_that_is_still_fresh(self):
        """Test a valid token is not re-minted on every request."""
        credentials = FakeCredentials()
        proxy_credentials = ProxyCredentials(credentials=credentials)

        await proxy_credentials.get_token()
        await proxy_credentials.get_token()

        assert credentials.refresh_count == 1

    @pytest.mark.asyncio
    async def test_refreshes_before_the_token_actually_expires(self):
        """Test a token inside the refresh margin is replaced rather than handed out about to die."""
        credentials = FakeCredentials(lifetime_secs=60)  # shorter than the 300s margin
        proxy_credentials = ProxyCredentials(credentials=credentials, refresh_margin_secs=300)

        assert await proxy_credentials.get_token() == 'proxy-token-1'
        assert await proxy_credentials.get_token() == 'proxy-token-2'

    @pytest.mark.asyncio
    async def test_refreshes_off_the_event_loop(self):
        """Test the blocking refresh runs in a worker thread, so it cannot stall concurrent streams."""
        credentials = FakeCredentials()
        proxy_credentials = ProxyCredentials(credentials=credentials)

        await proxy_credentials.get_token()

        assert credentials.refresh_threads == [credentials.refresh_threads[0]]
        assert credentials.refresh_threads[0] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_concurrent_callers_refresh_once(self):
        """Test every in-flight request noticing the same expiry does not mint a token each."""
        credentials = FakeCredentials()
        proxy_credentials = ProxyCredentials(credentials=credentials)

        tokens = await asyncio.gather(*(proxy_credentials.get_token() for _ in range(5)))

        assert credentials.refresh_count == 1
        assert set(tokens) == {'proxy-token-1'}

    @pytest.mark.asyncio
    async def test_raises_when_the_refresh_yields_no_token(self):
        """Test a silent credential failure surfaces rather than sending an empty Authorization header."""
        credentials = FakeCredentials()
        credentials.refresh = lambda _request: None  # type: ignore[method-assign]
        proxy_credentials = ProxyCredentials(credentials=credentials)

        with pytest.raises(RuntimeError):
            await proxy_credentials.get_token()
