import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import StreamingResponse

# Application constants
DEFAULT_PORT = 8080
GCS_BASE_URL = 'storage-download.googleapis.com'
REQUEST_URL_PARTS = 2
CLIENT_TIMEOUT = 5  # httpx default timeout 5 secs

logging.getLogger().setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Define application lifespan.

    https://fastapi.tiangolo.com/advanced/events/#startup-and-shutdown-together.
    """
    app.state.httpx_client = httpx.AsyncClient(timeout=CLIENT_TIMEOUT)
    yield
    await app.state.httpx_client.aclose()


def get_httpx_client(request: Request) -> httpx.AsyncClient:
    """Retrieve the shared httpx.AsyncClient instance."""
    return request.app.state.httpx_client


app = FastAPI(lifespan=lifespan)


@app.api_route('/health', methods=['GET'])
async def health_check(_request: Request):
    """Return health check response."""
    return Response(status_code=200, content='OK')


@app.api_route('/{full_path:path}', methods=['GET', 'HEAD'])
async def proxy_handler(request: Request, full_path: str, client: httpx.AsyncClient = Depends(get_httpx_client)):
    """Proxy requests to GCS."""
    path_segments = full_path.split('/', 1)
    if len(path_segments) < REQUEST_URL_PARTS:
        return Response(content='Invalid path format. Use /bucket/path', status_code=400)

    bucket = path_segments[0]
    object_path = path_segments[1]
    target_url = httpx.URL(scheme='https', host=f'{bucket}.{GCS_BASE_URL}', path=f'/{object_path}')

    headers = {}
    if 'authorization' in request.headers:
        headers['Authorization'] = request.headers['authorization']
    if 'range' in request.headers:
        headers['Range'] = request.headers['range']

    try:
        req = client.build_request(
            method=request.method,
            url=target_url,
            headers=headers,
            params=request.query_params,
        )

        gcs_response = await client.send(req, stream=True)
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
        logging.error(f'An error occurred while requesting {exc.request.url!r}. {exc}')
        await exc.response.aread()
        return Response(status_code=exc.response.status_code, content=exc.response.text)
    except Exception as e:  # noqa: BLE001
        logging.error(f'Unexpected error: {e}')
        return Response(status_code=500, content='Internal Server Error')


if __name__ == '__main__':
    logging.info('##### Started reverse proxying server #####')

    uvicorn.run(
        'server.main:app',
        host='localhost',
        port=DEFAULT_PORT,
    )
