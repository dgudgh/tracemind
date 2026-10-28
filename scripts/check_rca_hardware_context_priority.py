from __future__ import annotations

from types import SimpleNamespace

from _bootstrap import add_backend_to_path

add_backend_to_path()

from app.services.diagnosis import constants as C
from app.services.diagnosis import methodology_kb, rca_engine


class FakeSession:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def flush(self):
        return None


def evidence(layer: str, source: str, severity: str, phenomenon: str, judgement: str, weight: float = 0.7):
    return SimpleNamespace(
        layer=layer,
        source=source,
        severity=severity,
        phenomenon=phenomenon,
        judgement=judgement,
        evidence=phenomenon,
        metric_name="",
        weight=weight,
    )


def main() -> int:
    original = methodology_kb.match_methodology

    def fake_match(_db, _text, layer=None):
        if layer == C.LAYER_STORAGE:
            return (
                SimpleNamespace(
                    id=9,
                    prior_prob=0.7,
                    title="表空间使用率满",
                    judge_logic="表空间 used_pct >= 95%",
                    solution="扩容或清理。",
                ),
                0.883,
            )
        if layer == C.LAYER_DB:
            return (
                SimpleNamespace(
                    id=6,
                    prior_prob=0.55,
                    title="连接池/会话使用率异常",
                    judge_logic="连接使用率升高且有监控告警互证",
                    solution="检查连接池、活跃会话、阻塞链和异常应用来源。",
                ),
                0.95,
            )
        return None, 0.0

    methodology_kb.match_methodology = fake_match
    try:
        db = FakeSession()
        rows = rca_engine.rank_root_causes(
            db,
            incident_id=1,
            evidences=[
                evidence(C.LAYER_STORAGE, "hardware_profile", "warning", "文件系统 / 使用率 88.0%", "文件系统偏高但未达到满盘"),
                evidence(C.LAYER_DB, "prometheus", "warning", "数据库连接使用率 82%", "数据库连接使用率偏高"),
                evidence(C.LAYER_DB, "inspection", "warning", "活跃会话/连接风险", "连接池、并发突增或阻塞堆积"),
            ],
            arch_profile=SimpleNamespace(priority_template={"order": C.DEFAULT_PRIORITY}),
        )
    finally:
        methodology_kb.match_methodology = original

    if not rows or rows[0].layer != C.LAYER_DB:
        print(
            {
                "ok": False,
                "first_layer": rows[0].layer if rows else None,
                "scores": [(row.layer, row.total_score, row.cause) for row in rows],
            }
        )
        return 1
    print(
        {
            "ok": True,
            "first_layer": rows[0].layer,
            "scores": [(row.layer, row.total_score) for row in rows],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
