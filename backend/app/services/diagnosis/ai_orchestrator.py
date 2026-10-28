"""Call one selected model, then fall back to rules without changing recipients."""
from __future__ import annotations

import ipaddress
import logging
import re
from types import SimpleNamespace
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models.ai_provider_config import AIProviderConfig
from app.models.db_instance import DBInstance
from app.services.ai_config import DIAGNOSIS_CONTRACT, call_ai_provider, inference_metadata, validate_diagnosis
from app.services.ai_degradation import AI_MODEL_FAILED, AI_NOT_CONFIGURED, degradation, manual_for
from app.services.ai_evidence import evidence_state, is_supporting_evidence
from app.services.diagnosis import hardware_profiler

logger = logging.getLogger("app.services.diagnosis.ai_orchestrator")

CONTEXT_ONLY_SOURCES = {"manual_symptom"}
CLOSE_SCORE_GAP = 0.05


def _tier_of(config: AIProviderConfig) -> str:
    """Address classification for display/order only, never an egress permission."""
    host = (urlparse(config.base_url or "").hostname or "").lower()
    if host in ("localhost", "localhost.localdomain"):
        return "private"
    try:
        ip = ipaddress.ip_address(host)
        return "private" if (ip.is_private or ip.is_loopback) else "external"
    except ValueError:
        # 非 IP（域名）→ 视为公网 API
        return "external"


def ordered_ai_configs(db: Session) -> list[AIProviderConfig]:
    """启用的 AI 配置，按 私有优先 → 默认优先 → id 排序。"""
    rows = db.query(AIProviderConfig).filter(AIProviderConfig.enabled == 1).all()
    return sorted(
        rows,
        key=lambda c: (0 if _tier_of(c) == "private" else 1, 0 if c.is_default else 1, c.id),
    )


def desensitize(text: str, instance: DBInstance) -> str:
    """掩码敏感信息：主机名、IP、实例名、用户名。"""
    out = text or ""
    secrets = [instance.host, instance.instance_name, instance.username,
               getattr(instance, "service_name", None), getattr(instance, "sid", None),
               getattr(instance, "ssh_host", None)]
    for s in secrets:
        if s and len(str(s)) >= 2:
            out = out.replace(str(s), "<MASKED>")
    # IPv4
    out = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<IP>", out)
    return out


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _root_cause_confidence_note(root_causes: list[dict]) -> str:
    if not root_causes:
        return "未生成候选根因；只能输出证据缺口和下一步采集建议。"
    top = root_causes[0]
    top_score = _as_float(top.get("total_score"))
    top_confidence = _as_float(top.get("confidence"))
    if len(root_causes) < 2:
        return (
            f"当前仅有 1 个候选根因，综合分 {top_score:.4f}，置信度 {top_confidence:.0f}。"
            "若证据不足，请明确标注为待验证结论。"
        )

    second = root_causes[1]
    second_score = _as_float(second.get("total_score"))
    gap = round(top_score - second_score, 4)
    if gap <= CLOSE_SCORE_GAP and top_confidence < 70:
        return (
            f"Top1 与 Top2 分差 {gap:.4f}（不超过阈值 {CLOSE_SCORE_GAP:.2f}），且最高置信度 {top_confidence:.0f} 低于 70。"
            "请按 Rank 顺序把前两个根因作为并列候选/主次待验证项，不要强行输出单一根因；"
            "必须列出区分它们还需要补充的证据。"
        )
    if gap <= CLOSE_SCORE_GAP:
        return (
            f"Top1 与 Top2 分差 {gap:.4f}（不超过阈值 {CLOSE_SCORE_GAP:.2f}）。"
            "请按 Rank 顺序把前两个根因作为并列候选/主次待验证项，不要强行输出单一根因；"
            "必须列出区分它们还需要补充的证据。"
        )
    if top_confidence < 70:
        return (
            f"Top1 与 Top2 分差 {gap:.4f}（超过阈值 {CLOSE_SCORE_GAP:.2f}），但最高置信度 {top_confidence:.0f} 低于 70。"
            "请把 Top1 作为暂定首选，把 Top2 作为强挑战候选；"
            "不要把暂定首选写成已确认根因，除非证据链能直接证明。"
        )
    return (
        f"Top1 相对 Top2 分差 {gap:.4f}，最高置信度 {top_confidence:.0f}。"
        "可以给出首选根因，但仍需说明反证条件。"
    )


