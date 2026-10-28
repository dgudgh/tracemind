from __future__ import annotations
import random
from app.runtime.engine_host.lifecycle import CrashPolicy


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


def test_backoff_jitter_and_circuit():
    clock = Clock()
    policy = CrashPolicy(clock=clock, rng=random.Random(7))
    delays = [policy.delay() for _ in range(6)]
    assert delays[0] is not None and 0.8 <= delays[0] <= 1.2
    assert delays[5] is None
    assert policy.circuit_until == 900
    clock.now = 901
    assert policy.delay() is not None
    assert policy.circuit_until is None
