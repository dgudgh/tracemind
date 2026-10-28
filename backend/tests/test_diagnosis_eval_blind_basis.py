"""Rule-mode accuracy must not be presented as a blind-test result.

The rule channel groups evidence by the `cause_code` each item carries. With a
labelled dataset it reproduces the label, so the resulting top-1 number says
nothing about a real system. Rather than quietly publishing it, the run records
which basis it was computed on.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.services import diagnosis_evaluation


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _dataset(db, *, labelled: bool):
    dataset = diagnosis_evaluation.create_dataset(
        db, {"name": "口径集", "description": "t", "owner": "DBA"}, "tester",
    )
    evidence = {"id": "e1", "source": "mysql", "fact": "126 blocked sessions",
                "polarity": "supporting"}
    if labelled:
        evidence.update(cause_code="mysql_lock", cause="长事务锁等待")
    diagnosis_evaluation.add_case(db, dataset, {
        "case_key": "mysql-lock", "title": "MySQL 锁等待", "db_type": "mysql",
        "domain": "database", "severity": "critical",
        "input_json": {"evidences": [evidence]},
        "expected_root_causes": [{"cause_code": "mysql_lock", "label": "长事务锁等待", "aliases": []}],
        "required_evidence_refs": [], "counter_evidence": [], "forbidden_claims": [],
        "expected_impact": "超时", "validation_steps": ["检查"], "expert_label_by": "expert-a",
        "status": "active",
    }, "tester")
    diagnosis_evaluation.freeze_dataset(db, dataset, "tester")
    return dataset


def test_labelled_evidence_is_reported_as_label_assisted(sessions):
    with sessions() as db:
        dataset = _dataset(db, labelled=True)
        run = diagnosis_evaluation.run_evaluation(db, {
            "dataset_id": dataset.id, "mode": "rule",
            "prompt_version": "v1", "engine_version": "v1",
        }, "tester")
        detail = diagnosis_evaluation.run_detail(db, run)

    assert detail["rule_channel_basis"] == "label_assisted"
    # The number itself is still produced; what changes is the claim attached to it.
    assert detail["top1_accuracy"] == 1.0


def test_unlabelled_evidence_is_a_blind_run(sessions):
    with sessions() as db:
        dataset = _dataset(db, labelled=False)
        run = diagnosis_evaluation.run_evaluation(db, {
            "dataset_id": dataset.id, "mode": "rule",
            "prompt_version": "v1", "engine_version": "v1",
        }, "tester")
        detail = diagnosis_evaluation.run_detail(db, run)

    assert detail["rule_channel_basis"] == "blind"
    assert detail["top1_accuracy"] == 0.0, (
        "without labels the rule channel has nothing to group by - a non-zero "
        "score here would mean the answer leaked back in"
    )
