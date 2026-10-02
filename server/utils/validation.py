import logging
from http import HTTPStatus

from fastapi import HTTPException

from server.services.access_list import IgvProxyAccessList
from server.services.rate_limiter import DownloadRateLimiter
from server.utils.constants import (
    AUTH_HEADER_PARTS,
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


def forbidden_detail(bucket: str) -> str:
    """Return the 403 body.

    Routes new collaborators to the fix. Naming the bucket discloses nothing (the caller typed it);
    no dataset name, since the storage prefix is configurable and a wrong one is worse than none.
    """
    return (
        f'Not authorized to read bucket `{bucket}`. If you hold personal IAM on this bucket, use '
        "IGV's native Google access instead of the proxy. Otherwise, request access by opening a PR "
        'against `cpg-infrastructure-private` adding your email to the `igv-desktop-access` list for '
        'the dataset that owns this bucket.'
    )


async def authorize_bucket_access(access_list: IgvProxyAccessList, email: str, bucket: str) -> None:
    """Refuse the request unless this user is listed against this bucket.

    Raises 403 if not permitted, or 503 (from the access list) if no map has ever loaded.
    """
    if await access_list.is_allowed(email, bucket):
        return

    logging.info(f'Refused {email} access to {bucket}: not in the IGV proxy access map.')
    raise HTTPException(HTTPStatus.FORBIDDEN, detail=forbidden_detail(bucket))


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
    if range_header is None:  # range header specified for non-index files
        if object_path.endswith(INDEX_FILE_SUFFIXES):
            return None

        # Reject the request if range is not specified as we can not rate-limit them otherwise
        raise HTTPException(HTTPStatus.BAD_REQUEST)

    request_bytes = get_byte_range(range_header)
    if request_bytes is None:
        # An index file is served whole anyway, so a Range we cannot meter is treated as none
        if object_path.endswith(INDEX_FILE_SUFFIXES):
            return None

        # Anything without an exact byte count cannot be rate-limited either
        raise HTTPException(HTTPStatus.BAD_REQUEST, detail='A single closed byte range (bytes=START-END) is required.')

    return request_bytes


async def rate_limit_if_applicable(
    rate_limiter: DownloadRateLimiter,
    request_bytes: int | None,
) -> DownloadRateLimiter | None:
    """Charge this request against the user's download budget, if it is a metered request.

    Returns the limiter for GCSStreamer, or None if unmetered — handing it over would record
    zero-byte dl_stats rows. The caller must authorize first.
    """
    if request_bytes is None:
        return None

    is_allowed = await rate_limiter.evaluate_download_limits()
    if not is_allowed:
        raise HTTPException(HTTPStatus.TOO_MANY_REQUESTS)

    return rate_limiter
