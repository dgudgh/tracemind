"""Immutable catalog model. Package verification and activation remain host responsibilities."""
from dataclasses import dataclass, replace
import re

NATIVE_PUBLISHER = 'tracemind'
NATIVE_IDS = frozenset({'oracle', 'mysql', 'postgresql', 'sqlserver'})
KNOWN = (('oracle', 'oracle', 'oracle'), ('mysql', 'mysql', 'mysql'),
         ('postgresql', 'postgresql', 'postgresql'), ('sqlserver', 'sqlserver', 'tds'),
         ('dm', 'dm', 'dm'), ('kingbasees', 'kingbase', 'postgresql'))

@dataclass(frozen=True)
class Entry:
    engine_id: str
    family: str
    protocol: str
    publisher: str | None = None
    version: str | None = None
    capabilities: tuple[str, ...] = ()
    available: bool = False
    reason: str | None = 'not_installed'
    package_digest: str | None = None
    runtime_digest: str | None = None
    sdk_version: str | None = None
    fixture_only: bool = False

@dataclass(frozen=True)
class Catalog:
    generation: int
    entries: tuple[Entry, ...]

    def __post_init__(self):
        if type(self.generation) is not int or self.generation < 1 or not isinstance(self.entries, tuple):
            raise ValueError('catalog_invalid')
        if any(not isinstance(item, Entry) for item in self.entries):
            raise ValueError('catalog_invalid')
        if len({item.engine_id for item in self.entries}) != len(self.entries):
            raise ValueError('catalog_duplicate_engine')

    @classmethod
    def empty(cls):
        return cls(1, tuple(Entry(*item) for item in KNOWN))

    def get(self, engine_id):
        return next((item for item in self.entries if item.engine_id == engine_id), None)

    def register(self, entry: Entry, *, verified: bool):
        """verified must come from the host signature/trust verifier, never package input."""
        if not isinstance(entry, Entry):
            raise ValueError('descriptor_invalid')
        existing = self.get(entry.engine_id)
        if existing is None:
            raise ValueError('engine_unregistered')
        if verified is not True:
            raise ValueError('package_unverified')
        if entry.engine_id in NATIVE_IDS and entry.publisher != NATIVE_PUBLISHER:
            raise ValueError('native_identity_protected')
        if existing.publisher and entry.publisher != existing.publisher:
            raise ValueError('publisher_identity_protected')
        if (entry.family, entry.protocol) != (existing.family, existing.protocol):
            raise ValueError('identity_mismatch')
        if (not isinstance(entry.publisher, str) or not entry.publisher or
                not isinstance(entry.version, str) or not entry.version or
                not isinstance(entry.capabilities, tuple) or
                any(not isinstance(c, str) or not c for c in entry.capabilities) or
                len(set(entry.capabilities)) != len(entry.capabilities)):
            raise ValueError('descriptor_invalid')
        reasons = {'not_installed', 'disabled', 'isolated', 'incompatible',
                   'driver_unavailable', 'unsupported', 'requires_runtime_validation', 'fixture_only'}
        if type(entry.available) is not bool or (entry.available and entry.reason is not None) or (not entry.available and entry.reason not in reasons):
            raise ValueError('availability_invalid')
        if existing == entry:
            return self
        return Catalog(self.generation + 1, tuple(entry if e.engine_id == entry.engine_id else e for e in self.entries))

    def register_installed(self, repository, digest):
        """Ingest only a current, fully verified repository artifact, never a request bool.

        Installation does not assert driver/target readiness. A new extension
        requires no edit to KNOWN; identity comes from the external scoped key.
        """
        with repository.guard_installed(digest) as verified:
            manifest = verified['manifest']
            engine_id = manifest['engine_id']
            if (not isinstance(engine_id, str) or len(engine_id) > 64 or
                    re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*', engine_id) is None):
                raise ValueError('engine_identity_invalid')
            if engine_id in NATIVE_IDS and manifest['publisher'] != NATIVE_PUBLISHER:
                raise ValueError('native_identity_protected')
            current = self.get(engine_id)
            family = manifest.get('family', current.family if current else engine_id)
            protocol = manifest.get('protocol', current.protocol if current else engine_id)
            if any(not isinstance(x, str) or not x or len(x) > 64 for x in (family, protocol)):
                raise ValueError('descriptor_invalid')
            capabilities = manifest.get('capabilities', ())
            if (not isinstance(capabilities, (list, tuple)) or len(capabilities) > 256 or
                    any(type(c) is not str or not c or len(c) > 200 for c in capabilities) or
                    len(set(capabilities)) != len(capabilities)):
                raise ValueError('descriptor_invalid')
            candidate = Entry(engine_id, family, protocol, manifest['publisher'], manifest['version'],
                tuple(capabilities), False, 'requires_runtime_validation', digest,
                manifest['runtime_digest'], manifest['sdk_version'],
                bool(verified.get('fixture_only') or manifest.get('fixture_only')))
            target = self if current else Catalog(self.generation, (*self.entries, Entry(engine_id, family, protocol)))
            return target.register(candidate, verified=True)

# Only the four canonical legacy database values are mapped. Unknown is retained.
def resolve_legacy(value):
    normalized = value.strip().lower() if isinstance(value, str) else None
    return {'original': value, 'engine_id': normalized if normalized in NATIVE_IDS else None}
