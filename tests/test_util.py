from starlette.datastructures import Headers

from server.utils.helpers import format_redis_key, get_byte_range, get_headers, is_none


def test_get_byte_range():
    """Test get total bytes in range."""
    assert get_byte_range('bytes=0-511999') == 512000  # noqa: PLR2004
    assert get_byte_range('bytes=100-200') == 101  # noqa: PLR2004


def test_get_headers():
    """Test return the headers."""
    headers = Headers(
        {
            'authorization': 'Bearer token123',
            'range': 'bytes=0-100',
            'host': 'localhost',
        },
    )
    filtered = get_headers(headers)
    assert filtered == {
        'Authorization': 'Bearer token123',
        'Range': 'bytes=0-100',
    }

    filtered = get_headers(
        Headers(
            {
                'authorization': 'Bearer token123',
            },
        ),
    )
    assert filtered == {'range': 'bytes=0-100'}

    filtered = get_headers(
        Headers(
            {
                'Range': 'bytes=0-100',
            },
        ),
    )
    assert filtered == {'Authorization': 'Bearer token123'}


def test_is_none():
    """Test return True when None is passed."""
    assert is_none(None) is True
    assert is_none('') is False
    assert is_none(0) is False


def test_format_redis_key():
    """Test returns prefix:key string."""
    assert format_redis_key('token', 'abc123') == 'token:abc123'


def test_format_redis_key_returns_none_when_prefix_is_none():
    """Test returns key when prefix is None."""
    assert format_redis_key(None, 'abc123') == 'abc123'
