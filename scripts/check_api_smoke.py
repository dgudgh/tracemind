from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8800"
TRANSIENT_RETRY_DELAY_SECONDS = 0.5


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


@dataclass
class ProbeResult:
    path: str
    status: int | None
    elapsed_ms: int
    ok: bool
    error: str | None = None


def is_retryable_transport_error(exc: Exception) -> bool:
    return not isinstance(exc, urllib.error.HTTPError) and isinstance(
        exc,
        (
            TimeoutError,
            ConnectionResetError,
            urllib.error.URLError,
        ),
    )


def bearer_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_json(
    url: str,
    timeout: float,
    retries: int = 0,
    token: str | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=bearer_headers(token))
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
            return json.loads(payload)
        except Exception as exc:
            if attempt >= retries or not is_retryable_transport_error(exc):
                raise
            time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
    raise RuntimeError("unreachable retry state")


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    timeout: float,
    retries: int = 0,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
                return response.status, json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(text) if text else {}
            except json.JSONDecodeError:
                body = {"raw": text}
            return exc.code, body
        except Exception as exc:
            if attempt >= retries or not is_retryable_transport_error(exc):
                raise
            time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
    raise RuntimeError("unreachable retry state")


def call_get(
    url: str,
    timeout: float,
    retries: int = 0,
    token: str | None = None,
) -> tuple[int | None, str | None]:
    request = urllib.request.Request(url, headers=bearer_headers(token))
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read(4096)
                return response.status, None
        except urllib.error.HTTPError as exc:
            exc.read(4096)
            return exc.code, None
        except Exception as exc:  # noqa: BLE001 - smoke check should report every transport failure.
            if attempt < retries and is_retryable_transport_error(exc):
                time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                continue
            return None, f"{exc.__class__.__name__}: {exc}"
    return None, "retry loop exhausted"


def safe_get_paths(openapi: dict[str, Any]) -> list[str]:
    paths = []
    for path, methods in sorted((openapi.get("paths") or {}).items()):
        get_spec = (methods or {}).get("get")
        if not get_spec:
            continue
        if "{" in path or "}" in path:
            continue
        parameters = get_spec.get("parameters") or []
        if any(parameter.get("required") for parameter in parameters):
            continue
        paths.append(path)
    return paths


def normalize_include_paths(values: list[str] | None) -> set[str]:
    paths: set[str] = set()
    for value in values or []:
        for item in value.split(","):
            path = item.strip()
            if not path:
                continue
            paths.add(path if path.startswith("/") else f"/{path}")
    return paths


def login_token(base_url: str, username: str, password: str, timeout: float, retries: int = 0) -> str | None:
    status, body = request_json(
        "POST",
        f"{base_url}/api/v1/auth/login",
        {"username": username, "password": password},
        timeout,
        retries,
    )
    if status >= 400:
        return None
    token = body.get("token")
    return str(token) if token else None


def status_key(item: ProbeResult) -> str:
    if item.status is not None:
        return str(item.status)
    if item.error == "path is not a safe no-parameter GET route":
        return "not_safe_get"
    return "transport_error"


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test safe GET API routes without path or required query parameters.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Backend base URL.")
    parser.add_argument("--timeout", type=positive_float, default=5.0, help="Per-request timeout seconds.")
    parser.add_argument("--retries", type=non_negative_int, default=1, help="Retries for transient transport failures.")
    parser.add_argument("--strict", action="store_true", help="Treat any 4xx response as a failure.")
    parser.add_argument("--no-auth", action="store_true", help="Do not login before probing protected API routes.")
    parser.add_argument(
        "--include",
        action="append",
        help="Only probe specific safe GET path(s); repeat or comma-separate values, e.g. --include /api/v1/health,/api/v1/system/config.",
    )
    parser.add_argument("--username", default=os.getenv("MESGUARD_SMOKE_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("MESGUARD_SMOKE_PASSWORD"))
    args = parser.parse_args()
    if not args.no_auth and not args.password:
        parser.error("--password or MESGUARD_SMOKE_PASSWORD is required unless --no-auth is used")

    base_url = args.base_url.rstrip("/")
    token = None if args.no_auth else login_token(base_url, args.username, args.password, args.timeout, args.retries)
    if not args.no_auth and not token:
        print(json.dumps({"ok": False, "error": "login failed"}, ensure_ascii=False, indent=2))
        return 1
    try:
        openapi = fetch_json(
            f"{base_url}/openapi.json",
            args.timeout,
            args.retries,
            token=token,
        )
    except Exception as exc:  # noqa: BLE001 - this is the top-level smoke failure.
        print(json.dumps({"ok": False, "error": f"openapi unavailable: {exc}"}, ensure_ascii=False, indent=2))
        return 1

    paths = safe_get_paths(openapi)
    include_paths = normalize_include_paths(args.include)
    unmatched_includes: list[str] = []
    if include_paths:
        safe_paths = set(paths)
        unmatched_includes = sorted(include_paths - safe_paths)
        paths = [path for path in paths if path in include_paths]
    results: list[ProbeResult] = []
    for path in paths:
        started = time.perf_counter()
        status, error = call_get(f"{base_url}{path}", args.timeout, args.retries, token=token)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        ok = error is None and status is not None and (status < 500 if not args.strict else status < 400)
        results.append(ProbeResult(path=path, status=status, elapsed_ms=elapsed_ms, ok=ok, error=error))

    unmatched_results = [
        ProbeResult(path=path, status=None, elapsed_ms=0, ok=False, error="path is not a safe no-parameter GET route")
        for path in unmatched_includes
    ]
    all_results = [*results, *unmatched_results]
    failures = [item for item in all_results if not item.ok]
    status_counts: dict[str, int] = {}
    for item in all_results:
        key = status_key(item)
        status_counts[key] = status_counts.get(key, 0) + 1
    report = {
        "ok": not failures,
        "base_url": base_url,
        "authenticated": bool(token),
        "checked_count": len(results),
        "status_counts": status_counts,
        "failure_count": len(failures),
        "failures": [item.__dict__ for item in failures[:20]],
        "include_count": len(include_paths),
        "unmatched_includes": unmatched_includes,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
