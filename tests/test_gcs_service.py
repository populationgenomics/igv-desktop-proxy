from collections.abc import AsyncGenerator
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import Response
from fastapi.responses import StreamingResponse

from server.services.gcs_service import GCSStreamer


class TestGCSStreamer:
    """Test GCSStreamer class."""

    @pytest.fixture(autouse=True)
    def set_up(self, mock_httpx_client: httpx.AsyncClient):
        """Set up mock GCSStreamer."""
        self.gcs_streamer = GCSStreamer(mock_httpx_client)

    @pytest.mark.asyncio
    async def test_stream_from_gcs_success(self):
        """Test stream_from_gcs success."""
        mock_request = MagicMock(spec=httpx.Request)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = HTTPStatus.OK
        mock_response.headers = httpx.Headers({'Content-Length': '100'})

        async def async_iter() -> AsyncGenerator[bytes, None]:
            yield b'data chunk'

        mock_response.aiter_raw.return_value = async_iter()
        mock_response.aclose = AsyncMock()

        with (
            patch.object(self.gcs_streamer.httpx_client, 'build_request', return_value=mock_request),
            patch.object(self.gcs_streamer.httpx_client, 'send', new_callable=AsyncMock, return_value=mock_response),
        ):
            response = await self.gcs_streamer.stream_from_gcs(
                method='GET',
                target_url='https://test.com/bucket/path',
                headers={},
                query_params={},
            )

            assert isinstance(response, StreamingResponse)
            assert response.status_code == HTTPStatus.OK

    @pytest.mark.asyncio
    async def test_stream_from_gcs_request_error(self):
        """Test returns error when stream_from_gcs failure."""
        mock_request = MagicMock(spec=httpx.Request)
        mock_request.url = httpx.URL('https://test.com/bucket/path')

        with (
            patch.object(self.gcs_streamer.httpx_client, 'build_request', return_value=mock_request),
            patch.object(
                self.gcs_streamer.httpx_client,
                'send',
                new_callable=AsyncMock,
                side_effect=httpx.RequestError('Network error', request=mock_request),
            ),
        ):
            response = await self.gcs_streamer.stream_from_gcs(
                method='GET',
                target_url='https://test.com/bucket/path',
                headers={},
                query_params={},
            )

            assert isinstance(response, Response)
            assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR

    @pytest.mark.asyncio
    async def test_stream_from_gcs_http_status_error(self):
        """Test returns error when stream_from_gcs failed due to rejected by GCS."""
        mock_request = MagicMock(spec=httpx.Request)
        mock_request.url = httpx.URL('https://test.com/bucket/path')

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = HTTPStatus.NOT_FOUND
        mock_response.text = HTTPStatus.description
        mock_response.aread = AsyncMock()

        error = httpx.HTTPStatusError('404 Error', request=mock_request, response=mock_response)

        with (
            patch.object(self.gcs_streamer.httpx_client, 'build_request', return_value=mock_request),
            patch.object(self.gcs_streamer.httpx_client, 'send', new_callable=AsyncMock, side_effect=error),
        ):
            response = await self.gcs_streamer.stream_from_gcs(
                method='GET',
                target_url='https://test.com/bucket/path',
                headers={},
                query_params={},
            )

            assert isinstance(response, Response)
            assert response.status_code == HTTPStatus.NOT_FOUND
