from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection import policy_service


FORMAT_NAME = "tracemind-inspection-template"
FORMAT_VERSION = 1
PROFILE_FORMAT_VERSION = 2
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_TEMPLATE_FILES = 100
SUPPORTED_DB_TYPES = {"oracle", "mysql", "postgresql", "sqlserver", "host"}
RUNTIME_REPORT_KEYS = {
    "instance_ids",
    "last_run_at",
    "last_status",
    "last_message",
    "next_run_time",
    "assistant_default",
    "assistant_default_db_types",
    "script_source",
    "builtin_catalog_revision",
    "builtin_items_digest",
}


class TemplateExchangeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExportArtifact:
    filename: str
    media_type: str
    content: bytes


class PortableTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_types: list[str] = Field(default_factory=list, max_length=5)
    schedule: str | None = Field(default=None, max_length=100)
    categories: list[str] = Field(default_factory=list, max_length=100)
    item_config: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    threshold_config: dict[str, Any] = Field(default_factory=dict)
    action_config: list[str] = Field(default_factory=list, max_length=100)
    report_config: dict[str, Any] = Field(default_factory=dict)
    remark: str = Field(default="", max_length=10000)

    @field_validator("db_types")
    @classmethod
    def validate_db_types(cls, values: list[str]) -> list[str]:
        invalid = sorted(set(values) - SUPPORTED_DB_TYPES)
        if invalid:
            raise ValueError(f"unsupported database types: {', '.join(invalid)}")
        return list(dict.fromkeys(values))


class TemplateMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exported_at: str = Field(..., min_length=1, max_length=64)
    product_version: str = Field(..., min_length=1, max_length=64)


class TemplateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal[FORMAT_NAME]
    format_version: Literal[FORMAT_VERSION, PROFILE_FORMAT_VERSION]
    template_id: str = Field(..., min_length=1, max_length=100)
    alias: str = Field(..., min_length=1, max_length=200)
    template: PortableTemplate
    metadata: TemplateMetadata

    @field_validator("template_id")
    @classmethod
    def validate_template_id(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("template ID contains surrounding whitespace or control characters")
        return value


@dataclass(frozen=True)
class _ParsedUpload:
    filename: str
    selection_id: str
    document: TemplateDocument | None = None
    error: str | None = None


def _portable_report_config(value: dict | None) -> dict:
    result = copy.deepcopy(value or {})
    for key in RUNTIME_REPORT_KEYS:
        result.pop(key, None)
    return result


def export_document(item, product_version: str | None = None) -> dict:
    if bool(item.read_only):
        raise TemplateExchangeError("builtin_not_exportable", "内置模板不能导出，请先复制为自定义模板")
    if product_version is None:
        from app.core.config import settings

        product_version = settings.VERSION
    from app.services.inspection import custom_metric_service, template_runtime

    try:
        item_config = custom_metric_service.normalize_item_configs(
            copy.deepcopy(item.item_config_json or [])
        )
        custom_metric_service.validate_metric_db_types(
            item_config,
            item.db_types_json or [],
        )
        template_runtime.require_template_runtime(
            item.db_types_json or [],
            item_config,
            item.report_config_json or {},
        )
    except custom_metric_service.CustomMetricError as exc:
        template_id = getattr(item, "policy_key", "") or "-"
        raise TemplateExchangeError(
            "invalid_stored_template",
            f"模板 {template_id} 的自定义指标配置无效：{exc}",
        ) from exc
    except template_runtime.TemplateRuntimeError as exc:
        template_id = getattr(item, "policy_key", "") or "-"
        raise TemplateExchangeError(
            "invalid_stored_template",
            f"模板 {template_id} 无法执行：{exc}",
        ) from exc
    try:
        document = TemplateDocument(
            format=FORMAT_NAME,
            format_version=(PROFILE_FORMAT_VERSION if (item.report_config_json or {}).get("oracle_profile", "auto") != "auto" else FORMAT_VERSION),
            template_id=item.policy_key,
            alias=item.name,
            template=PortableTemplate(
                db_types=copy.deepcopy(item.db_types_json or []),
                schedule=item.schedule,
                categories=copy.deepcopy(item.categories_json or []),
                item_config=item_config,
                threshold_config=copy.deepcopy(item.threshold_config_json or {}),
                action_config=copy.deepcopy(item.action_config_json or []),
                report_config=_portable_report_config(item.report_config_json),
                remark=item.remark or "",
            ),
            metadata=TemplateMetadata(
                exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                product_version=product_version,
            ),
        )
    except ValidationError as exc:
        raise TemplateExchangeError(
            "invalid_stored_template",
            f"模板 {getattr(item, 'policy_key', '') or '-'} 的配置不符合导出格式",
        ) from exc
    return document.model_dump(mode="json")


def _document_bytes(document: dict) -> bytes:
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def _safe_stem(value: str) -> str:
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", value)
    stem = re.sub(r"\s+", "_", stem).strip(" ._-")
    stem = stem[:120] or "inspection-template"
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    return f"_{stem}" if stem.upper() in reserved else stem


def build_export(items: list, product_version: str | None = None) -> ExportArtifact:
    if not items:
        raise TemplateExchangeError("empty_export", "请至少选择一个自定义模板")
    if len(items) > MAX_TEMPLATE_FILES:
        raise TemplateExchangeError("too_many_templates", f"单次最多导出 {MAX_TEMPLATE_FILES} 个模板")
    documents = [export_document(item, product_version=product_version) for item in items]
    payloads = [_document_bytes(document) for document in documents]
    if any(len(payload) > MAX_JSON_BYTES for payload in payloads):
        raise TemplateExchangeError("json_too_large", "单个模板导出后不能超过 2 MB")
    if len(documents) == 1:
        filename = f"{_safe_stem(documents[0]['template_id'])}.inspection-template.json"
        return ExportArtifact(filename, "application/json", payloads[0])
    if sum(len(payload) for payload in payloads) > MAX_EXPANDED_BYTES:
        raise TemplateExchangeError("zip_expanded_too_large", "模板包解压后总大小不能超过 50 MB")

    output = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for document, payload in zip(documents, payloads):
            stem = _safe_stem(document["template_id"])
            candidate = f"{stem}.inspection-template.json"
            suffix = 2
            while candidate in used_names:
                candidate = f"{stem}-{suffix}.inspection-template.json"
                suffix += 1
            used_names.add(candidate)
            archive.writestr(candidate, payload)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    content = output.getvalue()
    if len(content) > MAX_ZIP_BYTES:
        raise TemplateExchangeError("zip_too_large", "模板 ZIP 导出后不能超过 20 MB")
    return ExportArtifact(f"inspection-templates-{timestamp}.zip", "application/zip", content)


def _invalid(filename: str, selection_id: str, reason: str) -> dict:
    return {
        "filename": filename,
        "selection_id": selection_id,
        "template_id": "",
        "alias": "",
        "db_types": [],
        "item_count": 0,
        "status": "invalid",
        "reason": reason,
    }


def _validate_archive_path(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise TemplateExchangeError("unsafe_zip_path", "ZIP 中包含不安全路径")
    if len(path.parts) != 1:
        raise TemplateExchangeError("nested_zip_path", "ZIP 中只能包含顶层 JSON 文件")
    return normalized


def _read_zip(filename: str, content: bytes, expanded_limit: int) -> list[tuple[str, bytes]]:
    if len(content) > MAX_ZIP_BYTES:
        raise TemplateExchangeError("zip_too_large", "ZIP 文件不能超过 20 MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise TemplateExchangeError("invalid_zip", "ZIP 文件已损坏或格式无效") from exc

    entries: list[tuple[str, bytes]] = []
    expanded_bytes = 0
    try:
        with archive:
            for info in archive.infolist():
                safe_name = _validate_archive_path(info.filename)
                if info.is_dir():
                    raise TemplateExchangeError("zip_directory", "ZIP 中不能包含目录")
                if info.flag_bits & 0x1:
                    raise TemplateExchangeError("encrypted_zip", "不支持加密 ZIP 文件")
                if not safe_name.lower().endswith(".json"):
                    raise TemplateExchangeError("unsupported_zip_entry", "ZIP 中只能包含 JSON 模板文件")
                if len(entries) >= MAX_TEMPLATE_FILES:
                    raise TemplateExchangeError("too_many_templates", f"单次最多导入 {MAX_TEMPLATE_FILES} 个模板")
                if info.file_size > MAX_JSON_BYTES:
                    raise TemplateExchangeError("json_too_large", "单个 JSON 模板不能超过 2 MB")
                with archive.open(info, "r") as stream:
                    data = stream.read(MAX_JSON_BYTES + 1)
                if len(data) > MAX_JSON_BYTES:
                    raise TemplateExchangeError("json_too_large", "单个 JSON 模板不能超过 2 MB")
                expanded_bytes += len(data)
                if expanded_bytes > expanded_limit:
                    raise TemplateExchangeError("zip_expanded_too_large", "上传文件解压后总大小不能超过 50 MB")
                entries.append((f"{filename}::{safe_name}", data))
    except TemplateExchangeError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, OSError, EOFError, zlib.error) as exc:
        raise TemplateExchangeError("invalid_zip", "ZIP 文件成员已损坏或压缩格式不受支持") from exc
    if not entries:
        raise TemplateExchangeError("empty_zip", "ZIP 中没有模板 JSON 文件")
    return entries


def _expand_upload(filename: str, content: bytes, expanded_limit: int) -> list[tuple[str, bytes]]:
    lower_name = filename.lower()
    if lower_name.endswith(".zip"):
        return _read_zip(filename, content, expanded_limit)
    if not lower_name.endswith(".json"):
        raise TemplateExchangeError("unsupported_file", "仅支持 JSON 或 ZIP 文件")
    if len(content) > MAX_JSON_BYTES:
        raise TemplateExchangeError("json_too_large", "单个 JSON 模板不能超过 2 MB")
    if len(content) > expanded_limit:
        raise TemplateExchangeError("zip_expanded_too_large", "上传文件解压后总大小不能超过 50 MB")
    return [(filename, content)]


def _parse_document(content: bytes) -> TemplateDocument:
    from app.services.inspection import custom_metric_service, template_runtime

    if len(content) > MAX_JSON_BYTES:
        raise TemplateExchangeError("json_too_large", "单个 JSON 模板不能超过 2 MB")
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TemplateExchangeError("invalid_json", "JSON 文件格式无效") from exc
    if not isinstance(raw, dict):
        raise TemplateExchangeError("invalid_document", "JSON 模板必须是对象")
    if raw.get("format") != FORMAT_NAME:
        raise TemplateExchangeError("unsupported_format", "不是 TraceMind 巡检模板文件")
    if type(raw.get("format_version")) is not int or raw["format_version"] not in (FORMAT_VERSION, PROFILE_FORMAT_VERSION):
        raise TemplateExchangeError("unsupported_version", "不支持的模板格式版本")
    try:
        document = TemplateDocument.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", []))
        detail = first.get("msg", "字段校验失败")
        raise TemplateExchangeError("invalid_document", f"模板字段无效：{location} {detail}".strip()) from exc
    try:
        custom_metric_service.normalize_item_configs(document.template.item_config)
        custom_metric_service.validate_metric_db_types(
            document.template.item_config,
            document.template.db_types,
        )
        template_runtime.require_template_runtime(
            document.template.db_types,
            document.template.item_config,
            document.template.report_config,
        )
        if document.template.report_config.get("oracle_profile", "auto") != "auto" and document.format_version != PROFILE_FORMAT_VERSION:
            raise TemplateExchangeError("unsupported_version", "带 Oracle 适用架构的模板必须使用格式版本 2，防止旧程序忽略版本限制")
    except custom_metric_service.CustomMetricError as exc:
        raise TemplateExchangeError("invalid_custom_metric", str(exc)) from exc
    except template_runtime.TemplateRuntimeError as exc:
        raise TemplateExchangeError("template_not_executable", str(exc)) from exc
    return document


def _selection_id(index: int, filename: str, content: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(str(index).encode("ascii"))
    digest.update(b"\0")
    digest.update(filename.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(content)
    return digest.hexdigest()


def _parse_uploads(files: list[tuple[str, bytes]]) -> list[_ParsedUpload]:
    parsed: list[_ParsedUpload] = []
    expanded_count = 0
    expanded_bytes = 0
    selection_index = 0
    for filename, content in files:
        try:
            entries = _expand_upload(filename, content, MAX_EXPANDED_BYTES - expanded_bytes)
            expanded_count += len(entries)
            if expanded_count > MAX_TEMPLATE_FILES:
                raise TemplateExchangeError("too_many_templates", f"单次最多导入 {MAX_TEMPLATE_FILES} 个模板")
            for entry_name, entry_content in entries:
                selection_id = _selection_id(selection_index, entry_name, entry_content)
                selection_index += 1
                try:
                    parsed.append(_ParsedUpload(entry_name, selection_id, document=_parse_document(entry_content)))
                except TemplateExchangeError as exc:
                    parsed.append(_ParsedUpload(entry_name, selection_id, error=str(exc)))
            expanded_bytes += sum(len(entry_content) for _, entry_content in entries)
        except TemplateExchangeError as exc:
            selection_id = _selection_id(selection_index, filename, content)
            selection_index += 1
            parsed.append(_ParsedUpload(filename, selection_id, error=str(exc)))
    if not files:
        parsed.append(_ParsedUpload("", _selection_id(0, "", b""), error="请选择要导入的 JSON 或 ZIP 文件"))
    return parsed


def _preview_details(parsed: list[_ParsedUpload], existing_ids: set[str]) -> list[dict]:
    from app.services.inspection import template_runtime

    seen_ids: set[str] = set()
    items: list[dict] = []
    for item in parsed:
        if item.error or item.document is None:
            items.append(_invalid(item.filename, item.selection_id, item.error or "模板文件无效"))
            continue
        document = item.document
        runtime = template_runtime.inspect_template_runtime(
            document.template.db_types,
            document.template.item_config,
            document.template.report_config,
        )
        result = {
            "filename": item.filename,
            "selection_id": item.selection_id,
            "template_id": document.template_id,
            "alias": document.alias,
            "db_types": document.template.db_types,
            "item_count": len(document.template.item_config),
            "executable_item_count": runtime["executable_item_count"],
            "runtime_valid": runtime["valid"],
            "runtime_errors": runtime["errors"],
            "status": "ready",
            "reason": "可以导入",
        }
        if document.template_id in existing_ids:
            result.update(status="conflict", reason="模板 ID 已存在，请先删除旧模板")
        elif document.template_id in seen_ids:
            result.update(status="duplicate", reason="上传文件中模板 ID 重复，已跳过后续文件")
        else:
            seen_ids.add(document.template_id)
        items.append(result)
    return items


def preview_uploads(files: list[tuple[str, bytes]], existing_ids: set[str]) -> list[dict]:
    return _preview_details(_parse_uploads(files), existing_ids)


def _import_payload(document: TemplateDocument) -> dict:
    template = document.template
    return {
        "policy_key": document.template_id,
        "name": document.alias,
        "db_types": copy.deepcopy(template.db_types),
        "enabled": False,
        "schedule": template.schedule,
        "categories": copy.deepcopy(template.categories),
        "item_config": copy.deepcopy(template.item_config),
        "threshold_config": copy.deepcopy(template.threshold_config),
        "action_config": copy.deepcopy(template.action_config),
        "report_config": _portable_report_config(template.report_config),
        "remark": template.remark,
        "read_only": False,
    }


def import_uploads(
    db: Session,
    files: list[tuple[str, bytes]],
    selected_ids: set[str] | None = None,
) -> dict:
    existing_ids = {row[0] for row in db.query(DBInspectionPolicy.policy_key).all()}
    parsed = _parse_uploads(files)
    if selected_ids is not None:
        unknown_ids = selected_ids - {item.selection_id for item in parsed}
        if unknown_ids:
            raise TemplateExchangeError("invalid_selection", "选择项与上传文件不匹配，请重新预览")
    preview = _preview_details(parsed, existing_ids)
    imported: list[dict] = []
    results: list[dict] = []
    for parsed_item, preview_item in zip(parsed, preview):
        result = dict(preview_item)
        if preview_item["status"] != "ready" or parsed_item.document is None:
            results.append(result)
            continue
        if selected_ids is not None and parsed_item.selection_id not in selected_ids:
            result.update(status="unselected", reason="未选择导入")
            results.append(result)
            continue
        try:
            created = policy_service.create_policy(db, _import_payload(parsed_item.document))
        except IntegrityError:
            db.rollback()
            result.update(status="conflict", reason="模板 ID 已存在，请先删除旧模板")
            results.append(result)
            continue
        imported.append(created)
        result.update(status="imported", reason="导入成功", id=created["id"])
        results.append(result)

    skipped_statuses = {"conflict", "duplicate"}
    return {
        "imported_count": len(imported),
        "skipped_count": sum(item["status"] in skipped_statuses for item in results),
        "failed_count": sum(item["status"] == "invalid" for item in results),
        "not_selected_count": sum(item["status"] == "unselected" for item in results),
        "items": results,
        "imported": imported,
    }
