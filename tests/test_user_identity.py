import json
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import tenacity
from fastapi import HTTPException

from server.services.user_identity import UserIdentityResolver, verify_user_info
from tests.helpers import userinfo_response


class TestUserIdentityResolver:
    """Tests for UserIdentityResolver."""

    @pytest.fixture(autouse=True)
    def set_up(self, mock_redis_client: MagicMock, mock_httpx_client: httpx.AsyncClient):
        """Set up the user identity resolver."""
        self.resolver = UserIdentityResolver(
            redis_client=mock_redis_client,
            http_client=mock_httpx_client,
            user_token='fake_token',  # noqa: S106
        )
        self.redis_client = mock_redis_client

    @pytest.mark.asyncio
    async def test_resolve_returns_sub_and_email(self):
        """Test resolve returns both identity fields the request flow needs."""
        identity = {'sub': 'user123', 'email': 'alice@example.com'}
        with patch.object(self.resolver, 'get_authenticated_identity', return_value=identity):
            assert await self.resolver.resolve() == identity

    @pytest.mark.asyncio
    async def test_resolve_raises_401_on_failed_identification(self):
        """Test resolve raises 401 when no identity could be established."""
        with (
            patch.object(self.resolver, 'get_authenticated_identity', return_value=None),
            pytest.raises(HTTPException) as exc,
        ):
            await self.resolver.resolve()
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_resolve_raises_401_on_retry_failure(self):
        """Test resolve raises 401 when get_authenticated_identity exhausts its retries."""
        with (
            patch.object(
                self.resolver,
                'get_authenticated_identity',
                side_effect=tenacity.RetryError(MagicMock()),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await self.resolver.resolve()
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_cached(self):
        """Test a cached identity is returned without calling userinfo."""
        self.redis_client.get.return_value = json.dumps({'sub': 'user123', 'email': 'alice@example.com'})

        identity = await self.resolver.get_authenticated_identity('hash123')
        assert identity == {'sub': 'user123', 'email': 'alice@example.com'}

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_discards_an_unusable_cache_value(self):
        """Test a cached value that is not a complete identity is refetched rather than trusted."""
        self.redis_client.get.return_value = 'not json'

        with patch.object(self.resolver, 'fetch_user_info', return_value=userinfo_response()):
            identity = await self.resolver.get_authenticated_identity('hash_corrupt')

        assert identity == {'sub': 'user123', 'email': 'alice@example.com'}

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_caches_identity_as_json(self):
        """Test the identity is cached as JSON for an access token's maximum lifetime."""
        self.redis_client.get.return_value = None

        with patch.object(self.resolver, 'fetch_user_info', return_value=userinfo_response()):
            await self.resolver.get_authenticated_identity('hash_fresh')

        cache_write = next(
            call for call in self.redis_client.set.call_args_list if call.args[0].startswith('token_hash_v2:')
        )
        assert json.loads(cache_write.args[1]) == {'sub': 'user123', 'email': 'alice@example.com'}
        assert cache_write.kwargs['ex'] == 3600  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_retry_logic(self) -> None:
        """Test get_authenticated_identity retries to acquire the lock before invoking userinfo."""
        with (
            patch('asyncio.sleep', new_callable=AsyncMock),
            patch.object(self.resolver, 'fetch_user_info', return_value=userinfo_response()),
        ):
            self.redis_client.get.return_value = None

            # 1st attempt: lock fails
            # 2nd attempt: lock fails
            # 3rd attempt: lock succeeds, then it caches the result (called again for caching)
            self.redis_client.set.side_effect = [False, False, True, True]
            identity = await self.resolver.get_authenticated_identity('hash_retry')
            assert identity == {'sub': 'user123', 'email': 'alice@example.com'}
            assert self.redis_client.set.call_count == 4  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_retry_gets_cached_value(self):
        """Test get_authenticated_identity gets the value another request cached, on retry."""
        other = json.dumps({'sub': 'user_from_other_request', 'email': 'bob@example.com'})
        self.redis_client.get.side_effect = [None, other]
        self.redis_client.set.return_value = False

        with patch('asyncio.sleep', new_callable=AsyncMock), patch.object(self.resolver, 'fetch_user_info'):
            identity = await self.resolver.get_authenticated_identity('hash_concurrent')

            assert identity == {'sub': 'user_from_other_request', 'email': 'bob@example.com'}
            assert self.redis_client.get.call_count == 2  # noqa: PLR2004
            # called 1 time (on the first attempt to acquire lock, second returns early from cache)
            assert self.redis_client.set.call_count == 1

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_raises_retry_error_on_exhaustion(self):
        """Test get_authenticated_identity raises RetryError after all retry attempts return None."""
        self.redis_client.get.return_value = None  # no cache hit on any attempt
        self.redis_client.set.return_value = False  # lock never acquired -> always returns None

        with patch('asyncio.sleep', new_callable=AsyncMock), pytest.raises(tenacity.RetryError):
            await self.resolver.get_authenticated_identity('hash_exhausted')

    @pytest.mark.asyncio
    async def test_invalid_token_raises_401_without_caching_none(self):
        """Test a rejected token 401s immediately and never writes None into the identity cache."""
        self.redis_client.get.return_value = None

        with (
            patch.object(self.resolver, 'fetch_user_info', return_value={'error': 'invalid_request'}),
            pytest.raises(HTTPException) as exc,
        ):
            await self.resolver.get_authenticated_identity('hash_invalid')

        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED
        assert not [call for call in self.redis_client.set.call_args_list if call.args[0].startswith('token_hash')]
        self.redis_client.release_lock.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_user_info_calls_the_userinfo_endpoint(self):
        """Test the proxy asks userinfo, not tokeninfo, which Google says isn't for production use."""
        mock_response = httpx.Response(HTTPStatus.OK, json=userinfo_response())

        with patch.object(self.resolver.http_client, 'get', return_value=mock_response) as mock_get:
            user_info = await self.resolver.fetch_user_info()

        assert user_info == userinfo_response()
        assert mock_get.call_args.args[0] == 'https://www.googleapis.com/oauth2/v3/userinfo'
        assert mock_get.call_args.kwargs['params'] == {'access_token': 'fake_token'}

    @pytest.mark.asyncio
    @pytest.mark.parametrize('status', [HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.SERVICE_UNAVAILABLE])
    async def test_fetch_user_info_retries_when_throttled_or_down(self, status: HTTPStatus):
        """Test a throttled or failing userinfo is retried, not taken as a verdict on the token."""
        mock_response = httpx.Response(status, json={'error': 'rate_limit_exceeded'})

        with patch.object(self.resolver.http_client, 'get', return_value=mock_response):
            assert await self.resolver.fetch_user_info() is None

    @pytest.mark.asyncio
    async def test_fetch_user_info_failure(self):
        """Test fetch_user_info returns None on a transport failure, so the call is retried."""
        with patch.object(self.resolver.http_client, 'get', side_effect=httpx.HTTPError('Error Requesting')):
            assert await self.resolver.fetch_user_info() is None

    def test_verify_accepts_a_well_formed_token(self):
        """Test a token with a verified email yields an identity."""
        assert verify_user_info(userinfo_response()) == {
            'sub': 'user123',
            'email': 'alice@example.com',
        }

    def test_verify_rejects_a_token_with_no_email(self):
        """Test a token granted without the email scope is refused, not silently unauthorized."""
        payload = userinfo_response()
        del payload['email']
        with pytest.raises(HTTPException) as exc:
            verify_user_info(payload)
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.parametrize('verified', [True, 'true'])
    def test_verify_accepts_a_verified_email(self, verified: object):
        """Test both userinfo's boolean and tokeninfo's string form of a verified email are trusted."""
        assert verify_user_info(userinfo_response(email_verified=verified))['email'] == 'alice@example.com'

    @pytest.mark.parametrize('verified', [False, 'false', 'True', 1, '', None])
    def test_verify_rejects_an_unverified_email(self, verified: object):
        """Test only an affirmatively verified email is trusted against an email-keyed access map."""
        with pytest.raises(HTTPException) as exc:
            verify_user_info(userinfo_response(email_verified=verified))
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    def test_verify_rejects_an_absent_email_verified(self):
        """Test an absent email_verified claim refuses, exactly as an explicit false does."""
        payload = userinfo_response()
        del payload['email_verified']
        with pytest.raises(HTTPException) as exc:
            verify_user_info(payload)
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    def test_verify_rejects_a_token_with_no_sub(self):
        """Test an identity without a sub is refused; the sub keys every budget and stats record."""
        payload = userinfo_response()
        del payload['sub']
        with pytest.raises(HTTPException) as exc:
            verify_user_info(payload)
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED
