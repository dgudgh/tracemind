"""Escaped report rendering for recorded slow-SQL model calls."""
import html
import json


def _literal(value):
    return "\n".join("    " + line for line in str(value or "").splitlines())


def source_markdown(source, watermark=None):
    detail = source.get("analysis_detail") or {}
    evidence = detail.get("evidence") or {}
    model = detail.get("model_result") or {}
    provider = detail.get("provider") or {}
    lines = ["# 慢 SQL AI 分析", "",
             f"报告：{source.get('id') or source.get('source_id') or '-'}；实例 ID：{source.get('instance_id', '-')}；数据库：{source.get('db_type') or '-'}", "",
             _literal(f"标题：{source.get('title') or '-'}\n实例名称：{source.get('instance_name') or '-'}"), "",
             f"状态：{detail.get('status')}；模型：{provider.get('model')}；完成时间：{detail.get('finished_at')}", "",
             "模型输出是待验证的分析建议，不是已确认根因。本报告未执行任何优化或业务库变更。", "",
             "## 模型分析结论（待验证文本）", "", _literal(detail.get("error") or model.get("summary") or "未取得有效结论"), ""]
    for hypothesis in model.get("hypotheses") or []:
        lines += ["### 待验证假设", _literal(hypothesis["cause"]), _literal(f"证据：{', '.join(hypothesis['evidence_ids'])}"),
                  _literal(f"验证：{hypothesis['validation']}"), ""]
    for label, values in (("优化建议", model.get("recommendations")), ("缺失证据", model.get("missing_evidence")),
                          ("采样限制", evidence.get("limitations"))):
        lines += [f"## {label}", "", *[_literal(value) for value in values or []], ""]
    for label, value in (("证据身份与指标", {key: value for key, value in evidence.items() if key not in {"sql_text", "plan_text"}}),
                         ("SQL 原文", evidence.get("sql_text")), ("执行计划", evidence.get("plan_text") or "未采集")):
        # Indented code cannot be closed by a fence embedded in untrusted SQL.
        text = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else str(value or "")
        lines += [f"## {label}", "", *["    " + line for line in text.splitlines()], ""]
    lines += [f"证据 SHA-256：{detail.get('evidence_sha256')}"]
    if watermark:
        lines += ["", _literal(watermark)]
    return "\n".join(lines)


def source_html(source, watermark=None):
    title = html.escape(str(source.get("title") or "慢 SQL AI 分析"))
    content = html.escape(source_markdown(source, watermark))
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>'
            '<style>body{max-width:1100px;margin:32px auto;padding:0 24px;color:#202124;font:14px/1.7 system-ui}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}h1{font-size:24px}</style></head>'
            f'<body><h1>{title}</h1><pre>{content}</pre></body></html>')
