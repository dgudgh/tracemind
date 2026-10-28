from __future__ import annotations

import hashlib
import hmac
import os
import base64
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import requests

from app.core.crypto import decrypt_password
from app.core.http_security import require_http_url
from app.services.object_storage.base import ObjectStorageAdapter, validate_object_key


class S3CompatibleStorageAdapter(ObjectStorageAdapter):
    """Builds path-style URLs shared by S3-compatible providers without SDK coupling."""

    def object_location(self, object_key: str) -> str:
        key = validate_object_key(object_key)
        endpoint = (self.config.endpoint_url or "").rstrip("/")
        bucket = (self.config.bucket_name or "").strip()
        if not endpoint or not bucket:
            raise ValueError("S3 兼容存储需要 endpoint_url 和 bucket_name")
        endpoint = require_http_url(endpoint)
        if "{bucket}" in endpoint:
            return endpoint.replace("{bucket}", quote(bucket, safe="")) + "/" + quote(key, safe="/")
        return f"{endpoint}/{quote(bucket, safe='')}/{quote(key, safe='/')}"

    def read_bytes(self, object_key: str) -> bytes:
        response = self._request("GET", object_key)
        return response.content

    def write_file(
        self,
        source_path: Path,
        object_key: str,
        *,
        sha256: str,
        legal_hold: bool = False,
    ) -> dict:
        source = Path(source_path).resolve()
        headers = {"x-amz-meta-sha256": sha256}
        if legal_hold:
            headers["x-amz-object-lock-legal-hold"] = "ON"
        with source.open("rb") as handle:
            response = self._request("PUT", object_key, payload_hash=sha256, headers=headers, data=handle)
        return {
            "size_bytes": source.stat().st_size,
            "sha256": response.headers.get("x-amz-meta-sha256") or sha256,
            "etag": response.headers.get("etag"),
        }

    def download_file(self, object_key: str, target_path: Path) -> Path:
        target = Path(target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        response = self._request("GET", object_key, stream=True)
        temporary = target.with_name(f".{target.name}.downloading")
        with temporary.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    handle.write(chunk)
        temporary.replace(target)
        return target

    def stat_object(self, object_key: str) -> dict:
        response = self._request("HEAD", object_key)
        size = response.headers.get("content-length")
        return {
            "size_bytes": int(size) if size not in {None, ""} else None,
            "sha256": response.headers.get("x-amz-meta-sha256"),
            "etag": response.headers.get("etag"),
            "legal_hold": response.headers.get("x-amz-object-lock-legal-hold"),
        }

    def delete_object(self, object_key: str) -> None:
        self._request("DELETE", object_key)

    def set_legal_hold(self, object_key: str, enabled: bool) -> None:
        status = "ON" if enabled else "OFF"
        body = (
            "<LegalHold xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
            f"<Status>{status}</Status></LegalHold>"
        ).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        self._request(
            "PUT",
            object_key,
            query={"legal-hold": ""},
            payload_hash=digest,
            headers={
                "content-md5": base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode("ascii"),
                "content-type": "application/xml",
            },
            data=body,
        )

    def _request(
        self,
        method: str,
        object_key: str,
        *,
        payload_hash: str | None = None,
        headers: dict[str, str] | None = None,
        data=None,
        stream: bool = False,
        query: dict[str, str] | None = None,
    ):
        access_key = decrypt_password(self.config.access_key_cipher) if self.config.access_key_cipher else ""
        secret_key = decrypt_password(self.config.secret_key_cipher) if self.config.secret_key_cipher else ""
        if not access_key or not secret_key:
            raise ValueError("S3 兼容存储缺少 Access Key 或 Secret Key")
        url = self.object_location(object_key)
        if query:
            url = f"{url}?{urlencode(sorted(query.items()))}"
        parsed = urlsplit(url)
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        region = str(
            getattr(self.config, "region_name", "")
            or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        ).strip() or "us-east-1"
        body_hash = payload_hash or hashlib.sha256(b"").hexdigest()
        signed = {
            "host": parsed.netloc,
            "x-amz-content-sha256": body_hash,
            "x-amz-date": amz_date,
        }
        for key, value in (headers or {}).items():
            signed[key.lower()] = str(value).strip()
        signed_names = ";".join(sorted(signed))
        canonical_headers = "".join(f"{key}:{signed[key]}\n" for key in sorted(signed))
        canonical_uri = quote(unquote(parsed.path or "/"), safe="/-_.~")
        canonical_query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        canonical_request = "\n".join([
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_names,
            body_hash,
        ])
        scope = f"{date_stamp}/{region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signing_key = _signature_key(secret_key, date_stamp, region)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        request_headers = {key: value for key, value in signed.items() if key != "host"}
        request_headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        )
        response = requests.request(
            method.upper(),
            url,
            headers=request_headers,
            data=data,
            timeout=(10, 300),
            stream=stream,
            verify=bool(getattr(self.config, "verify_tls", True)),
        )
        if response.status_code >= 400:
            detail = response.text[:500] if not stream else response.reason
            response.close()
            raise RuntimeError(f"S3 请求失败 HTTP {response.status_code}: {detail}")
        return response


def _signature_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(("AWS4" + secret_key).encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
