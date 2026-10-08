from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pytest_asyncio import fixture

from server.main import app
from server.utils.constants import FORBIDDEN_DETAIL
from tests.helpers import PROXY_TOKEN, ProxyTransport

CALLER_HEADERS = {'Authorization': 'Bearer token123'}
INDEX_PATH = '/mybucket/path/to/file.cram.crai'
DATA_PATH = '/mybucket/path/to/file.cram'


class TestMainAPI:
    """Test desktop proxy API endpoints."""

    @fixture(autouse=True)
    def set_up(
        self,
        mock_proxy_api: TestClient,
        proxy_transport: ProxyTransport,
        mock_access_list: MagicMock,
        mock_redis_client: MagicMock,
    ):
        """Set up the test client and the collaborators tests reach into."""
        self.proxy_api = mock_proxy_api
        self.transport = proxy_transport
        self.access_list = mock_access_list
        self.redis_client = mock_redis_client

    def test_health_check(self):
        """Test health check endpoint."""
        response = self.proxy_api.get('/health')
        assert response.status_code == HTTPStatus.OK
        assert 'OK' in response.text

    def test_invalid_path_is_rejected(self):
        """Test a path with no object component is a 400."""
        response = self.proxy_api.get('/bucket-only', headers=CALLER_HEADERS)
        assert response.status_code == HTTPStatus.BAD_REQUEST

    def test_missing_authorization_header_is_rejected(self):
        """Test an unauthenticated request never reaches identity resolution."""
        response = self.proxy_api.get(INDEX_PATH)
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert self.transport.requests == []

    # --- the header swap -------------------------------------------------------------------

    def test_index_file_get_is_served_with_the_proxy_credentials(self):
        """Test an index file GET succeeds, and GCS sees the proxy's token, not the caller's."""
        response = self.proxy_api.get(INDEX_PATH, headers=CALLER_HEADERS)

        assert response.status_code == HTTPStatus.OK
        assert response.content == b'fake data'

        gcs_request = self.transport.gcs_requests[0]
        assert gcs_request.headers['Authorization'] == f'Bearer {PROXY_TOKEN}'
        assert 'token123' not in gcs_request.headers['Authorization']

    def test_range_and_accept_cross_over_unchanged(self):
        """Test the headers that describe what the caller wants are forwarded verbatim."""
        headers = {**CALLER_HEADERS, 'Range': 'bytes=0-1023', 'Accept': 'application/octet-stream'}
        self.proxy_api.get(DATA_PATH, headers=headers)

        gcs_request = self.transport.gcs_requests[0]
        assert gcs_request.headers['Range'] == 'bytes=0-1023'
        assert gcs_request.headers['Accept'] == 'application/octet-stream'

    # --- authorization ---------------------------------------------------------------------

    def test_unauthorized_bucket_is_refused_without_touching_gcs(self):
        """Test a 403 is decided here, so an unlisted pair never reaches storage at all."""
        self.access_list.is_allowed = AsyncMock(return_value=False)

        response = self.proxy_api.get(INDEX_PATH, headers=CALLER_HEADERS)

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert self.transport.gcs_requests == []

    def test_403_body_tells_the_caller_how_to_get_access(self):
        """Test the refusal points the caller at their data contact, without internal details."""
        self.access_list.is_allowed = AsyncMock(return_value=False)

        response = self.proxy_api.get(DATA_PATH, headers={**CALLER_HEADERS, 'Range': 'bytes=0-1023'})

        assert response.json()['detail'] == FORBIDDEN_DETAIL

    def test_authorization_is_checked_against_the_resolved_email(self):
        """Test the access map is consulted with the verified email and the requested bucket."""
        self.proxy_api.get(INDEX_PATH, headers=CALLER_HEADERS)

        self.access_list.is_allowed.assert_awaited_once_with('alice@example.com', 'mybucket')

    def test_refused_request_does_not_consume_budget(self):
        """Test a request the proxy refuses never charges the caller's download quota."""
        self.access_list.is_allowed = AsyncMock(return_value=False)

        self.proxy_api.get(DATA_PATH, headers={**CALLER_HEADERS, 'Range': 'bytes=0-1023'})

        self.redis_client.deduct_if_balance.assert_not_called()

    def test_missing_access_map_is_a_503_not_a_403(self):
        """Test an access map that has never loaded fails closed as unavailable."""
        self.access_list.is_allowed = AsyncMock(side_effect=HTTPException(HTTPStatus.SERVICE_UNAVAILABLE))

        response = self.proxy_api.get(INDEX_PATH, headers=CALLER_HEADERS)

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert self.transport.gcs_requests == []

    # --- identity --------------------------------------------------------------------------

    @pytest.mark.parametrize('email_verified', [False, None])
    def test_unverified_email_is_refused(self, email_verified: bool | None):
        """Test an email the proxy cannot vouch for is refused, false and absent alike."""
        if email_verified is None:
            del self.transport.userinfo['email_verified']
        else:
            self.transport.userinfo['email_verified'] = email_verified

        response = self.proxy_api.get(INDEX_PATH, headers=CALLER_HEADERS)

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert self.transport.gcs_requests == []

    def test_token_without_an_email_claim_is_refused(self):
        """Test a token carrying a sub but no email cannot be authorized against an email-keyed map."""
        del self.transport.userinfo['email']

        response = self.proxy_api.get(INDEX_PATH, headers=CALLER_HEADERS)

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert self.transport.gcs_requests == []

    def test_invalid_token_is_refused_without_caching_none(self):
        """Test a token userinfo rejects 401s, and writes nothing into the identity cache."""
        self.transport.userinfo = {'error': 'invalid_request', 'error_description': 'Invalid Credentials'}

        response = self.proxy_api.get(INDEX_PATH, headers=CALLER_HEADERS)

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert self.transport.gcs_requests == []
        assert not [call for call in self.redis_client.set.call_args_list if call.args[0].startswith('token_hash')]

    # --- metering --------------------------------------------------------------------------

    def test_range_request_is_metered_and_recorded(self):
        """Test a ranged GET reserves its bytes and records the download once it streams."""
        response = self.proxy_api.get(DATA_PATH, headers={**CALLER_HEADERS, 'Range': 'bytes=0-1023'})

        assert response.status_code == HTTPStatus.OK
        self.redis_client.deduct_if_balance.assert_awaited_once()
        assert self.redis_client.deduct_if_balance.await_args.args[1] == 1024  # noqa: PLR2004
        self.redis_client.increment_download_stats.assert_awaited_once()

    def test_credential_failure_does_not_strand_budget(self, mock_proxy_credentials: MagicMock):
        """Test a failure minting the proxy's own token never costs the caller an hour of quota."""
        mock_proxy_credentials.get_token = AsyncMock(side_effect=RuntimeError('no token'))

        with pytest.raises(RuntimeError):
            self.proxy_api.get(DATA_PATH, headers={**CALLER_HEADERS, 'Range': 'bytes=0-1023'})

        self.redis_client.deduct_if_balance.assert_not_called()

    def test_exhausted_budget_is_a_429(self):
        """Test a user out of budget is refused before anything is fetched."""
        self.redis_client.deduct_if_balance = AsyncMock(return_value=-1)

        response = self.proxy_api.get(DATA_PATH, headers={**CALLER_HEADERS, 'Range': 'bytes=0-1023'})

        assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
        assert self.transport.gcs_requests == []

    def test_index_file_get_is_not_metered(self):
        """Test an index file served whole neither reserves budget nor writes a stats key."""
        self.proxy_api.get(INDEX_PATH, headers=CALLER_HEADERS)

        self.redis_client.deduct_if_balance.assert_not_called()
        self.redis_client.increment_download_stats.assert_not_called()

    def test_data_file_without_a_range_is_rejected(self):
        """Test a data file GET with no Range is a 400: it cannot be metered."""
        response = self.proxy_api.get(DATA_PATH, headers=CALLER_HEADERS)
        assert response.status_code == HTTPStatus.BAD_REQUEST

    def test_data_file_with_an_open_ended_range_is_rejected_without_consuming_budget(self):
        """Test an open-ended Range on a data file is a 400 decided before any metering."""
        response = self.proxy_api.get(DATA_PATH, headers={**CALLER_HEADERS, 'Range': 'bytes=1000-'})

        assert response.status_code == HTTPStatus.BAD_REQUEST
        self.redis_client.deduct_if_balance.assert_not_called()
        assert self.transport.gcs_requests == []

    # --- HEAD ------------------------------------------------------------------------------

    def test_head_is_authorized_but_never_metered(self):
        """Test HEAD gains the authorize hop it never had, and still records no download."""
        response = self.proxy_api.head(INDEX_PATH, headers=CALLER_HEADERS)

        assert response.status_code == HTTPStatus.OK
        self.access_list.is_allowed.assert_awaited_once_with('alice@example.com', 'mybucket')
        self.redis_client.deduct_if_balance.assert_not_called()
        self.redis_client.increment_download_stats.assert_not_called()

    def test_head_on_an_unauthorized_bucket_leaks_nothing(self):
        """Test an unlisted user cannot probe object existence or size through HEAD."""
        self.access_list.is_allowed = AsyncMock(return_value=False)

        response = self.proxy_api.head(DATA_PATH, headers=CALLER_HEADERS)

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert self.transport.gcs_requests == []

    def test_head_is_served_with_the_proxy_credentials(self):
        """Test HEAD swaps credentials exactly as GET does."""
        self.proxy_api.head(DATA_PATH, headers=CALLER_HEADERS)

        assert self.transport.gcs_requests[0].headers['Authorization'] == f'Bearer {PROXY_TOKEN}'

    # --- upstream failures -------------------------------------------------------------------

    @pytest.mark.parametrize(
        ('upstream_status', 'expected_status'),
        [
            (HTTPStatus.NOT_FOUND, HTTPStatus.NOT_FOUND),
            (HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE),
            (HTTPStatus.FORBIDDEN, HTTPStatus.BAD_GATEWAY),
            (HTTPStatus.UNAUTHORIZED, HTTPStatus.BAD_GATEWAY),
            (HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.BAD_GATEWAY),
        ],
    )
    def test_upstream_failures_are_mapped(self, upstream_status: HTTPStatus, expected_status: HTTPStatus):
        """Test user-facing upstream answers pass through and proxy-side faults become 502s."""
        upstream_body = 'proxy-sa@project.iam.gserviceaccount.com denied; bucket exists'
        self.transport.gcs_handler = lambda _request: httpx.Response(upstream_status, text=upstream_body)

        response = self.proxy_api.get(DATA_PATH, headers={**CALLER_HEADERS, 'Range': 'bytes=0-1023'})

        assert response.status_code == expected_status
        assert 'gserviceaccount' not in response.text

    def test_upstream_failure_refunds_the_reserved_bytes(self):
        """Test budget reserved for a request GCS refused is handed back."""
        self.transport.gcs_handler = lambda _request: httpx.Response(HTTPStatus.NOT_FOUND)

        self.proxy_api.get(DATA_PATH, headers={**CALLER_HEADERS, 'Range': 'bytes=0-1023'})

        self.redis_client.refund.assert_awaited_once()


