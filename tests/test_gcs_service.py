from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from server.services.gcs_service import GCSStreamer


class TestGCSStreamer:
    """Test GCSStreamer class."""

    @pytest.mark.asyncio
    @patch.object(httpx.AsyncClient, 'send', new_callable=AsyncMock)
    async def test_stream_from_gcs_success(
        self,
        mock_send: AsyncMock,
        mock_httpx_client: httpx.AsyncClient,
        mock_download_rate_limiter: MagicMock,
    ):
        """Test stream_from_gcs success."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = HTTPStatus.OK
        mock_response.headers = httpx.Headers({'Content-Length': '100'})
        mock_send.return_value = mock_response

        gcs_streamer = GCSStreamer(mock_httpx_client, mock_download_rate_limiter)
        response = await gcs_streamer.stream_from_gcs(
            method='GET',
            target_url='https://test.com/bucket/path',
            headers={},
            query_params={},
        )

        assert isinstance(response, StreamingResponse)
        assert response.status_code == HTTPStatus.OK

    @pytest.mark.asyncio
    @patch.object(httpx.AsyncClient, 'send', new_callable=AsyncMock)
    @patch.object(httpx.AsyncClient, 'build_request')
    async def test_stream_from_gcs_request_error(
        self,
        mock_build_request: AsyncMock,
        mock_send: AsyncMock,
        mock_httpx_client: httpx.AsyncClient,
        mock_download_rate_limiter: MagicMock,
    ):
        """Test returns error Response when stream_from_gcs fail on server side."""
        mock_request = MagicMock(spec=httpx.Request)
        mock_request.url = httpx.URL('https://test.com/bucket/path')

        mock_build_request.return_value = mock_request
        mock_send.side_effect = httpx.RequestError('Network error', request=mock_request)

        gcs_streamer = GCSStreamer(mock_httpx_client, mock_download_rate_limiter)
        with pytest.raises(HTTPException):
            await gcs_streamer.stream_from_gcs(
                method='GET',
                target_url='https://test.com/bucket/path',
                headers={},
                query_params={},
            )

    @pytest.mark.asyncio
    @patch.object(httpx.AsyncClient, 'send', new_callable=AsyncMock)
    @patch.object(httpx.AsyncClient, 'build_request')
    async def test_stream_from_gcs_http_status_error(
        self,
        mock_build_request: AsyncMock,
        mock_send: AsyncMock,
        mock_httpx_client: httpx.AsyncClient,
        mock_download_rate_limiter: MagicMock,
    ):
        """Test forward the error Response when stream_from_gcs fail on error response from server."""
        mock_request = MagicMock(spec=httpx.Request)
        mock_request.url = httpx.URL('https://test.com/bucket/path')

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = HTTPStatus.NOT_FOUND
        mock_response.text = HTTPStatus.NOT_FOUND.description

        error = httpx.HTTPStatusError('404 Error', request=mock_request, response=mock_response)

        mock_build_request.return_value = mock_request
        mock_send.side_effect = error

        gcs_streamer = GCSStreamer(mock_httpx_client, mock_download_rate_limiter)
        with pytest.raises(HTTPException):
            await gcs_streamer.stream_from_gcs(
                method='GET',
                target_url='https://test.com/bucket/path',
                headers={},
                query_params={},
            )
