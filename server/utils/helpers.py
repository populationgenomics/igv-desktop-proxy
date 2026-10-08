from typing import Any

from starlette.datastructures import Headers

from server.utils.constants import CLOSED_BYTE_RANGE


def get_byte_range(range_header: str) -> int | None:
    """Return the total bytes in a single closed `bytes=START-END` range (START <= END), or None."""
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
    """Return a copy of the inbound headers with the caller's token replaced by the proxy's own."""
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