class TestLifespan:
    """Tests for what the app does before it serves anything."""

    @pytest.mark.usefixtures('proxy_env')
    def test_startup_fails_when_the_access_map_never_loads(self, mock_proxy_credentials: MagicMock):
        """Test an unreadable secret fails the revision instead of 503ing every user."""
        broken_access_list = MagicMock()
        broken_access_list.load = AsyncMock(side_effect=PermissionError('secret is unreadable'))

        with (
            patch('server.main.IgvProxyAccessList', return_value=broken_access_list),
            patch('server.main.ProxyCredentials', return_value=mock_proxy_credentials),
            pytest.raises(PermissionError),
            TestClient(app),
        ):
            pass  # pragma: no cover

    @pytest.mark.usefixtures('proxy_env')
    def test_startup_fails_without_its_required_configuration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_access_list: MagicMock,
        mock_proxy_credentials: MagicMock,
    ):
        """Test a missing config project is a deploy failure, not a silently wrong secret."""
        monkeypatch.setenv('IGV_PROXY_CONFIG_PROJECT', '   ')

        with (
            patch('server.main.IgvProxyAccessList', return_value=mock_access_list),
            patch('server.main.ProxyCredentials', return_value=mock_proxy_credentials),
            pytest.raises(RuntimeError, match='IGV_PROXY_CONFIG_PROJECT'),
            TestClient(app),
        ):
            pass  # pragma: no cover
