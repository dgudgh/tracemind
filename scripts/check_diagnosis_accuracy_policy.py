#!/usr/bin/env python3
from __future__ import annotations

import os
from types import SimpleNamespace

from _bootstrap import add_backend_to_path

os.environ.setdefault("DEBUG", "false")
add_backend_to_path()

from app.services.diagnosis import ai_orchestrator, rca_engine
from app.services.diagnosis import constants as C


def main() -> int:
    inspection = SimpleNamespace(
        layer=C.LAYER_SQL,
        source="inspection",
        phenomenon="慢 SQL 风险",
        judgement="Slow_queries 大于 0",
        evidence="rnd_next_to_key_ratio=15.5",
        metric_name="rnd_next_to_key_ratio",
        severity="warning",
        weight=0.6,
    )
    manual = SimpleNamespace(
        layer=C.LAYER_SQL,
        source="manual_symptom",
        phenomenon="客户要求做端到端 AI 分析验证",
        judgement="作为上下文，不单独作为故障证据",
        evidence="测试描述",
        metric_name="customer_symptom",
        severity="info",
        weight=0.55,
    )

    diagnostic_items = rca_engine._diagnostic_items([inspection, manual])
    diagnostic_sources = rca_engine._diagnostic_sources([inspection, manual])
    assert diagnostic_items == [inspection]
    assert diagnostic_sources == {"inspection"}

    root_causes = [
        {
            "rank": 1,
            "layer": C.LAYER_DB,
            "cause": "[数据库层] 行锁争用 / 阻塞会话",
            "total_score": 0.3864,
            "confidence": 67,
        },
        {
            "rank": 2,
            "layer": C.LAYER_SQL,
            "cause": "[SQL层] 高逻辑读 / 全表扫描慢 SQL",
            "total_score": 0.3822,
            "confidence": 67,
        },
    ]
    note = ai_orchestrator._root_cause_confidence_note(root_causes)
    assert "并列候选" in note
    assert "不要强行输出单一根因" in note

    instance = SimpleNamespace(host="192.0.2.10", instance_name="db01", username="app")
    prompt = ai_orchestrator.build_prompt(
        instance,
        {"arch_type": "单机", "arch_tags": [], "priority_template": {"note": "网络 > 存储 > 内存 > CPU"}},
        [
            {
                "layer": C.LAYER_SQL,
                "source": "inspection",
                "phenomenon": "慢 SQL 风险",
                "evidence": "Slow_queries > 0",
                "judgement": "SQL 性能相关指标异常",
            },
            {
                "layer": C.LAYER_SQL,
                "source": "manual_symptom",
                "phenomenon": "用户要求测试 AI 分析",
                "evidence": "人工输入",
                "judgement": "上下文",
            },
        ],
        root_causes,
    )
    assert "并列候选" in prompt
    assert "人工上下文" in prompt
    assert "不能作为独立故障证据" in prompt
    assert "[SQL层|manual_symptom]" not in prompt
    print("diagnosis accuracy policy ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
