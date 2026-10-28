"""License write path must not destroy a working license.

``_build_license_info`` (the read path) refuses an unsigned, forged, wrong-machine or
expired license. ``save_license_to_db`` used to delete the stored row and insert
whatever it was handed. So a rejected upload still wiped the working license and left
the whole platform behind LicenseMiddleware with no way back except manual DB surgery.
"""

import base64
import json
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.license import License

THIS_MACHINE = "MACHINE-A"
OTHER_MACHINE = "MACHINE-B"


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture
def license_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'license.sqlite'}")
    License.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("app.core.database.SessionLocal", factory)
    monkeypatch.setattr("app.license.verify.generate_machine_code", lambda: THIS_MACHINE)
    try:
        yield factory
    finally:
        engine.dispose()


def _sign(payload: dict, private_key) -> dict:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    return {**payload, "signature": base64.b64encode(signature).decode("utf-8")}


def _payload(machine_code=THIS_MACHINE, expire_date="2099-12-31"):
    return {
        "machine_code": machine_code,
        "customer": "验收客户",
        "product": "TraceMind AI 智能运维平台",
        "expire_date": expire_date,
        "features": ["core"],
    }


def _store_existing_license(factory, keypair):
    """Seed the working license the fix has to protect, and return its stored shape."""
    signed = _sign(_payload("MACHINE-WORKING"), keypair[0])
    with factory() as db:
        db.query(License).delete()
        db.add(License(
            machine_code=signed["machine_code"], customer=signed["customer"],
            product=signed["product"], expire_date=signed["expire_date"],
            features=json.dumps(signed["features"], ensure_ascii=False),
            signature=signed["signature"],
        ))
        db.commit()
    return _stored_license(factory)


def _stored_license(factory):
    with factory() as db:
        row = db.query(License).order_by(License.id.desc()).first()
        return None if row is None else {
            "machine_code": row.machine_code,
            "customer": row.customer,
            "product": row.product,
            "expire_date": row.expire_date,
            "features": json.loads(row.features) if row.features else [],
            "signature": row.signature,
        }


def _save(license_db, keypair, payload, *, public_key_missing=False):
    from app.license.verify import save_license_to_db

    key = None if public_key_missing else keypair[1]
    with patch("app.license.verify._load_public_key", return_value=key):
        return save_license_to_db(payload)


def _rejected_and_preserved(license_db, keypair, payload, expected_reason, *,
                            public_key_missing=False):
    before = _store_existing_license(license_db, keypair)
    result = _save(license_db, keypair, payload, public_key_missing=public_key_missing)
    assert result["ok"] is False, result
    assert expected_reason in result["message"], result
    assert _stored_license(license_db) == before


def test_unsigned_payload_is_rejected_and_keeps_the_working_license(license_db, keypair):
    _rejected_and_preserved(license_db, keypair, _payload(), "no_signature")


def test_forged_signature_is_rejected_and_keeps_the_working_license(license_db, keypair):
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _sign(_payload(), attacker_key)
    _rejected_and_preserved(license_db, keypair, forged, "signature_invalid")


def test_tampered_payload_after_signing_is_rejected(license_db, keypair):
    signed = _sign(_payload(), keypair[0])
    signed["customer"] = "篡改后的客户"
    _rejected_and_preserved(license_db, keypair, signed, "signature_invalid")


def test_license_for_another_machine_is_rejected_and_keeps_the_working_license(license_db, keypair):
    """Validly signed, but unusable here -- storing it would disable the platform."""
    signed = _sign(_payload(OTHER_MACHINE), keypair[0])
    _rejected_and_preserved(license_db, keypair, signed, "machine_mismatch")


def test_expired_license_is_rejected_and_keeps_the_working_license(license_db, keypair):
    signed = _sign(_payload(expire_date="2000-01-01"), keypair[0])
    _rejected_and_preserved(license_db, keypair, signed, "expired")


def test_unparseable_expire_date_is_rejected(license_db, keypair):
    signed = _sign(_payload(expire_date="not-a-date"), keypair[0])
    _rejected_and_preserved(license_db, keypair, signed, "expire_date_invalid")


def test_missing_public_key_fails_closed(license_db, keypair):
    _rejected_and_preserved(license_db, keypair, _sign(_payload(), keypair[0]),
                            "no_public_key", public_key_missing=True)


def test_correctly_signed_and_usable_payload_still_replaces_the_stored_license(license_db, keypair):
    _store_existing_license(license_db, keypair)
    signed = _sign(_payload(), keypair[0])
    result = _save(license_db, keypair, signed)
    assert result["ok"] is True
    stored = _stored_license(license_db)
    assert stored["machine_code"] == THIS_MACHINE
    assert stored["signature"] == signed["signature"]


def test_write_path_accepts_exactly_what_the_read_path_accepts(license_db, keypair):
    """The two paths must agree, otherwise one of them is lying about validity."""
    from app.license import verify as verify_module

    valid = _sign(_payload(), keypair[0])
    wrong_machine = _sign(_payload(OTHER_MACHINE), keypair[0])
    with patch.object(verify_module, "_load_public_key", return_value=keypair[1]):
        assert verify_module.license_payload_issues(valid) is None
        assert verify_module.license_payload_issues(wrong_machine) == "machine_mismatch"
        # The write path stores nothing for the payload the read path would reject.
        assert _save(license_db, keypair, wrong_machine)["ok"] is False


def test_embedded_public_key_is_loadable():
    """The shipped public key must parse, otherwise every license fails closed."""
    from app.license.verify import _load_public_key

    assert _load_public_key() is not None
