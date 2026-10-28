from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class NotifyResult:
    success: bool
    status_code: int = 0
    response_body: str = ""
    error_message: str = ""


class BaseAdapter(ABC):
    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def send(self, recipient: str, title: str, content: str) -> NotifyResult:
        pass

    @abstractmethod
    def validate_config(self) -> list[str]:
        pass
