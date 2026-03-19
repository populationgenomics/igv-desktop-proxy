from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Response
from pytest_asyncio import fixture
from starlette.testclient import TestClient


class TestMainAPI:
    """Test desktop proxy API endpoints."""

    @fixture(autouse=True)
    def set_up(self, mock_proxy_api: TestClient):
        """Set up the test client."""
        self.proxy_api = mock_proxy_api

    def test_health_check(self):
        """Test health check endpoint."""
        response = self.proxy_api.get('/health')
        assert response.status_code == HTTPStatus.OK
        assert 'OK' in response.text

    def test_proxy_invalid_path_format(self):
        """Test return Error when invalid bucket path format is passed."""
        response = self.proxy_api.get('/mybucket')
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert 'Invalid path format' in response.text

    def test_proxy_missing_auth_header(self):
        """Test return Error when authorization header is missing."""
        response = self.proxy_api.get('/mybucket/path/to/file.cram', headers={'Range': 'bytes=0-100'})
        assert response.status_code == HTTPStatus.BAD_REQUEST

    def test_proxy_invalid_auth_header(self):
        """Test return Error when authorization header format is invalid."""
        headers = {'Authorization': 'InvalidToken', 'Range': 'bytes=0-100'}
        response = self.proxy_api.get('/mybucket/path/to/file.cram', headers=headers)
        assert response.status_code == HTTPStatus.BAD_REQUEST

    def test_proxy_missing_range_header_for_non_index_files(self):
        """Test return Error when range header is missing when requesting data from non index files."""
        headers = {'Authorization': 'Bearer token123'}
        response = self.proxy_api.get('/mybucket/path/to/file.cram', headers=headers)
        assert response.status_code == HTTPStatus.BAD_REQUEST

    @patch('server.main.GCSStreamer')
    def test_proxy_success_index_file(self, mock_gcs_streamer_cls: MagicMock):
        """Test return success when requesting data from index file."""
        mock_gcs_streamer = mock_gcs_streamer_cls.return_value
        mock_gcs_response = Response(status_code=HTTPStatus.OK, content=b'fake data')
        mock_gcs_streamer.stream_from_gcs = AsyncMock(return_value=mock_gcs_response)

        headers = {'Authorization': 'Bearer token123'}
        response = self.proxy_api.get('/mybucket/path/to/file.crai', headers=headers)

        assert response.status_code == HTTPStatus.OK
        assert response.content == b'fake data'

    @patch('server.main.apply_rate_limit_if_applicable')
    @patch('server.main.GCSStreamer')
    def test_proxy_success_with_range_limit(self, mock_gcs_streamer_cls: MagicMock, mock_rate_limiter_cls: MagicMock):
        """Test return success when requesting data from non-index file."""
        mock_gcs_streamer = mock_gcs_streamer_cls.return_value
        mock_gcs_response = Response(status_code=HTTPStatus.PARTIAL_CONTENT, content=b'partial data')
        mock_gcs_streamer.stream_from_gcs = AsyncMock(return_value=mock_gcs_response)

        # Mock Rate Limiter
        mock_limiter = mock_rate_limiter_cls.return_value
        mock_limiter.check_user_limit = AsyncMock(return_value=True)

        headers = {
            'Authorization': 'Bearer token123',
            'Range': 'bytes=0-1024',
        }
        response = self.proxy_api.get('/mybucket/path/to/file.cram', headers=headers)

        assert response.status_code == HTTPStatus.PARTIAL_CONTENT
        assert response.content == b'partial data'
