from pydantic import BaseModel


class PageResponse(BaseModel):
    items: list
    total: int
    page: int
    page_size: int


class MessageResponse(BaseModel):
    message: str


class ConnectionTestResponse(BaseModel):
    ok: bool
    message: str
    detail: dict | None = None
