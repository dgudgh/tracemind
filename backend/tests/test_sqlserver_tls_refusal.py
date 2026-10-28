"""SQL Server must not pretend to honour certificate pinning.

pymssql exposes `encryption` only. An instance carrying a certificate profile
used to connect anyway: encrypted, but with no CA, client certificate or hostname
verification - while the console showed the profile as applied. Refusing the
combination is the only honest answer; silently downgrading is not.
"""

from types import SimpleNamespace

import pytest

from app.collectors.sqlserver import sqlserver_connection as module


def _instance(**overrides):
    base = dict(
        host="127.0.0.1", port=1433, username="sa", password_cipher="unused",
        database_name="master", encrypt="require", certificate_profile_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_certificate_profile_is_refused_instead_of_silently_ignored():
    with pytest.raises(ValueError) as excinfo:
        with module.sqlserver_connection(_instance(certificate_profile_id=7)):
            pass
    assert "证书" in str(excinfo.value)


def test_encryption_levels_still_parse():
    assert module._encryption_level("require") == "require"
    assert module._encryption_level("off") == "off"
    assert module._encryption_level(None) == "request"
    with pytest.raises(ValueError):
        module._encryption_level("nonsense")
