import logging
from http import HTTPStatus

from fastapi import HTTPException

from server.services.access_list import IgvProxyAccessList
from server.services.rate_limiter import DownloadRateLimiter
from server.utils.constants import (
    AUTH_HEADER_PARTS,
    FORBIDDEN_DETAIL,
    INDEX_FILE_SUFFIXES,
    REQUEST_URL_PARTS,
)
from server.utils.helpers import get_byte_range


def validate_and_parse_path(full_path: str) -> tuple[str, str]:
    """Parse bucket/path and return bucket, path tuple."""
    path_segments = full_path.split('/', 1)

    if len(path_segments) < REQUEST_URL_PARTS:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST.value,
            detail='Invalid path format. Use /bucket/path',
        )

    return path_segments[0], path_segments[1]


def validate_auth(headers: dict) -> str:
    """Validate auth header in the request."""
    auth_header = headers.get('Authorization')

    if auth_header is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED)

    user_token = auth_header.split(' ')

    if len(user_token) != AUTH_HEADER_PARTS:
        raise HTTPException(HTTPStatus.UNAUTHORIZED)

    return user_token[1].strip()


async def authorize_bucket_access(access_list: IgvProxyAccessList, email: str, bucket: str) -> None:
    """Raise 403 unless this user is listed against this bucket (503 if no map has ever loaded)."""
    if await access_list.is_allowed(email, bucket):
        return

    logging.info(f'Refused {email} access to {bucket}: not in the IGV proxy access map.')
    raise HTTPException(HTTPStatus.FORBIDDEN, detail=FORBIDDEN_DETAIL)


def resolve_request_bytes(object_path: str, range_header: str | None) -> int | None:
    """Return how many bytes this request reserves, or None if it is not metered.

    For CRAM files:
        the initial requests usually fetch content from the CRAM index file.
        Requests to fetch index file data some time include range header ('range': 'bytes=0-999999'), some not.
        Requests for a specific region in the CRAM file (non-index) typically includes a request
        for the actual byte range and (occasionally) another for the first 512 KB.

    For BAM files:
        Similar to CRAM files. The initial requests usually fetch content from the BAM index file.
        Requests to fetch index file data some time include range header ('range': 'bytes=0-999999'), some not.
        Requests for a specific region in the BAM file (non-index) typically includes a request
        for the actual byte range and (occasionally) another for the first 512 KB.

    For BigWig files:
        https://genome.ucsc.edu/goldenpath/help/bigWig.html
        Already indexed file.
        Requests for a specific region in the BigWig file includes a range request
        (varied ranges-does not stick to 512 KB).
    """
    request_bytes = None if range_header is None else get_byte_range(range_header)
    if request_bytes is not None:
        return request_bytes

    # Index files are served whole and unmetered; anything else without an exact byte count can't be rate-limited
    if object_path.endswith(INDEX_FILE_SUFFIXES):
        return None

    raise HTTPException(HTTPStatus.BAD_REQUEST, detail='A single closed byte range (bytes=START-END) is required.')


async def apply_rate_limit(rate_limiter: DownloadRateLimiter, request_bytes: int | None) -> bool:
    """Charge a metered request against the user's budget, raising 429 if exhausted. Return whether it was metered."""
    if request_bytes is None:
        return False

    is_allowed = await rate_limiter.evaluate_download_limits()
    if not is_allowed:
        raise HTTPException(HTTPStatus.TOO_MANY_REQUESTS)

    return True
