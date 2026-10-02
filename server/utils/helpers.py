import re
from typing import Any

from starlette.datastructures import Headers

# A single closed range, the only form whose byte count is known before the response arrives.
CLOSED_BYTE_RANGE = re.compile(r'bytes=([0-9]+)-([0-9]+)', re.IGNORECASE)


def get_byte_range(range_header: str) -> int | None:
    """Return the total bytes in a single closed `bytes=START-END` range, or None.

    None covers every Range that has no exact byte count: open-ended (`bytes=1000-`), suffix
    (`bytes=-500`), multi-range, unparseable, and START > END — that last would otherwise yield a
    zero or negative count and credit the caller's budget.
    """
    match = CLOSED_BYTE_RANGE.fullmatch(range_header.strip())
    if match is None:
        return None

    start_byte, end_byte = int(match[1]), int(match[2])
    if start_byte > end_byte:
        return None

    return end_byte - start_byte + 1


def get_headers(headers: Headers) -> dict[str, str]:
    """Return the headers as a dictionary."""
    _headers = {}
    if 'authorization' in headers:
        _headers['Authorization'] = headers['authorization']
    if 'range' in headers:
        _headers['Range'] = headers['range']

    if 'accept' in headers:
        _headers['Accept'] = headers['accept']

    return _headers


def build_gcs_headers(inbound: dict[str, str], proxy_token: str) -> dict[str, str]:
    """Return the headers to send upstream: the inbound ones, with our own credentials.

    Returns a fresh dict; `inbound` stays the record of what the caller actually sent. The
    caller's token is only ever used to learn who they are, and must not reach GCS.
    """
    gcs_headers = {name: value for name, value in inbound.items() if name.lower() != 'authorization'}
    gcs_headers['Authorization'] = f'Bearer {proxy_token}'
    return gcs_headers


def is_none(value: Any) -> bool:
    """Return True if value is None."""
    return value is None


def format_redis_key(prefix: str | None, key: str) -> str | None:
    """Return the redis key combined with a prefix."""
    if is_none(prefix):
        return key
    return f'{prefix}:{key}'
