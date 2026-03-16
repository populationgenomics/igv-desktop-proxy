import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Response
from fastapi.responses import StreamingResponse


class GCSStreamer:
    """Handles streaming data from Google Cloud Storage."""

    def __init__(self, httpx_client: httpx.AsyncClient) -> None:
        """Initialize client."""
        self.httpx_client = httpx_client

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

        except httpx.RequestError as exc:
            logging.error(f'An error occurred while requesting {exc.request.url!r}. {exc}')
            return Response(status_code=500, content='Internal Server Error')

        except httpx.HTTPStatusError as exc:
            logging.error(f'HTTP error {exc.response.status_code} while requesting {exc.request.url!r}.')
            await exc.response.aread()
            return Response(status_code=exc.response.status_code, content=exc.response.text)

        except Exception as e:  # noqa: BLE001
            logging.error(f'Unexpected error: {e}')
            return Response(status_code=500, content='Internal Server Error')
