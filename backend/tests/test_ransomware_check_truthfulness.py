"""`check_ransomware` must not report "passed" for checks it could not run.

The endpoint is ``POST /api/v1/oracle/{id}/ransomware-check``. Every one of its five
queries hits a ``dba_*`` view, so an instance monitored by a business / limited-privilege
account fails all five. The original implementation wrapped each query in
``except Exception: pass`` and then concluded "勒索病毒检查通过 / 未发现明显勒索病毒痕迹"
whenever no findings had been collected -- i.e. it rendered "could not look" as
"looked, and it is clean". On a security check that is a false negative, and it is the
one shape the project's own convention forbids ("连接失败不得生成健康结论").

The connection itself is *not* the problem: ``oracle_connection`` still raises, so an
unreachable instance never reaches the conclusion (pinned below). The defect is the
narrower and more common case -- connected, but the catalog views are not readable.

``check_ransomware`` had zero test coverage before this file, which is why the
conflation survived. Same story as ``delete_user`` earlier in this round.
"""

import logging
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models.db_instance import DBInstance
from app.services import parameters

CONNECT_PATCH = "app.services.parameters.oracle_connection"

# The five labels in execution order, matching the queries in check_ransomware.
CHECK_LABELS = (
    "大量表近期被分析",
    "大量新表创建",
    "无效对象",
    "近期新建活跃用户",
    "表空间使用率>95%",
)

# The frontend maps risk -> colour with a fixed dictionary
# (OracleSecurityCheckPage.jsx: riskColor). Anything outside this set renders an
# undefined tag colour, so the fix must stay inside it rather than invent "unknown".
FRONTEND_RISK_VOCABULARY = {"normal", "info", "warning", "critical"}

FINDING_KEYS = {"check", "count", "risk", "detail", "users", "tablespaces", "unavailable_checks"}


def _instance() -> DBInstance:
    return DBInstance(
        instance_name="db-1", db_type="oracle", host="10.0.0.10", port=1521,
        username="monitor", password_cipher="encrypted", enabled=1,
        service_name="MES",
    )


class _ScriptedCursor:
    """One scripted outcome per ``execute`` so a test fails exactly the queries it means to.

    An outcome is either an exception to raise or the rows to return.
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self._rows = []
        self.description = [("CNT",)]
        self.executed = 0

    def execute(self, sql, params=None):  # noqa: ARG002 - signature mirrors the driver
        if self.executed >= len(self._outcomes):
            raise AssertionError(
                f"check_ransomware issued more than the {len(self._outcomes)} queries this test scripted"
            )
        outcome = self._outcomes[self.executed]
        self.executed += 1
        if isinstance(outcome, BaseException):
            raise outcome
        self._rows = outcome

    def fetchall(self):
        return self._rows


@contextmanager
def _connection(cursor):
    class _Conn:
        def cursor(self):
            return cursor

    yield _Conn()


def _run(outcomes):
    cursor = _ScriptedCursor(outcomes)
    with patch(CONNECT_PATCH, lambda *_a, **_k: _connection(cursor)):
        return parameters.check_ransomware(_instance())


def _checks(result) -> list[str]:
    return [item["check"] for item in result["findings"]]


def _risks(result) -> set[str]:
    return {item["risk"] for item in result["findings"]}


def test_an_unqueryable_instance_is_not_reported_as_passed():
    """The core regression: five failed queries must not add up to a clean bill of health."""
    denied = PermissionError("ORA-00942: table or view does not exist")
    result = _run([denied] * 5)

    assert "勒索病毒检查通过" not in _checks(result)
    assert "normal" not in _risks(result), "no check ran, so nothing may be marked healthy"

    unavailability = [f for f in result["findings"] if f.get("unavailable_checks")]
    assert len(unavailability) == 1
    summary = unavailability[0]
    assert summary["risk"] == "warning"
    assert summary["count"] == 5
    assert {entry["check"] for entry in summary["unavailable_checks"]} == set(CHECK_LABELS)
    assert all(entry["error"] == str(denied) for entry in summary["unavailable_checks"])


def test_a_fully_healthy_instance_still_reports_passed():
    """Guard the normal path: quiet catalogues must keep producing the familiar verdict."""
    result = _run([[(0,)], [(0,)], [(0,)], [], []])

    assert _checks(result) == ["勒索病毒检查通过"]
    assert _risks(result) == {"normal"}
    assert result["instance"] == "db-1"


def test_a_partial_failure_keeps_the_real_findings_and_flags_the_gap():
    """The two are independent: a detected risk and an unrunnable check both get reported."""
    denied = PermissionError("ORA-00942: table or view does not exist")
    # check 1 fails, check 2 finds 100 newly created tables, the rest are quiet.
    result = _run([denied, [(100,)], [(0,)], [], []])

    assert "大量新表创建" in _checks(result)
    assert "勒索病毒检查通过" not in _checks(result)

    unavailability = [f for f in result["findings"] if f.get("unavailable_checks")]
    assert len(unavailability) == 1
    assert unavailability[0]["count"] == 1
    assert unavailability[0]["unavailable_checks"][0]["check"] == "大量表近期被分析"


def test_each_unexecuted_check_is_logged_with_its_reason(caplog):
    """A silent guard would leave an operator unable to tell why the check was skipped."""
    denied = PermissionError("ORA-00942: table or view does not exist")
    with caplog.at_level(logging.WARNING, logger="app.services.parameters"):
        _run([denied, denied, [(0,)], [], []])

    messages = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(messages) == 2, messages
    assert "大量表近期被分析" in messages[0]
    assert "大量新表创建" in messages[1]
    assert "ORA-00942" in messages[0]


def test_a_connection_failure_still_propagates_instead_of_becoming_a_verdict():
    """An unreachable instance must raise, not answer -- this half was always correct."""
    def _explode(*_a, **_k):
        raise ConnectionError("ORA-01034: ORACLE not available")

    with patch(CONNECT_PATCH, _explode):
        with pytest.raises(ConnectionError):
            parameters.check_ransomware(_instance())


def test_the_response_shape_stays_within_the_frontend_vocabulary():
    """Interface compatibility: the page reads ``check`` / ``detail`` and colours by ``risk``."""
    denied = PermissionError("ORA-00942: table or view does not exist")
    scenarios = [
        _run([denied] * 5),
        _run([[(0,)], [(0,)], [(0,)], [], []]),
        _run([denied, [(100,)], [(0,)], [], []]),
    ]

    for result in scenarios:
        assert set(result) == {"instance", "findings"}
        assert result["findings"], "an empty findings list renders a blank card"
        for finding in result["findings"]:
            assert set(finding) <= FINDING_KEYS
            assert finding["risk"] in FRONTEND_RISK_VOCABULARY
            assert isinstance(finding["count"], int)
            assert isinstance(finding["detail"], str) and finding["detail"]
