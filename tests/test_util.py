from starlette.datastructures import Headers

from server.utils.util import get_byte_range, get_headers, is_none

"""
Tests functions in utils.util
"""


def test_get_byte_range():
    """Test get Total bytes in range."""
    assert get_byte_range('bytes=0-511999') == 512000  # noqa: PLR2004
    assert get_byte_range('bytes=100-200') == 101  # noqa: PLR2004


def test_get_headers():
    """Test get required headers."""
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


def test_is_none():
    """Test return True when None is passed."""
    assert is_none(None) is True
    assert is_none('') is False
    assert is_none(0) is False