def _split_evidence_context(evidences: list[dict]) -> tuple[list[dict], list[dict]]:
    diagnostic = []
    context = []
    for item in evidences:
        if (item.get("source") or "") in CONTEXT_ONLY_SOURCES:
            context.append(item)
        else:
            diagnostic.append(item)
    return diagnostic, context


def _prompt_evidences(evidences: list[dict]) -> list[dict]:
    diagnostic, _ = _split_evidence_context(evidences)
    return [{**row, "evidence_ref": f"e{index + 1}"} for index, row in enumerate(diagnostic[:20])]


def _supporting_evidence_ids(evidences: list[dict]) -> set[str]:
    refs = set()
    for row in _prompt_evidences(evidences):
        if is_supporting_evidence(row):
            refs.add(row["evidence_ref"])
    return refs


def build_prompt(instance: DBInstance, arch: dict, evidences: list[dict], root_causes: list[dict]) -> str:
    arch_line = f"架构画像：{arch.get('arch_type')}，标签 {arch.get('arch_tags')}，优先级 {(arch.get('priority_template') or {}).get('note')}"
    hardware_profile = (arch.get("details") or {}).get("hardware_profile")
    hardware_lines = hardware_profiler.format_for_prompt(hardware_profile)
    _, context_evidences = _split_evidence_context(evidences)
    ev_lines = []
    for e in _prompt_evidences(evidences):
        ev_lines.append(
            f"- ID {e['evidence_ref']} [{e.get('layer')}|{e.get('source')}] 执行状态：{evidence_state(e)[1]}；现象：{e.get('phenomenon')}；证据：{e.get('evidence')}；判断：{e.get('judgement')}"
        )
    context_lines = []
    for e in context_evidences[:6]:
        context_lines.append(
            f"- 人工上下文：{e.get('phenomenon')}；说明：不能作为独立故障证据，也不能作为跨源互证来源。"
        )
    rc_lines = []
    for c in root_causes[:8]:
        rc_lines.append(
            f"- Rank {c.get('rank')}: [{c.get('layer')}] {c.get('cause')}（综合分 {c.get('total_score')}，置信度 {c.get('confidence')}）"
        )
    confidence_note = _root_cause_confidence_note(root_causes)
    prompt = f"""你是资深数据库与端到端故障诊断专家（Oracle/MySQL/MES）。下面是某数据库实例的端到端诊断证据链与候选根因（已脱敏）。

【{arch_line}】

【服务器硬件画像 / 运行环境基础事实】
{hardware_lines}

【诊断证据链】
{chr(10).join(ev_lines) or '（无结构化证据）'}

【人工上下文（不能作为独立故障证据）】
{chr(10).join(context_lines) or '（无）'}

【候选根因（规则引擎按 证据分+先验+方法论匹配 已排序）】
{chr(10).join(rc_lines) or '（无）'}

【诊断置信策略】
{confidence_note}

请基于以上证据输出端到端诊断结论，结构如下：
1. 总体结论与风险等级
2. 最可能根因或并列候选（结合层级优先级、分数差距和证据强弱；分数接近时必须输出主次待验证）
3. 证据链推理（现象→证据→判断→影响）
4. 处理建议（按优先级，区分立即/计划；高危操作只给方案不自动执行）
5. 需要补充的证据或检查项

要求：不要编造证据中不存在的数据；不要把人工描述当成监控、巡检或 AWR 互证；结论必须可落地于生产运维。"""
    return desensitize(prompt, instance) + DIAGNOSIS_CONTRACT + f"\nAllowed supporting evidence IDs: {sorted(_supporting_evidence_ids(evidences))}"


def _local_summary(severity: str, root_causes: list[dict]) -> str:
    if not root_causes:
        return f"本地规则：综合风险 {severity}，未生成明确根因候选，建议补充证据后复诊。"
    primary = root_causes[0]
    lines = [f"本地规则结论：综合风险 {severity}。最可能根因 {primary.get('cause')}。"]
    for c in root_causes[1:4]:
        lines.append(f"次要候选：{c.get('cause')}（{c.get('total_score')}）")
    lines.append(f"处理建议：{primary.get('suggestion') or '按方法论库对应条目分层排查。'}")
    return "\n".join(lines)


def orchestrate(
    db: Session,
    instance: DBInstance,
    arch: dict,
    evidences: list[dict],
    root_causes: list[dict],
    severity: str,
    *, collection_incomplete: bool = False,
) -> dict:
    """Enabled configurations are not permission to retry context at another target."""
    prompt = build_prompt(instance, arch, evidences, root_causes)
    attempts = []
    evidence_ids = _supporting_evidence_ids(evidences)
    incomplete = collection_incomplete or severity == "unknown" or any(
        evidence_state(row)[1] != "success" for row in evidences if row.get("source") != "manual_symptom"
    )
    configs = ordered_ai_configs(db)
    # Freeze the chosen target once. No cross-provider or cross-context retry.
    for selected in configs[:1] if evidence_ids else []:
        config = SimpleNamespace(**{key: getattr(selected, key, None) for key in (
            "id", "provider_name", "provider_type", "base_url", "model_name", "api_key_cipher", "timeout_seconds",
        )})
        tier = _tier_of(config)
        metadata = {**inference_metadata(config, prompt), "egress_policy": "selected_provider_only_v1",
                    "evidence_manifest": _prompt_evidences(evidences),
                    "response_schema": "diagnosis-v1", "validation_status": "failed",
                    "validation_limitations": [
                        "References checked; semantic entailment and freshness not verified.",
                        "Upstream evidence may omit execution status or polarity; these are not independently verified.",
                    ]}
        try:
            text = call_ai_provider(config, prompt)
            metadata.update(getattr(text, "metadata", {}))
            result = validate_diagnosis(text, evidence_ids, risk_floor=severity, incomplete=incomplete)
            metadata["validation_status"] = "structure_and_references_validated"
            return {
                "tier": tier,
                "provider": config.provider_name,
                "model": config.model_name,
                "summary": result["summary"],
                "ai_text": str(text).strip(),
                "diagnosis": result,
                "inference": metadata,
                "attempts": attempts + [{"provider": config.provider_name, "tier": tier, "ok": True, "inference": metadata}],
            }
        except Exception as e:
            metadata.update(getattr(e, "metadata", {}))
            metadata["validation_status"] = "failed"
            error = getattr(e, "code", type(e).__name__)
            logger.warning("AI tier %s provider %s failed: %s", tier, config.provider_name, error)
            attempts.append({"provider": config.provider_name, "tier": tier, "ok": False, "error": error, "inference": metadata})

    # Local rules are the only fallback recipient.
    fallback_reason = "model_failed" if attempts else "no_supporting_evidence" if not evidence_ids else "not_configured"
    # The local rule result is authoritative here; the manual step says what to read next.
    state = {
        "model_failed": degradation(AI_MODEL_FAILED, local_result_available=True, reason_code="provider_unavailable"),
        "no_supporting_evidence": degradation(AI_MODEL_FAILED, local_result_available=True, reason_code="no_evidence"),
    }.get(fallback_reason, degradation(AI_NOT_CONFIGURED, local_result_available=True))
    return {
        "tier": "local",
        "provider": "本地规则引擎",
        "model": None,
        "summary": _local_summary(severity, root_causes if evidence_ids else []),
        "ai_text": None,
        "attempts": attempts,
        "egress_policy": "selected_provider_only_v1",
        "fallback_reason": fallback_reason,
        "degradation": state,
        "manual": manual_for(state, "diagnosis.evidence_review"),
    }
