import pytest
from app.engine_registry import Catalog, Entry, resolve_legacy


def test_snapshot_identity_protection_and_unavailable():
    old = Catalog.empty()
    assert old.get('oracle').reason == 'not_installed'
    assert old.get('kingbasees').protocol == 'postgresql'
    entry = Entry('oracle', 'oracle', 'oracle', 'tracemind', '1', ('identify',), True, None)
    new = old.register(entry, verified=True)
    assert old.generation == 1 and not old.get('oracle').available
    assert new.generation == 2 and new.get('oracle').available
    with pytest.raises(ValueError, match='native_identity_protected'):
        old.register(Entry('oracle', 'oracle', 'oracle', 'evil', '1'), verified=True)
    with pytest.raises(ValueError, match='package_unverified'):
        old.register(entry, verified=False)
    with pytest.raises(ValueError, match='identity_mismatch'):
        old.register(Entry('kingbasees', 'postgresql', 'postgresql', 'vendor', '1'), verified=True)
    with pytest.raises(ValueError, match='engine_unregistered'):
        old.register(Entry('fake', 'postgresql', 'postgresql', 'vendor', '1'), verified=True)


def test_legacy_bridge_does_not_guess():
    assert resolve_legacy(' MySQL ') == {'original': ' MySQL ', 'engine_id': 'mysql'}
    for value in ('DomesticDB', 'mariadb', 'pg', 'kingbasees', None, ''):
        assert resolve_legacy(value) == {'original': value, 'engine_id': None}


def test_extension_publisher_cannot_change_and_metadata_is_immutable():
    old = Catalog.empty()
    dm = Entry('dm', 'dm', 'dm', 'dm-vendor', '1', ('identify',), True, None)
    new = old.register(dm, verified=True)
    with pytest.raises(ValueError, match='publisher_identity_protected'):
        new.register(Entry('dm', 'dm', 'dm', 'other', '2'), verified=True)
    with pytest.raises(ValueError, match='descriptor_invalid'):
        old.register(Entry('dm', 'dm', 'dm', 'dm-vendor', '1', (['mutable'],)), verified=True)


def test_catalog_rejects_mutable_or_duplicate_snapshots():
    with pytest.raises(ValueError, match='catalog_invalid'): Catalog(1, [])
    entry = Catalog.empty().get('mysql')
    with pytest.raises(ValueError, match='catalog_duplicate_engine'): Catalog(1, (entry, entry))
    with pytest.raises(ValueError, match='catalog_invalid'): Catalog(0, ())


def test_unavailable_reason_is_machine_readable():
    old = Catalog.empty()
    with pytest.raises(ValueError, match='availability_invalid'):
        old.register(Entry('dm', 'dm', 'dm', 'vendor', '1', (), False, 'arbitrary'), verified=True)
    new = old.register(Entry('dm', 'dm', 'dm', 'vendor', '1', (), False, 'driver_unavailable'), verified=True)
    assert new.get('dm').reason == 'driver_unavailable'
