"""Immutable fixture catalog descriptors; no registration at import."""
from __future__ import annotations

from dataclasses import dataclass, replace

from app.runtime.engine_sdk.contracts import Descriptor


@dataclass(frozen=True)
class Catalog:
    descriptors: tuple[Descriptor, ...] = ()

    def add(self, descriptor: Descriptor) -> "Catalog":
        if not descriptor.fixture_only:
            raise ValueError("catalog_requires_fixture_descriptor")
        if any(item.engine_id == descriptor.engine_id
               and item.generation == descriptor.generation for item in self.descriptors):
            raise ValueError("catalog_duplicate_generation")
        return replace(self, descriptors=(*self.descriptors, descriptor))

    def get(self, engine_id: str, generation: int | None = None) -> Descriptor | None:
        if generation is None:
            return next((item for item in self.descriptors if item.engine_id == engine_id), None)
        return next((item for item in self.descriptors
                     if item.engine_id == engine_id and item.generation == generation), None)
