import asyncio
import json
import logging
import time
from collections.abc import Callable
from http import HTTPStatus

from fastapi import HTTPException
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from google.cloud.secretmanager_v1 import SecretManagerServiceAsyncClient
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential_jitter

from server.utils.constants import (
    ACCESS_LIST_LOAD_ATTEMPTS,
    ACCESS_LIST_TTL_SECS,
    IGV_PROXY_CONFIG_SECRET_ID,
)

# Refresh failures, survivable while a previous map is in hand; anything else is a bug.
# GoogleAuthError isn't a GoogleAPIError: it covers credential failures like a metadata-server blip.
REFRESH_ERRORS = (GoogleAPIError, GoogleAuthError, ValueError, OSError)


def normalise_email(email: str) -> str:
    """Return the form of an email used as an access map key.

    Both the parse and the lookup go through here: the secret derives from hand-written YAML, so
    `Alice@example.com` must not silently fail to match, and the two sides must never drift.
    """
    return email.strip().lower()


def parse_access_map(payload: bytes) -> dict[str, frozenset[str]]:
    """Parse the igv-proxy-config secret payload into an email -> buckets map.

    The payload shape is fixed by SET-1249: {"users": {"<email>": ["<bucket>", ...]}}.

    Raises ValueError on any shape that is not that, so a corrupt secret fails loudly rather than
    parsing to an empty map that looks exactly like "nobody is listed".
    """
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError('Access map payload is not a JSON object.')

    users = document.get('users')
    if not isinstance(users, dict):
        raise ValueError("Access map payload has no 'users' object.")

    parsed: dict[str, frozenset[str]] = {}
    for email, buckets in users.items():
        if not isinstance(buckets, list) or not all(isinstance(bucket, str) for bucket in buckets):
            raise ValueError('Access map entry does not hold a list of bucket names.')
        parsed[normalise_email(email)] = frozenset(buckets)

    return parsed


class IgvProxyAccessList:
    """The set of (user, bucket) pairs this proxy will serve, read from Secret Manager.

    Absent is modelled separately from empty. A map that has never loaded is not a map in which
    nobody is listed: the first refuses with 503, the second refuses with 403. Collapsing them
    would make a failed first fetch look exactly like correct enforcement.
    """

    def __init__(
        self,
        project_id: str,
        client: SecretManagerServiceAsyncClient | None = None,
        ttl_secs: int = ACCESS_LIST_TTL_SECS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the access list against the project holding the igv-proxy-config secret."""
        self._project_id = project_id
        self._client = client if client is not None else SecretManagerServiceAsyncClient()
        self._ttl_secs = ttl_secs
        self._clock = clock

        self._users: dict[str, frozenset[str]] | None = None
        self._expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    @property
    def secret_version_name(self) -> str:
        """Return the resource path of the secret version, as fixed by SET-1249."""
        return f'projects/{self._project_id}/secrets/{IGV_PROXY_CONFIG_SECRET_ID}/versions/latest'

    async def load(self) -> None:
        """Fetch the access map eagerly at startup, with a bounded retry.

        Raises if it never succeeds, so a missing secret or a missing IAM binding fails the Cloud
        Run revision — the previous one keeps serving — rather than 503ing every user.
        """
        retrying = AsyncRetrying(
            stop=stop_after_attempt(ACCESS_LIST_LOAD_ATTEMPTS),
            wait=wait_exponential_jitter(initial=1, max=8),
            reraise=True,
        )
        await retrying(self._refresh)
        logging.info(f'Loaded the IGV proxy access map from {self.secret_version_name}.')

    async def is_allowed(self, email: str, bucket: str) -> bool:
        """Return whether this user may read this bucket through the proxy.

        Raises 503 if no map has ever loaded — fail closed, and say so distinguishably.
        """
        await self._refresh_if_stale()

        if self._users is None:
            logging.error('No IGV proxy access map has ever loaded; refusing every request.')
            raise HTTPException(
                HTTPStatus.SERVICE_UNAVAILABLE,
                detail='Access configuration is unavailable. Please retry shortly.',
            )

        return bucket in self._users.get(normalise_email(email), frozenset())

    async def aclose(self) -> None:
        """Close the Secret Manager transport."""
        await self._client.transport.close()

    async def _refresh_if_stale(self) -> None:
        """Refetch the access map if its TTL has lapsed.

        On traffic, not a timer: Cloud Run scales to zero and doesn't guarantee CPU outside a request.
        """
        if self._clock() < self._expires_at:
            return

        async with self._refresh_lock:
            # A concurrent request may have refreshed while this one waited for the lock.
            if self._clock() < self._expires_at:
                return

            try:
                await self._refresh()
            except REFRESH_ERRORS as exc:
                if self._users is None:
                    logging.error(f'Failed to load the IGV proxy access map: {exc!r}')
                    return
                logging.error(f'Failed to refresh the IGV proxy access map, serving the last good copy: {exc!r}')

    async def _refresh(self) -> None:
        """Fetch and parse the secret, replacing the in-memory map."""
        response = await self._client.access_secret_version(name=self.secret_version_name)
        self._users = parse_access_map(response.payload.data)
        self._expires_at = self._clock() + self._ttl_secs
