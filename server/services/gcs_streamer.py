import logging
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import httpx
from fastapi import HTTPException, Response
from fastapi.responses import StreamingResponse

from server.services.rate_limiter import DownloadRateLimiter


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
                logging.error(f'HTTP error {exc.response.status_code} while requesting {exc.request.url!r}.')
                raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text) from exc
            if isinstance(exc, httpx.RequestError):
                logging.error(f'An error occurred while requesting {exc.request.url!r}. {exc}')
                raise HTTPException(status_code=HTTPStatus.INTERNAL_SERVER_ERROR) from exc

            logging.error(f'Unexpected error: {exc}')
            raise HTTPException(status_code=HTTPStatus.INTERNAL_SERVER_ERROR) from exc
