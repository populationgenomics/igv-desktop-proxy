from starlette.datastructures import Headers


def get_byte_range(range_header: str) -> int:
    """Return the total bytes of the given byte range."""
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

    return _headers
