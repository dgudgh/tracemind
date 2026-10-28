from datetime import datetime

from pydantic import BaseModel


class CheckResultResponse(BaseModel):
    id: int
    task_id: str
    instance_id: int
    db_type: str
    check_category: str | None
    check_item: str | None
    check_status: str | None
    metric_name: str | None
    metric_value: str | None
    threshold_value: str | None
    finding: str | None
    suggestion: str | None
    raw_data: dict | list | None
    create_time: datetime | None

    model_config = {"from_attributes": True}


class InspectionResponse(BaseModel):
    task_id: str
    instance_id: int
    db_type: str
    total: int
    warning: int
    critical: int
    results: list[CheckResultResponse]
