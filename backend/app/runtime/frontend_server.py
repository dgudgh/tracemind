import logging
import os

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from app.runtime.static_paths import resolve_static_file

logger = logging.getLogger("frontend_server")


def create_frontend_app(static_dir: str, backend_port: int = 8800) -> FastAPI:
    import httpx

    app = FastAPI(docs_url=None, redoc_url=None)
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend_port}", timeout=30)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    async def proxy_api(request: Request, path: str):
        url = f"/api/{path}"
        if request.url.query:
            url += f"?{request.url.query}"

        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)
        headers["X-TraceMind-Proxy-Client-IP"] = request.client.host if request.client else ""

        try:
            resp = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers),
            )
        except Exception as e:
            logger.error("Proxy error: %s %s -> %s", request.method, url, e)
            return Response(content=f"Proxy error: {e}", status_code=502)

    if os.path.isdir(static_dir):
        assets_dir = os.path.join(static_dir, "assets")
        if os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{path:path}")
        async def serve_spa(request: Request, path: str):
            try:
                return FileResponse(resolve_static_file(static_dir, path))
            except ValueError:
                return Response(content="Forbidden", status_code=403)

    return app
