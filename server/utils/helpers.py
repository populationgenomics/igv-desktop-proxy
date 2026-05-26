from typing import Any

from starlette.datastructures import Headers


def get_byte_range(range_header: str) -> int:
    """Return the total bytes in the given byte range."""
    range_parts = range_header.replace('bytes=', '').split('-')
    start_byte = int(range_parts[0])
    end_byte = int(range_parts[1])

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


def is_none(value: Any) -> bool:
    """Return True if value is None."""
    return value is None


def format_redis_key(prefix: str | None, key: str) -> str | None:
    """Return the redis key combined with a prefix."""
    if is_none(prefix):
        return key
    return f'{prefix}:{key}'
