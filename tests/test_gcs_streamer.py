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

    @staticmethod
    async def stream_upstream_status(status: HTTPStatus, body: str, rate_limiter: MagicMock) -> HTTPException:
        """Drive a request that upstream answers with the given status, and return the raised error."""
        transport = httpx.MockTransport(handler=lambda _req: httpx.Response(status, text=body))
        client = httpx.AsyncClient(transport=transport)
        gcs_streamer = GCSStreamer(client, rate_limiter)
        try:
            with pytest.raises(HTTPException) as exc:
                await gcs_streamer.stream_from_gcs(
                    method='GET',
                    target_url='https://test.com/bucket/path',
                    headers={},
                    query_params={},
                    bucket_name='bucket',
                )
        finally:
            await client.aclose()
        return exc.value

    @pytest.mark.asyncio
    async def test_upstream_404_passes_through_with_an_opaque_body(self, mock_download_rate_limiter: MagicMock):
        """Test a missing object is a legitimate user-facing answer, with GCS's own body withheld."""
        upstream_body = '<Error><Details>proxy-sa@project.iam.gserviceaccount.com denied</Details></Error>'
        error = await self.stream_upstream_status(HTTPStatus.NOT_FOUND, upstream_body, mock_download_rate_limiter)

        assert error.status_code == HTTPStatus.NOT_FOUND
        assert 'gserviceaccount' not in str(error.detail)

    @pytest.mark.asyncio
    async def test_upstream_416_passes_through(self, mock_download_rate_limiter: MagicMock):
        """Test an unsatisfiable range reaches IGV, which acts on it."""
        error = await self.stream_upstream_status(
            HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
            'range too big',
            mock_download_rate_limiter,
        )
        assert error.status_code == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE

    @pytest.mark.asyncio
    async def test_upstream_403_becomes_502(self, mock_download_rate_limiter: MagicMock):
        """Test an upstream refusal is a gateway fault now: the proxy's own IAM is broken."""
        upstream_body = '<Error><Details>proxy-sa@project.iam.gserviceaccount.com has no access</Details></Error>'
        error = await self.stream_upstream_status(HTTPStatus.FORBIDDEN, upstream_body, mock_download_rate_limiter)

        assert error.status_code == HTTPStatus.BAD_GATEWAY
        assert 'gserviceaccount' not in str(error.detail)

    @pytest.mark.asyncio
    async def test_upstream_401_becomes_502(self, mock_download_rate_limiter: MagicMock):
        """Test an upstream auth failure is never relayed as a user denial."""
        error = await self.stream_upstream_status(HTTPStatus.UNAUTHORIZED, 'nope', mock_download_rate_limiter)
        assert error.status_code == HTTPStatus.BAD_GATEWAY

    @pytest.mark.asyncio
    async def test_upstream_5xx_becomes_502(self, mock_download_rate_limiter: MagicMock):
        """Test an upstream server error is reported as a gateway fault."""
        error = await self.stream_upstream_status(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            'boom',
            mock_download_rate_limiter,
        )
        assert error.status_code == HTTPStatus.BAD_GATEWAY

    @pytest.mark.asyncio
    async def test_upstream_error_refunds_the_reserved_bytes(self, mock_download_rate_limiter: MagicMock):
        """Test budget reserved for a request GCS refused is handed back."""
        await self.stream_upstream_status(HTTPStatus.FORBIDDEN, 'nope', mock_download_rate_limiter)
        mock_download_rate_limiter.refund.assert_called_once()

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
