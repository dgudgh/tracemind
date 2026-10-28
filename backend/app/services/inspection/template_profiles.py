"""Portable template scope, independent of deployment paths and entry points."""
from __future__ import annotations

from app.collectors.oracle.oracle_inspection_profile import SCRIPT_CATALOG


ORACLE_TEMPLATE_KEYS = {
    "10g": "oracle_10g",
    "noncdb": "oracle_noncdb",
    "cdb": "oracle_cdb",
}
ORACLE_PROFILE_LABELS = {
    "10g": "Oracle 10g",
    "noncdb": "Oracle Non-CDB",
    "cdb": "Oracle CDB / PDB",
}


def normalize_profile_config(db_types, report_config) -> dict:
    from .template_runtime import TemplateRuntimeError

    config = dict(report_config or {})
    host_platform = str(config.get("host_platform") or "auto").strip().lower()
    if host_platform not in {"auto", "unix", "windows"}:
        raise TemplateRuntimeError("invalid_host_platform", "主机平台必须为 auto、unix 或 windows")
    if host_platform != "auto":
        if list(db_types or []) != ["host"]:
            raise TemplateRuntimeError("invalid_host_platform", "主机平台只能用于主机巡检模板")
        script = "oscheck.ps1" if host_platform == "windows" else "oscheck.sh"
        if config.get("script_name") not in (None, "", script):
            raise TemplateRuntimeError("invalid_host_script", f"当前主机平台必须使用 {script}")
        config.update(host_platform=host_platform, script_name=script)
    profile = str(config.get("oracle_profile") or "auto").strip().lower()
    if profile not in {"auto", *ORACLE_TEMPLATE_KEYS}:
        raise TemplateRuntimeError("invalid_oracle_profile", "Oracle 适用架构必须为 auto、10g、noncdb 或 cdb")
    if profile != "auto" and "oracle" not in (db_types or []):
        raise TemplateRuntimeError("invalid_oracle_profile", "Oracle 适用架构只能用于包含 Oracle 的模板")
    if "oracle_profile" in config:
        config["oracle_profile"] = profile
        if profile == "auto":
            if config.get("script_name") in {row["script_name"] for row in SCRIPT_CATALOG.values()}:
                config.pop("script_name", None)
        else:
            # Never accept an absolute source path supplied by a different installation.
            expected = SCRIPT_CATALOG[profile]["script_name"]
            if config.get("script_name") not in (None, "", expected):
                raise TemplateRuntimeError("invalid_oracle_script", f"{ORACLE_PROFILE_LABELS[profile]} 必须使用 {expected}")
            config["script_name"] = expected
    return config


def verified_oracle_profile_key(profile) -> str:
    major = profile.major_version
    if major is None:
        raise ValueError("无法识别 Oracle 版本，未执行巡检模板；请检查版本查询权限")
    if major <= 10:
        return "10g"
    if major < 12:
        return "noncdb"
    if profile.cdb not in {"YES", "NO"}:
        raise ValueError("无法确认 Oracle CDB/Non-CDB 架构，未执行巡检模板；请检查 v$database 查询权限")
    return "cdb" if profile.cdb == "YES" else "noncdb"


def require_matching_oracle_profile(profile, expected: str | None) -> str:
    actual = verified_oracle_profile_key(profile)
    if expected and expected != "auto" and actual != expected:
        raise ValueError(
            f"巡检模板不匹配：模板适用 {ORACLE_PROFILE_LABELS[expected]}，"
            f"实际数据库为 {ORACLE_PROFILE_LABELS[actual]}（{profile.version}）；未执行模板 SQL"
        )
    return actual
