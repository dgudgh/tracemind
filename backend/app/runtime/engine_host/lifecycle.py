"""Bounded lifecycle/backoff/circuit policy for fixture Provider supervisors."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    VERIFYING = "verifying"
    STAGED = "staged"
    STARTING = "starting"
    READY = "ready"
    ACTIVE = "active"
    DRAINING = "draining"
    STOPPED = "stopped"
    QUARANTINED = "quarantined"
    FAILED = "failed"


@dataclass
class CrashPolicy:
    clock: callable = field(default_factory=lambda: __import__("time").monotonic)
    rng: random.Random = field(default_factory=random.Random)
    crashes: list[float] = field(default_factory=list)
    circuit_until: float | None = None

    def delay(self, now: float | None = None) -> float | None:
        now = self.clock() if now is None else now
        if self.circuit_until is not None:
            if now < self.circuit_until:
                return None
            self.circuit_until = None
            self.crashes.clear()
        self.crashes = [stamp for stamp in self.crashes if now - stamp <= 600]
        self.crashes.append(now)
        if len(self.crashes) >= 6:
            self.circuit_until = now + 900
            return None
        index = min(len(self.crashes) - 1, 5)
        base = (1, 2, 4, 8, 16, 30)[index]
        return base * self.rng.uniform(0.8, 1.2)

    def manual_reset(self) -> None:
        self.crashes.clear()
        self.circuit_until = None
