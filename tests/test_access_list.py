import asyncio
import json
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import PermissionDenied
from google.auth.exceptions import RefreshError

from server.services.access_list import IgvProxyAccessList, parse_access_map

PROJECT = 'cpg-igv-proxy-test'


def secret_payload(users: dict[str, list[str]]) -> SimpleNamespace:
    """Return a stand-in for an AccessSecretVersionResponse carrying the given access map."""
    return SimpleNamespace(payload=SimpleNamespace(data=json.dumps({'users': users}).encode('utf-8')))


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, value: float = 1000.0) -> None:
        """Start the clock at the given reading."""
        self.value = value

    def __call__(self) -> float:
        """Return the current reading."""
        return self.value


def mock_secret_client(*responses: object) -> AsyncMock:
    """Return a Secret Manager async client stub yielding the given responses (or side effects) in order."""
    client = AsyncMock()
    client.access_secret_version = AsyncMock(side_effect=list(responses))
    return client


class TestParseAccessMap:
    """Tests for parsing the igv-proxy-config secret payload."""

    def test_lower_cases_emails_and_freezes_buckets(self):
        """Test emails are normalised to lower case and bucket lists become sets."""
        parsed = parse_access_map(secret_payload({'Alice@Example.COM': ['cpg-fewgenomes-main']}).payload.data)
        assert parsed == {'alice@example.com': frozenset({'cpg-fewgenomes-main'})}

    def test_empty_users_object_parses_to_empty_map(self):
        """Test a loaded-but-empty map is a valid parse, distinct from never having loaded."""
        assert parse_access_map(secret_payload({}).payload.data) == {}

    @pytest.mark.parametrize(
        'payload',
        [
            b'not json at all',
            b'[]',
            b'{}',  # no 'users' key
            b'{"users": []}',
            b'{"users": {"alice@example.com": "cpg-fewgenomes-main"}}',  # buckets not a list
            b'{"users": {"alice@example.com": [1, 2]}}',  # buckets not strings
        ],
    )
    def test_rejects_malformed_payloads(self, payload: bytes):
        """Test a malformed secret payload raises rather than silently yielding an empty map."""
        with pytest.raises(ValueError):  # noqa: PT011
            parse_access_map(payload)


