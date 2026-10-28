from dataclasses import dataclass


@dataclass
class Finding:
    check_category: str
    check_item: str
    check_status: str
    metric_name: str
    metric_value: str
    threshold_value: str
    finding: str
    suggestion: str
    raw_data: dict | list | None = None
