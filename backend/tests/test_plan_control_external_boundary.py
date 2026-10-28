"""C07: local control never pretends to be an external stop/fence."""
from __future__ import annotations

import pytest

from app.services import plan_control as control
from test_capability_plan import ADMIN, db  # noqa: F401
from test_plan_control import admit, factory  # noqa: F401


def test_external_stop_and_fence_are_advertised_unsupported(db):
    _plan, _batch, execution_id = admit(db)
    view = control.read_control(db, execution_id, actor=ADMIN)
    assert view["scope"] == "local_step_boundary"
    assert view["external_stop"] == "unsupported"
    assert view["external_fence"] == "unsupported"


@pytest.mark.parametrize("action", ["external_stop", "external_fence", "kill", "terminate"])
def test_external_actions_are_rejected_without_touching_the_execution(db, factory, action):
    _plan, _batch, execution_id = admit(db)
    with pytest.raises(control.ControlError, match="invalid_control_request"):
        control.request_control(db, execution_id, actor=ADMIN, action=action,
                                expected_control_version=0, idempotency_key="external-attempt",
                                reason="attempt an external stop")
    with factory() as fresh:
        from app.models.capability_execution import DBCapabilityExecution
        execution = fresh.get(DBCapabilityExecution, execution_id)
        assert execution.state == "queued"
        assert execution.control_version == 0
