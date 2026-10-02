import json
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import tenacity
from fastapi import HTTPException

from server.services.rate_limiter import DownloadRateLimiter


def tokeninfo_response(**overrides: object) -> dict:
    """Return a tokeninfo payload shaped the way oauth2.googleapis.com actually returns one.

    Note every value is a string on the wire, including `email_verified` and `expires_in`. `aud` is
    present because the endpoint returns it, not because the proxy checks it.
    """
    payload: dict[str, object] = {
        'sub': 'user123',
        'email': 'alice@example.com',
        'email_verified': 'true',
        'aud': '1234-abc.apps.googleusercontent.com',
        'scope': 'openid email',
        'expires_in': '3599',
    }
    payload.update(overrides)
    return payload


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
    async def test_resolve_user_sets_sub_and_email(self):
        """Test resolve_user records both identity fields the request flow needs."""
        identity = {'sub': 'user123', 'email': 'alice@example.com'}
        with patch.object(self.rate_limiter, 'get_authenticated_identity', return_value=identity):
            assert await self.rate_limiter.resolve_user() == 'alice@example.com'

        assert self.rate_limiter.user_sub == 'user123'
        assert self.rate_limiter.user_email == 'alice@example.com'

    @pytest.mark.asyncio
    async def test_resolve_user_raises_401_on_failed_identification(self):
        """Test resolve_user raises 401 when no identity could be established."""
        with (
            patch.object(self.rate_limiter, 'get_authenticated_identity', return_value=None),
            pytest.raises(HTTPException) as exc,
        ):
            await self.rate_limiter.resolve_user()
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_resolve_user_raises_401_on_retry_failure(self):
        """Test resolve_user raises 401 when get_authenticated_identity exhausts its retries."""
        with (
            patch.object(
                self.rate_limiter,
                'get_authenticated_identity',
                side_effect=tenacity.RetryError(MagicMock()),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await self.rate_limiter.resolve_user()
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_cached(self):
        """Test a cached identity is returned without calling tokeninfo."""
        self.redis_client.get.return_value = json.dumps({'sub': 'user123', 'email': 'alice@example.com'})

        identity = await self.rate_limiter.get_authenticated_identity('hash123')
        assert identity == {'sub': 'user123', 'email': 'alice@example.com'}

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_discards_an_unusable_cache_value(self):
        """Test a cached value that is not a complete identity is refetched rather than trusted."""
        self.redis_client.get.return_value = 'not json'

        with patch.object(self.rate_limiter, 'fetch_token_info', return_value=tokeninfo_response()):
            identity = await self.rate_limiter.get_authenticated_identity('hash_corrupt')

        assert identity == {'sub': 'user123', 'email': 'alice@example.com'}

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_caches_identity_as_json(self):
        """Test the identity is cached as JSON, with a TTL bounded by the token's own lifetime."""
        self.redis_client.get.return_value = None

        with patch.object(self.rate_limiter, 'fetch_token_info', return_value=tokeninfo_response()):
            await self.rate_limiter.get_authenticated_identity('hash_fresh')

        cache_write = next(
            call for call in self.redis_client.set.call_args_list if call.args[0].startswith('token_hash_v2:')
        )
        assert json.loads(cache_write.args[1]) == {'sub': 'user123', 'email': 'alice@example.com'}
        assert cache_write.kwargs['ex'] == 3599  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_cache_ttl_never_exceeds_an_hour(self):
        """Test an implausibly long expires_in cannot extend the identity cache past an hour."""
        self.redis_client.get.return_value = None

        with patch.object(
            self.rate_limiter,
            'fetch_token_info',
            return_value=tokeninfo_response(expires_in='86400'),
        ):
            await self.rate_limiter.get_authenticated_identity('hash_long')

        cache_write = next(
            call for call in self.redis_client.set.call_args_list if call.args[0].startswith('token_hash_v2:')
        )
        assert cache_write.kwargs['ex'] == 3600  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_retry_logic(self) -> None:
        """Test get_authenticated_identity retries to acquire the lock before invoking tokeninfo."""
        with (
            patch('asyncio.sleep', new_callable=AsyncMock),
            patch.object(self.rate_limiter, 'fetch_token_info', return_value=tokeninfo_response()),
        ):
            self.redis_client.get.return_value = None

            # 1st attempt: lock fails
            # 2nd attempt: lock fails
            # 3rd attempt: lock succeeds, then it caches the result (called again for caching)
            self.redis_client.set.side_effect = [False, False, True, True]
            identity = await self.rate_limiter.get_authenticated_identity('hash_retry')
            assert identity == {'sub': 'user123', 'email': 'alice@example.com'}
            assert self.redis_client.set.call_count == 4  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_get_authenticated_identity_retry_gets_cached_value(self):
        """Test get_authenticated_identity gets the value another request cached, on retry."""
        other = json.dumps({'sub': 'user_from_other_request', 'email': 'bob@example.com'})
        self.redis_client.get.side_effect = [None, other]
        self.redis_client.set.return_value = False

        with patch('asyncio.sleep', new_callable=AsyncMock), patch.object(self.rate_limiter, 'fetch_token_info'):
            identity = await self.rate_limiter.get_authenticated_identity('hash_concurrent')

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
            await self.rate_limiter.get_authenticated_identity('hash_exhausted')

    @pytest.mark.asyncio
    async def test_invalid_token_raises_401_without_caching_none(self):
        """Test a rejected token 401s immediately and never writes None into the identity cache."""
        self.redis_client.get.return_value = None

        with (
            patch.object(self.rate_limiter, 'fetch_token_info', return_value={'error': 'invalid_token'}),
            pytest.raises(HTTPException) as exc,
        ):
            await self.rate_limiter.get_authenticated_identity('hash_invalid')

        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED
        assert not [call for call in self.redis_client.set.call_args_list if call.args[0].startswith('token_hash')]
        self.redis_client.release_lock.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_token_info_calls_the_tokeninfo_endpoint(self):
        """Test the proxy asks tokeninfo, which returns expires_in alongside the identity claims."""
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.json.return_value = tokeninfo_response()

        with patch.object(self.rate_limiter.http_client, 'get', return_value=mock_response) as mock_get:
            token_info = await self.rate_limiter.fetch_token_info()

        assert token_info == tokeninfo_response()
        assert mock_get.call_args.args[0] == 'https://oauth2.googleapis.com/tokeninfo'
        assert mock_get.call_args.kwargs['params'] == {'access_token': 'fake_token'}

    @pytest.mark.asyncio
    async def test_fetch_token_info_failure(self):
        """Test fetch_token_info returns None on a transport failure, so the call is retried."""
        with patch.object(self.rate_limiter.http_client, 'get', side_effect=httpx.HTTPError('Error Requesting')):
            assert await self.rate_limiter.fetch_token_info() is None

    def test_verify_accepts_a_well_formed_token(self):
        """Test a token with a verified email yields an identity."""
        assert self.rate_limiter.verify_token_info(tokeninfo_response()) == {
            'sub': 'user123',
            'email': 'alice@example.com',
        }

    def test_verify_accepts_a_boolean_email_verified(self):
        """Test a genuine JSON boolean is accepted as well as tokeninfo's string form."""
        assert self.rate_limiter.verify_token_info(tokeninfo_response(email_verified=True))['sub'] == 'user123'

    def test_verify_ignores_the_audience(self):
        """Test the proxy accepts a token minted for any OAuth client, including none at all.

        Deliberate: anyone in the access map may read their buckets with any Google token they
        hold. The access map, not the calling application, is the control.
        """
        assert self.rate_limiter.verify_token_info(tokeninfo_response(aud='9999-other.apps.googleusercontent.com'))

        payload = tokeninfo_response()
        del payload['aud']
        assert self.rate_limiter.verify_token_info(payload) == {'sub': 'user123', 'email': 'alice@example.com'}

    def test_verify_rejects_a_token_with_no_email(self):
        """Test a token granted without the email scope is refused, not silently unauthorized."""
        payload = tokeninfo_response()
        del payload['email']
        with pytest.raises(HTTPException) as exc:
            self.rate_limiter.verify_token_info(payload)
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.parametrize('verified', ['false', False, 'False', '', None])
    def test_verify_rejects_an_unverified_email(self, verified: object):
        """Test only an affirmatively verified email is trusted against an email-keyed access map."""
        with pytest.raises(HTTPException) as exc:
            self.rate_limiter.verify_token_info(tokeninfo_response(email_verified=verified))
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    def test_verify_rejects_an_absent_email_verified(self):
        """Test an absent email_verified claim refuses, exactly as an explicit false does."""
        payload = tokeninfo_response()
        del payload['email_verified']
        with pytest.raises(HTTPException) as exc:
            self.rate_limiter.verify_token_info(payload)
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    def test_verify_rejects_a_token_with_no_sub(self):
        """Test an identity without a sub is refused; the sub keys every budget and stats record."""
        payload = tokeninfo_response()
        del payload['sub']
        with pytest.raises(HTTPException) as exc:
            self.rate_limiter.verify_token_info(payload)
        assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

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
