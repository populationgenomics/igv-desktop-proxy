from http import HTTPStatus
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from server.services.gcs_streamer import GCSStreamer


class TestGCSStreamer:
    """Test GCSStreamer class."""

    @pytest.mark.asyncio
    async def test_stream_from_gcs_success(self, mock_download_rate_limiter: MagicMock):
        """Test stream_from_gcs success."""
        transport = httpx.MockTransport(
            handler=lambda _req: httpx.Response(HTTPStatus.OK, headers={'Content-Length': '100'}),
        )
        client = httpx.AsyncClient(transport=transport)
        gcs_streamer = GCSStreamer(client, mock_download_rate_limiter)
        response = await gcs_streamer.stream_from_gcs(
            method='GET',
            target_url='https://test.com/bucket/path',
            headers={},
            query_params={},
            bucket_name='bucket',
        )

        assert isinstance(response, StreamingResponse)
        assert response.status_code == HTTPStatus.OK

        await client.aclose()
        mock_download_rate_limiter.refund.assert_not_called()
        mock_download_rate_limiter.record_download_stats.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_from_gcs_raise_error(self, mock_download_rate_limiter: MagicMock):
        """Test returns error Response when stream_from_gcs fail on server side."""

        def network_error_handler(req: httpx.Request) -> httpx.Response:
            raise httpx.RequestError('Network error', request=req)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler=network_error_handler))
        gcs_streamer = GCSStreamer(client, mock_download_rate_limiter)
        with pytest.raises(HTTPException) as exc:
            await gcs_streamer.stream_from_gcs(
                method='GET',
                target_url='https://test.com/bucket/path',
                headers={},
                query_params={},
                bucket_name='bucket',
            )
        assert exc.value.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        await client.aclose()
        mock_download_rate_limiter.refund.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_from_gcs_http_status_error(self, mock_download_rate_limiter: MagicMock):
        """Test forward the error Response when stream_from_gcs fail on error response from server."""
        transport = httpx.MockTransport(
            handler=lambda _req: httpx.Response(HTTPStatus.NOT_FOUND, text=HTTPStatus.NOT_FOUND.description),
        )
        client = httpx.AsyncClient(transport=transport)
        gcs_streamer = GCSStreamer(client, mock_download_rate_limiter)
        with pytest.raises(HTTPException):
            await gcs_streamer.stream_from_gcs(
                method='GET',
                target_url='https://test.com/bucket/path',
                headers={},
                query_params={},
                bucket_name='bucket',
            )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_stream_from_gcs_request_error(self, mock_download_rate_limiter: MagicMock):
        """Test raises HTTP 500 when a network-level RequestError occurs."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.RequestError('Network error', request=req)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler=handler))
        gcs_streamer = GCSStreamer(client, mock_download_rate_limiter)
        with pytest.raises(HTTPException) as exc:
            await gcs_streamer.stream_from_gcs('GET', 'https://test.com/bucket/path', {}, {}, bucket_name='bucket')
        assert exc.value.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        await client.aclose()
        mock_download_rate_limiter.refund.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_from_gcs_unexpected_error(self, mock_download_rate_limiter: MagicMock):
        """Test raises HTTP 500 when an unexpected non-httpx exception occurs."""

        def handler(_req: httpx.Request) -> httpx.Response:
            raise ValueError('Unexpected error')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler=handler))
        gcs_streamer = GCSStreamer(client, mock_download_rate_limiter)
        with pytest.raises(HTTPException) as exc:
            await gcs_streamer.stream_from_gcs('GET', 'https://test.com/bucket/path', {}, {}, bucket_name='bucket')
        assert exc.value.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        await client.aclose()
        mock_download_rate_limiter.refund.assert_called_once()
