import logging
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import httpx
from fastapi import HTTPException, Response
from fastapi.responses import StreamingResponse

from server.services.rate_limiter import DownloadRateLimiter
from server.utils.constants import PASS_THROUGH_STATUSES, UPSTREAM_FAILED_DETAIL


def map_upstream_error(upstream_status: int) -> tuple[int, str]:
    """Return the (status, body) this proxy answers with for a given upstream failure."""
    detail = PASS_THROUGH_STATUSES.get(upstream_status)
    if detail is not None:
        return upstream_status, detail
    return HTTPStatus.BAD_GATEWAY.value, UPSTREAM_FAILED_DETAIL


class GCSStreamer:
    """Handles streaming data from Google Cloud Storage."""

    def __init__(self, httpx_client: httpx.AsyncClient, rate_limiter: DownloadRateLimiter | None) -> None:
        """Initialize clients."""
        self.httpx_client = httpx_client
        self.rate_limiter = rate_limiter

    async def stream_from_gcs(
        self,
        method: str,
        target_url: str | httpx.URL,
        headers: dict[str, str],
        query_params: Any,
        bucket_name: str,
    ) -> Response:
        """Fetch data and streams the response back to the client."""
        try:
            req = self.httpx_client.build_request(
                method=method,
                url=target_url,
                headers=headers,
                params=query_params,
            )

            gcs_response = await self.httpx_client.send(req, stream=True)
            gcs_response.raise_for_status()

            # https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse
            async def stream_wrapper() -> AsyncIterator[bytes]:
                try:
                    async for chunk in gcs_response.aiter_raw():
                        yield chunk
                finally:
                    await gcs_response.aclose()

            if self.rate_limiter is not None:
                await self.rate_limiter.record_download_stats(bucket_name)

            return StreamingResponse(
                stream_wrapper(),
                status_code=gcs_response.status_code,
                headers=dict(gcs_response.headers),
            )

        except Exception as exc:
            if self.rate_limiter is not None:
                # refund the requested_bytes back to the user's download quota as the request to GCS failed
                await self.rate_limiter.refund()

            if isinstance(exc, httpx.HTTPStatusError):
                await exc.response.aread()
                upstream_status = exc.response.status_code
                logging.error(
                    f'HTTP error {upstream_status} while requesting {exc.request.url!r}. '
                    f'Upstream body: {exc.response.text}',
                )
                # Never relay the upstream body: it names the proxy's service account
                status, detail = map_upstream_error(upstream_status)
                raise HTTPException(status_code=status, detail=detail) from exc
            if isinstance(exc, httpx.RequestError):
                logging.error(f'An error occurred while requesting {exc.request.url!r}. {exc}')
                # GCS unreachable or timed out: an upstream failure, so the same alertable 502
                raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=UPSTREAM_FAILED_DETAIL) from exc

            logging.error(f'Unexpected error: {exc}')
            raise HTTPException(status_code=HTTPStatus.INTERNAL_SERVER_ERROR) from exc
