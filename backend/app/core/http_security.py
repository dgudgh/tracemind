from urllib.parse import urlsplit


def require_http_url(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(char in text for char in ("\r", "\n", "\t")):
        raise ValueError("访问地址无效")

    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("访问地址仅支持 HTTP/HTTPS，且必须包含主机名")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("访问地址端口无效") from exc
    return text
