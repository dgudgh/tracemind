from app.schemas.ai import AIDiagnosisRequest, AIDiagnosisResponse
from app.schemas.check_result import CheckResultResponse, InspectionResponse
from app.schemas.common import ConnectionTestResponse, MessageResponse, PageResponse
from app.schemas.db_instance import (
    DBInstanceCreate,
    DBInstanceResponse,
    DBInstanceUpdate,
)
from app.schemas.report import ReportGenerateRequest, ReportResponse
from app.schemas.slow_sql import SlowSQLResponse
from app.schemas.operations import (
    ArchiveCleanupPlanRequest,
    OracleOperationRequest,
    TablespaceExpansionPlanRequest,
)

__all__ = [
    "AIDiagnosisRequest",
    "AIDiagnosisResponse",
    "CheckResultResponse",
    "ConnectionTestResponse",
    "DBInstanceCreate",
    "DBInstanceResponse",
    "DBInstanceUpdate",
    "InspectionResponse",
    "MessageResponse",
    "PageResponse",
    "ReportGenerateRequest",
    "ReportResponse",
    "SlowSQLResponse",
    "ArchiveCleanupPlanRequest",
    "OracleOperationRequest",
    "TablespaceExpansionPlanRequest",
]