class TestIgvProxyAccessList:
    """Tests for IgvProxyAccessList."""

    @pytest.mark.asyncio
    async def test_allows_listed_user_and_bucket(self):
        """Test a listed user is allowed on a bucket they are listed against."""
        client = mock_secret_client(secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)
        await access_list.load()

        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is True

    @pytest.mark.asyncio
    async def test_matches_email_case_insensitively(self):
        """Test the incoming claim is lower-cased before lookup, mirroring the parse."""
        client = mock_secret_client(secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)
        await access_list.load()

        assert await access_list.is_allowed('Alice@Example.com', 'cpg-fewgenomes-main') is True

    @pytest.mark.asyncio
    async def test_refuses_listed_user_on_unlisted_bucket(self):
        """Test a listed user is refused a bucket that is not in their list."""
        client = mock_secret_client(secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)
        await access_list.load()

        assert await access_list.is_allowed('alice@example.com', 'cpg-thousand-genomes-main') is False

    @pytest.mark.asyncio
    async def test_refuses_unlisted_user(self):
        """Test a user absent from the map is refused."""
        client = mock_secret_client(secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)
        await access_list.load()

        assert await access_list.is_allowed('mallory@example.com', 'cpg-fewgenomes-main') is False

    @pytest.mark.asyncio
    async def test_loaded_but_empty_map_refuses_rather_than_503s(self):
        """Test an empty map is enforcement, not unavailability."""
        client = mock_secret_client(secret_payload({}))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)
        await access_list.load()

        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is False

    @pytest.mark.asyncio
    async def test_never_loaded_raises_503_not_403(self):
        """Test an absent map fails closed as unavailable, so it is never mistaken for enforcement."""
        client = mock_secret_client(PermissionDenied('no access'))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)

        with pytest.raises(HTTPException) as exc:
            await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main')
        assert exc.value.status_code == HTTPStatus.SERVICE_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_reads_within_ttl_do_not_refetch(self):
        """Test the map is cached for its TTL rather than fetched per request."""
        clock = FakeClock()
        client = mock_secret_client(secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client, ttl_secs=300, clock=clock)
        await access_list.load()
        clock.value += 299

        await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main')
        await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main')

        assert client.access_secret_version.await_count == 1

    @pytest.mark.asyncio
    async def test_expired_ttl_refetches_and_picks_up_revocation(self):
        """Test a read after the TTL lapses refreshes the map, so revocation takes effect."""
        client = mock_secret_client(
            secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}),
            secret_payload({}),
        )
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client, ttl_secs=0)

        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is True
        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is False

    @pytest.mark.asyncio
    async def test_refresh_failure_serves_the_last_good_map(self):
        """Test a Secret Manager blip does not take IGV down while a map is in hand."""
        client = mock_secret_client(
            secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}),
            PermissionDenied('transient'),
        )
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client, ttl_secs=0)

        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is True
        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is True

    @pytest.mark.asyncio
    async def test_credential_failure_on_refresh_serves_the_last_good_map(self):
        """Test a metadata-server blip is a refresh failure, not a 500.

        GoogleAuthError isn't a GoogleAPIError, so it must be caught explicitly.
        """
        client = mock_secret_client(
            secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}),
            RefreshError('metadata server unavailable'),
        )
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client, ttl_secs=0)

        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is True
        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is True

    @pytest.mark.asyncio
    async def test_credential_failure_before_any_map_is_a_503(self):
        """Test the same credential failure with no map in hand still fails closed as unavailable."""
        client = mock_secret_client(RefreshError('metadata server unavailable'))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)

        with pytest.raises(HTTPException) as exc:
            await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main')
        assert exc.value.status_code == HTTPStatus.SERVICE_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_concurrent_reads_after_ttl_lapse_fetch_once(self):
        """Test the refresh lock prevents a fetch stampede when the TTL lapses under load."""
        clock = FakeClock()
        client = mock_secret_client(
            *[secret_payload({'alice@example.com': ['cpg-fewgenomes-main']})] * 6,
        )
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client, ttl_secs=300, clock=clock)
        await access_list.load()
        clock.value += 301  # the TTL lapses while requests are in flight

        results = await asyncio.gather(
            *(access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') for _ in range(5)),
        )

        assert all(results)
        eager_load_plus_one_refresh = 2
        assert client.access_secret_version.await_count == eager_load_plus_one_refresh

    @pytest.mark.asyncio
    async def test_load_retries_before_giving_up(self):
        """Test the eager load retries a transient failure rather than failing the revision immediately."""
        client = mock_secret_client(
            PermissionDenied('transient'),
            secret_payload({'alice@example.com': ['cpg-fewgenomes-main']}),
        )
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await access_list.load()

        assert await access_list.is_allowed('alice@example.com', 'cpg-fewgenomes-main') is True

    @pytest.mark.asyncio
    async def test_load_raises_when_the_fetch_never_succeeds(self):
        """Test a permanently unreadable secret fails the deploy instead of 503ing every user."""
        client = mock_secret_client(*[PermissionDenied('no access')] * 10)
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)

        with patch('asyncio.sleep', new_callable=AsyncMock), pytest.raises(PermissionDenied):
            await access_list.load()

    @pytest.mark.asyncio
    async def test_reads_the_secret_version_named_by_the_contract(self):
        """Test the resource path matches the one SET-1249 fixed."""
        client = mock_secret_client(secret_payload({}))
        access_list = IgvProxyAccessList(project_id=PROJECT, client=client)
        await access_list.load()

        assert client.access_secret_version.await_args.kwargs['name'] == (
            f'projects/{PROJECT}/secrets/igv-proxy-config/versions/latest'
        )
