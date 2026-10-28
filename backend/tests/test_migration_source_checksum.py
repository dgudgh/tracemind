from types import SimpleNamespace

from app.migrations import contract


def test_source_checksum_changes_even_if_revision_id_is_reused(tmp_path, monkeypatch):
    source = tmp_path / "migration.py"
    source.write_text("def upgrade(connection): pass\n")
    module = SimpleNamespace(__file__=str(source), __name__="test.migration", REVISION="same-revision")
    monkeypatch.setattr(contract.runner, "MIGRATIONS", (module,))
    before = contract._source_checksum()
    compatible_axis = contract._checksum(contract._revision_ids())
    source.write_text("def upgrade(connection): connection.execute('new ddl')\n")
    assert contract._source_checksum() != before
    assert contract._checksum(contract._revision_ids()) == compatible_axis
