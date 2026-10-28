from abc import ABC, abstractmethod
from dataclasses import dataclass


#: How far the adapter actually got. ``success`` alone cannot carry this: an adapter that
#: posts a transition and never reads the answer used to report success, and the service
#: layer closed both the platform ticket and the source alert on that claim.
OUTCOME_CONFIRMED = "confirmed_closed"   # external system re-read and reports a terminal state
OUTCOME_UNVERIFIED = "unverified"        # accepted by the remote API, final state not re-read
OUTCOME_UNKNOWN = "unknown"              # submitted, then the confirmation read failed
OUTCOME_BLOCKED = "blocked"              # refused before any change: no transition, no permission
OUTCOME_FAILED = "failed"                # transport/HTTP error


@dataclass
class TicketResult:
    success: bool
    external_id: str = ""
    external_url: str = ""
    status: str = ""
    response_body: str = ""
    error_message: str = ""
    outcome: str = OUTCOME_UNVERIFIED


class BaseTicketAdapter(ABC):
    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def test_connection(self) -> list[str]:
        pass

    @abstractmethod
    def create_ticket(self, payload: dict) -> TicketResult:
        pass

    @abstractmethod
    def get_ticket(self, external_id: str) -> TicketResult:
        pass

    @abstractmethod
    def update_ticket(self, external_id: str, payload: dict) -> TicketResult:
        pass

    @abstractmethod
    def close_ticket(self, external_id: str, payload: dict) -> TicketResult:
        pass
