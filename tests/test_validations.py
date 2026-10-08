from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from server.services.rate_limiter import DownloadRateLimiter
from server.utils.constants import FORBIDDEN_DETAIL
from server.utils.validation import (
    apply_rate_limit,
    authorize_bucket_access,
    resolve_request_bytes,
    validate_and_parse_path,
    validate_auth,
)

dummy_auth_string = 'dummy_value'


def make_rate_limiter(*, within_limits: bool) -> MagicMock:
    """Return a DownloadRateLimiter stand-in that reports whether the user has budget left."""
    rate_limiter = MagicMock(spec=DownloadRateLimiter)
    rate_limiter.evaluate_download_limits = AsyncMock(return_value=within_limits)
    return rate_limiter


def test_validate_and_parse_path_success():
    """Test valid path returns bucket and path."""
    bucket, path = validate_and_parse_path('my-bucket/some/file.cram')
    assert bucket == 'my-bucket'
    assert path == 'some/file.cram'


def test_validate_and_parse_path_invalid():
    """Test invalid path raises HTTP 400."""
    with pytest.raises(HTTPException) as exc:
        validate_and_parse_path('my-bucket-without-slash')
    assert exc.value.status_code == HTTPStatus.BAD_REQUEST
    assert exc.value.detail == 'Invalid path format. Use /bucket/path'


def test_validate_auth_success():
    """Test valid auth header returns the token."""
    headers = {'Authorization': f'Bearer {dummy_auth_string}'}
    token = validate_auth(headers)
    assert token == dummy_auth_string


def test_validate_auth_missing():
    """Test missing auth header raises HTTP 401."""
    with pytest.raises(HTTPException) as exc:
        validate_auth({})
    assert exc.value.status_code == HTTPStatus.UNAUTHORIZED


def test_validate_auth_invalid_format():
    """Test invalid auth header (wrong parts) raises HTTP 401."""
    with pytest.raises(HTTPException) as exc:
        validate_auth({'Authorization': 'Bearer'})
    assert exc.value.status_code == HTTPStatus.UNAUTHORIZED

    with pytest.raises(HTTPException) as exc:
        validate_auth({'Authorization': 'Bearer dummy extra'})
    assert exc.value.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize('object_path', ['file.cram.crai', 'file.bam.bai', 'file.vcf.gz.tbi', 'file.bam.csi'])
def test_index_file_without_a_range_is_not_metered(object_path: str):
    """Test index files served whole carry no byte reservation."""
    assert resolve_request_bytes(object_path, None) is None


def test_non_index_file_without_a_range_is_rejected():
    """Test a data file with no Range is refused: it cannot be rate-limited."""
    with pytest.raises(HTTPException) as exc:
        resolve_request_bytes('file.cram', None)
    assert exc.value.status_code == HTTPStatus.BAD_REQUEST


def test_range_request_reserves_its_byte_count():
    """Test a ranged request reserves exactly the bytes it asked for."""
    assert resolve_request_bytes('file.cram', 'bytes=100-199') == 100  # noqa: PLR2004
    assert resolve_request_bytes('file.cram.crai', 'bytes=0-999999') == 1000000  # noqa: PLR2004


UNMETERABLE_RANGES = ['bytes=1000-', 'bytes=-500', 'bytes=0-1,5-6', 'bytes=200-100', 'foo']


@pytest.mark.parametrize('range_header', UNMETERABLE_RANGES)
def test_non_index_file_with_an_unmeterable_range_is_rejected(range_header: str):
    """Test a data file whose Range has no exact byte count is a 400, never a 500."""
    with pytest.raises(HTTPException) as exc:
        resolve_request_bytes('file.cram', range_header)
    assert exc.value.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize('range_header', UNMETERABLE_RANGES)
def test_index_file_with_an_unmeterable_range_is_not_metered(range_header: str):
    """Test an index file with an unmeterable Range is treated as if it sent none."""
    assert resolve_request_bytes('file.cram.crai', range_header) is None


@pytest.mark.asyncio
async def test_authorize_allows_a_listed_user():
    """Test an authorized user passes the hop without an exception."""
    access_list = MagicMock()
    access_list.is_allowed = AsyncMock(return_value=True)

    await authorize_bucket_access(access_list, 'alice@example.com', 'cpg-fewgenomes-main')


@pytest.mark.asyncio
async def test_authorize_raises_403_with_a_generic_detail():
    """Test the 403 points the caller at their data contact without exposing how access is granted."""
    access_list = MagicMock()
    access_list.is_allowed = AsyncMock(return_value=False)

    with pytest.raises(HTTPException) as exc:
        await authorize_bucket_access(access_list, 'alice@example.com', 'cpg-fewgenomes-main')

    assert exc.value.status_code == HTTPStatus.FORBIDDEN
    assert exc.value.detail == FORBIDDEN_DETAIL
    assert 'cpg-infrastructure-private' not in exc.value.detail


@pytest.mark.asyncio
async def test_authorize_lets_the_503_through_untouched():
    """Test an unavailable access map is not flattened into a 403."""
    access_list = MagicMock()
    access_list.is_allowed = AsyncMock(side_effect=HTTPException(HTTPStatus.SERVICE_UNAVAILABLE))

    with pytest.raises(HTTPException) as exc:
        await authorize_bucket_access(access_list, 'alice@example.com', 'cpg-fewgenomes-main')
    assert exc.value.status_code == HTTPStatus.SERVICE_UNAVAILABLE


@pytest.mark.asyncio
async def test_unmetered_request_is_not_charged():
    """Test an unmetered request neither deducts budget nor reports itself metered.

    Handing the limiter to the streamer would record a zero-byte download as an empty CSV row.
    """
    rate_limiter = make_rate_limiter(within_limits=True)

    assert await apply_rate_limit(rate_limiter, None) is False
    rate_limiter.evaluate_download_limits.assert_not_called()


@pytest.mark.asyncio
async def test_metered_request_deducts_and_reports_metered():
    """Test a metered request within budget is charged and reported as metered."""
    rate_limiter = make_rate_limiter(within_limits=True)

    assert await apply_rate_limit(rate_limiter, 512_000) is True
    rate_limiter.evaluate_download_limits.assert_awaited_once()


@pytest.mark.asyncio
async def test_raise_error_when_rate_limit_exceeded():
    """Test limit check failure returning HTTP 429 if the user has exhausted their quota."""
    rate_limiter = make_rate_limiter(within_limits=False)

    with pytest.raises(HTTPException) as exc:
        await apply_rate_limit(rate_limiter, 512_000)
    assert exc.value.status_code == HTTPStatus.TOO_MANY_REQUESTS
