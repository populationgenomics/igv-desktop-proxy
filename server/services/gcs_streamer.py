import logging
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import httpx
from fastapi import HTTPException, Response
from fastapi.responses import StreamingResponse

from server.services.rate_limiter import DownloadRateLimiter

# Upstream statuses that are a legitimate answer to the user's question, rather than a fault in
# the proxy's own access. Everything else — notably 401/403/5xx — becomes a 502, because after the
# credential swap an upstream refusal means this proxy's IAM is broken and must be alertable.
PASS_THROUGH_STATUSES: dict[int, str] = {
    HTTPStatus.NOT_FOUND: 'Object not found.',
    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE: 'Requested range not satisfiable.',
}


def map_upstream_error(upstream_status: int) -> tuple[int, str]:
    """Return the (status, body) this proxy answers with for a given upstream failure."""
    detail = PASS_THROUGH_STATUSES.get(upstream_status)
    if detail is not None:
        return upstream_status, detail
    return HTTPStatus.BAD_GATEWAY.value, 'Upstream storage request failed.'


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
                # Never relay the upstream body. The only principal GCS sees is this proxy's own
                # service account, so its error XML names that account and confirms the bucket exists.
                status, detail = map_upstream_error(upstream_status)
                raise HTTPException(status_code=status, detail=detail) from exc
            if isinstance(exc, httpx.RequestError):
                logging.error(f'An error occurred while requesting {exc.request.url!r}. {exc}')
                raise HTTPException(status_code=HTTPStatus.INTERNAL_SERVER_ERROR) from exc

            logging.error(f'Unexpected error: {exc}')
            raise HTTPException(status_code=HTTPStatus.INTERNAL_SERVER_ERROR) from exc
