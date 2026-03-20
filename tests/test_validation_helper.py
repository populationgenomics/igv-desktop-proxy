"""Tests for validation_helper."""

from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from server.services.rate_limit_service import DownloadRateLimiter
from server.utils.validation_helper import (
    rate_limit_if_applicable,
    validate_and_parse_path,
    validate_auth,
)


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
    headers = {'Authorization': 'Bearer super-secret-token'}
    token = validate_auth(headers)
    assert token == 'super-secret-token'


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
        validate_auth({'Authorization': 'Bearer token extra'})
    assert exc.value.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.asyncio
async def test_rate_limit_if_applicable_index_file(mock_httpx_client, mock_redis_client):
    """Test returning None for index files."""
    result = await rate_limit_if_applicable(
        object_path='file.crai',  # index file
        range_header='bytes=0-100',
        user_token='token',
        httpx_client=mock_httpx_client,
        redis_client=mock_redis_client,
    )
    assert result is None


@pytest.mark.asyncio
async def test_rate_limit_if_applicable_no_range(mock_httpx_client, mock_redis_client):
    """Test exception when range is completely missing."""
    with pytest.raises(HTTPException) as exc:
        await rate_limit_if_applicable(
            object_path='file.cram',
            range_header=None,
            user_token='token',
            httpx_client=mock_httpx_client,
            redis_client=mock_redis_client,
        )
    assert exc.value.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.asyncio
async def test_rate_limit_if_applicable_first_byte_range(mock_httpx_client, mock_redis_client):
    """Test returning None for first byte range check."""
    result = await rate_limit_if_applicable(
        object_path='file.cram',
        range_header='bytes=0-511999',
        user_token='token',
        httpx_client=mock_httpx_client,
        redis_client=mock_redis_client,
    )
    assert result is None


@pytest.mark.asyncio
async def test_rate_limit_if_applicable_success(mock_httpx_client, mock_redis_client):
    """Test successful limit check returning rate limiter object."""
    with patch(
        'server.services.rate_limit_service.DownloadRateLimiter.check_user_limit',
        new_callable=AsyncMock,
    ) as mock_check:
        mock_check.return_value = True
        result = await rate_limit_if_applicable(
            object_path='file.cram',
            range_header='bytes=100-199',
            user_token='token',
            httpx_client=mock_httpx_client,
            redis_client=mock_redis_client,
        )
        assert isinstance(result, DownloadRateLimiter)
        assert result.request_bytes == 100  # noqa: PLR2004


@pytest.mark.asyncio
async def test_rate_limit_if_applicable_too_many_requests(mock_httpx_client, mock_redis_client):
    """Test limit check failure returning HTTP 429."""
    with patch(
        'server.services.rate_limit_service.DownloadRateLimiter.check_user_limit',
        new_callable=AsyncMock,
    ) as mock_check:
        mock_check.return_value = False
        with pytest.raises(HTTPException) as exc:
            await rate_limit_if_applicable(
                object_path='file.cram',
                range_header='bytes=100-199',
                user_token='token',
                httpx_client=mock_httpx_client,
                redis_client=mock_redis_client,
            )
        assert exc.value.status_code == HTTPStatus.TOO_MANY_REQUESTS
