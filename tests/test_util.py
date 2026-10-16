import pytest
from starlette.datastructures import Headers

from server.utils.helpers import build_gcs_headers, get_byte_range, get_headers, is_none


def test_get_byte_range():
    """Test get total bytes in range."""
    assert get_byte_range('bytes=0-511999') == 512000  # noqa: PLR2004
    assert get_byte_range('bytes=100-200') == 101  # noqa: PLR2004
    assert get_byte_range('bytes=5-5') == 1


UNMETERABLE_RANGES = [
    'bytes=1000-',  # open-ended
    'bytes=-500',  # suffix
    'bytes=0-1,5-6',  # multi-range
    'bytes=200-100',  # start after end would credit the budget
    'bytes=a-b',
    'bytes=',
    'foo',
    '0-100',  # missing unit
    'items=0-100',  # wrong unit
]


@pytest.mark.parametrize('range_header', UNMETERABLE_RANGES)
def test_get_byte_range_returns_none_for_an_unmeterable_range(range_header: str):
    """Test anything but a single closed, ordered range yields no byte count rather than raising."""
    assert get_byte_range(range_header) is None


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
    assert filtered == {'Authorization': 'Bearer token123'}

    filtered = get_headers(
        Headers(
            {
                'range': 'bytes=0-100',
            },
        ),
    )
    assert filtered == {'Range': 'bytes=0-100'}


def test_is_none():
    """Test return True when None is passed."""
    assert is_none(None) is True
    assert is_none('') is False
    assert is_none(0) is False


def test_build_gcs_headers_swaps_the_caller_token_for_the_proxy_token():
    """Test the caller's token never reaches GCS."""
    inbound = {'Authorization': 'Bearer caller-token', 'Range': 'bytes=0-100', 'Accept': '*/*'}

    outbound = build_gcs_headers(inbound, 'proxy-token')

    assert outbound == {
        'Authorization': 'Bearer proxy-token',
        'Range': 'bytes=0-100',
        'Accept': '*/*',
    }


def test_build_gcs_headers_does_not_mutate_the_inbound_headers():
    """Test the inbound view stays intact, so it remains the record of what the caller sent."""
    inbound = {'Authorization': 'Bearer caller-token'}

    outbound = build_gcs_headers(inbound, 'proxy-token')

    assert inbound == {'Authorization': 'Bearer caller-token'}
    assert outbound is not inbound


def test_build_gcs_headers_sets_authorization_when_the_caller_sent_none():
    """Test the proxy still authenticates upstream on a request that carried no auth header."""
    assert build_gcs_headers({'Range': 'bytes=0-100'}, 'proxy-token') == {
        'Range': 'bytes=0-100',
        'Authorization': 'Bearer proxy-token',
    }
