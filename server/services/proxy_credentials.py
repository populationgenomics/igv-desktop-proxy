import asyncio
from datetime import UTC, datetime, timedelta

import anyio.to_thread
import google.auth
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

from server.utils.constants import GCS_READ_SCOPE, PROXY_TOKEN_REFRESH_MARGIN_SECS


class ProxyCredentials:
    """Mints and caches the access token the proxy uses to read GCS on a user's behalf.

    The narrow scope only binds local ADC/key-file runs; on the metadata server, instance scopes govern.
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        refresh_margin_secs: int = PROXY_TOKEN_REFRESH_MARGIN_SECS,
    ) -> None:
        """Initialize from the given credentials, or from application default credentials."""
        if credentials is None:
            credentials, _ = google.auth.default(scopes=[GCS_READ_SCOPE])

        self._credentials = credentials
        self._refresh_margin_secs = refresh_margin_secs
        self._token: str | None = None
        self._expiry: datetime | None = None
        self._refresh_lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Return a valid access token for the proxy's own service account."""
        token = self._fresh_token()
        if token is not None:
            return token

        async with self._refresh_lock:
            # Tokens expire at a single instant, so every concurrent request notices together.
            # Whoever holds the lock does the work; the rest find a fresh token waiting.
            token = self._fresh_token()
            if token is not None:
                return token

            # google-auth's transport is synchronous; refreshing on the event loop would stall
            # every stream this instance is serving.
            await anyio.to_thread.run_sync(self._refresh)

            self._token = self._credentials.token
            self._expiry = _as_utc(self._credentials.expiry)

            if self._token is None:
                raise RuntimeError('Credential refresh produced no access token for the proxy service account.')

            return self._token

    def _refresh(self) -> None:
        """Refresh the underlying credentials. Blocking; always called in a worker thread."""
        self._credentials.refresh(Request())

    def _fresh_token(self) -> str | None:
        """Return the cached token if it is still usable with the refresh margin to spare."""
        if self._token is None or self._expiry is None:
            return None
        if datetime.now(UTC) >= self._expiry - timedelta(seconds=self._refresh_margin_secs):
            return None
        return self._token


def _as_utc(expiry: datetime | None) -> datetime | None:
    """Return the expiry as an aware UTC datetime. google-auth stores it naive."""
    if expiry is None:
        return None
    if expiry.tzinfo is None:
        return expiry.replace(tzinfo=UTC)
    return expiry.astimezone(UTC)
