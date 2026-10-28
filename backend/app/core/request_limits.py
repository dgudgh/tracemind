"""Bounded request caching before downstream JSON and authorization readers."""
from starlette.requests import Request


ASSISTANT_BODY_LIMIT = 32768


class RequestBodyTooLarge(ValueError):
    pass


async def bounded_body(request: Request, *, limit: int) -> bytes:
    cached = getattr(request, "_body", None)
    if cached is not None:
        if len(cached) > limit:
            raise RequestBodyTooLarge()
        return cached
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        declared = None
    if declared is not None and declared > limit:
        raise RequestBodyTooLarge()
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise RequestBodyTooLarge()
        chunks.append(chunk)
    # Starlette's cached request replays _body to the downstream ASGI application.
    request._body = b"".join(chunks)
    return request._body
