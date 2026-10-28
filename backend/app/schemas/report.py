from datetime import datetime

from pydantic import BaseModel


class ReportGenerateRequest(BaseModel):
    task_id: int
    instance_id: int | None = None
    output_format: str = "markdown"


class ReportResponse(BaseModel):
    title: str
    format: str
    content: str
    generated_at: datetime
