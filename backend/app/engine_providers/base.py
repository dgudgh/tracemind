"""Bounded read operations; host must provide authorization, deadlines and isolation."""
from importlib import import_module
import json

class ProviderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

class ReadProvider:
    engine_id = ''
    driver = ''
    queries = {}
    identity_tokens = ()
    excluded_tokens = ()
    identity_sql = None
    identity_fields = ()

    def __init__(self, connect_factory=None):
        self.connect_factory = connect_factory

    def capabilities(self):
        return tuple({'id': key, 'access': 'read', 'parameters': {'type': 'object', 'additionalProperties': False},
                      'result': 'engine.rows/v1', 'permissions': 'select on identity and capability views',
                      'availability': 'requires_runtime_validation', 'cancellation': 'host_deadline_and_process',
                      'idempotent': True, 'row_limit': 100, 'byte_limit': 262144,
                      'effect': 'none', 'identity_assurance': ('asset_match_and_banner' if self.identity_sql else 'identity_unavailable')} for key in self.queries)

    def invoke(self, operation, config, params=None, *, expected_identity=None):
        if operation not in self.queries:
            raise ProviderError('unsupported')
        if params not in (None, {}):
            raise ProviderError('invalid_parameters')
        if not isinstance(config, dict):
            raise ProviderError('invalid_config')
        if self.identity_sql and (not isinstance(expected_identity, dict) or set(expected_identity) != set(self.identity_fields) or
                any(not isinstance(v, str) or not v.strip() or v.strip() == '0' for v in expected_identity.values())):
            raise ProviderError('expected_identity_required')
        factory = self.connect_factory
        if factory is None:
            try:
                factory = import_module(self.driver).connect
            except ImportError as exc:
                raise ProviderError('driver_unavailable') from None
        conn = cursor = None
        try:
            conn = factory(**config)
            cursor = conn.cursor()
            # Every operation independently verifies product before any additional read.
            identity = self._query(cursor, self.queries['identify'])
            if not identity or not self._identity_matches(identity[0]):
                raise ProviderError('identity_mismatch')
            if self.identity_sql is None:
                raise ProviderError('identity_unavailable')
            try:
                asset_rows = self._query(cursor, self.identity_sql)
            except Exception:
                raise ProviderError('identity_unavailable') from None
            if len(asset_rows) != 1 or any(asset_rows[0].get(k) is None or not str(asset_rows[0].get(k)).strip() or str(asset_rows[0].get(k)).strip() == '0' for k in self.identity_fields):
                raise ProviderError('identity_unavailable')
            asset = {k: str(asset_rows[0][k]).strip() for k in self.identity_fields}
            if any(asset[k] != expected_identity[k].strip() for k in self.identity_fields):
                raise ProviderError('identity_mismatch')
            rows = identity if operation == 'identify' else self._query(cursor, self.queries[operation])
            return {'schema': 'engine.rows/v1', 'engine_id': self.engine_id,
                    'capability_id': operation, 'rows': rows,
                    'product_identity_evidence': 'version_banner_only',
                    'instance_identity': asset, 'instance_identity_reason': None}
        except ProviderError:
            raise
        except Exception as exc:
            # Driver error strings can contain credentials/SQL; do not expose them.
            name = type(exc).__name__.lower()
            state = str(getattr(exc, 'sqlstate', '') or getattr(exc, 'pgcode', ''))
            code = 'permission_denied' if state == '42501' or 'permission' in name or 'privilege' in name else 'database_error'
            raise ProviderError(code) from None
        finally:
            if cursor is not None:
                try: cursor.close()
                except Exception: pass
            if conn is not None:
                try: conn.close()
                except Exception: pass

    def _query(self, cursor, sql):
        cursor.execute(sql)
        names = [str(col[0]).lower() for col in cursor.description]
        raw = cursor.fetchmany(101)
        if len(raw) > 100:
            raise ProviderError('row_limit_exceeded')
        rows = [({str(k).lower(): v for k, v in row.items()} if isinstance(row, dict)
                 else dict(zip(names, row))) for row in raw]
        try:
            encoded = json.dumps(rows, allow_nan=False, ensure_ascii=False).encode('utf-8')
        except (TypeError, ValueError):
            raise ProviderError('result_not_serializable') from None
        if len(encoded) > 262144:
            raise ProviderError('byte_limit_exceeded')
        return rows

    def _identity_matches(self, row):
        banner = str(row.get('version') or '').lower()
        return bool(banner and any(t in banner for t in self.identity_tokens)
                    and not any(t in banner for t in self.excluded_tokens))
