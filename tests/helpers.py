from collections.abc import AsyncGenerator, Callable
from http import HTTPStatus

import httpx

PROXY_TOKEN = 'proxy-sa-token'  # noqa: S105
USERINFO_HOST = 'www.googleapis.com'


class SingleChunkStream(httpx.AsyncByteStream):
    """An unconsumed response body, which is what the proxy's streaming path needs to iterate."""

    def __init__(self, data: bytes) -> None:
        """Hold the bytes to yield."""
        self._data = data

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        """Yield the whole body as one chunk."""
        yield self._data


def streaming_response(status: HTTPStatus, data: bytes = b'', **kwargs: object) -> httpx.Response:
    """Return a mock response the proxy can stream, rather than one httpx marks as already read."""
    return httpx.Response(status, stream=SingleChunkStream(data), **kwargs)  # type: ignore[arg-type]


def userinfo_response(**overrides: object) -> dict:
    """Return a valid userinfo payload."""
    payload: dict[str, object] = {
        'sub': 'user123',
        'email': 'alice@example.com',
        'email_verified': True,
    }
    payload.update(overrides)
    return payload


class ProxyTransport(httpx.MockTransport):
    """A mock transport standing in for everything the proxy talks to over HTTP.

    Routes userinfo vs GCS by host and records every request, so tests can assert nothing reached
    GCS when a request was refused.
    """

    def __init__(self) -> None:
        """Start with a valid token and a successful GCS response."""
        super().__init__(self._handle)
        self.requests: list[httpx.Request] = []
        self.userinfo: dict = userinfo_response()
        self.gcs_handler: Callable[[httpx.Request], httpx.Response] = lambda _request: streaming_response(
            HTTPStatus.OK,
            b'fake data',
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == USERINFO_HOST:
            return httpx.Response(HTTPStatus.OK, json=self.userinfo)
        return self.gcs_handler(request)

    @property
    def gcs_requests(self) -> list[httpx.Request]:
        """Return only the requests the proxy sent upstream to GCS."""
        return [request for request in self.requests if request.url.host != USERINFO_HOST]
