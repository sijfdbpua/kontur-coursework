import sqlite3
import pytest
from scripts.backup import backup


def test_backup_includes_committed_wal_and_can_be_restored(tmp_path):
    source, output = tmp_path / 'source.sqlite', tmp_path / 'copy.sqlite'
    with sqlite3.connect(source) as live:
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('CREATE TABLE records (value TEXT)')
        live.execute('INSERT INTO records VALUES (?)', ('Заявка сохранена',))
        live.commit()
        backup(source, output)
        with sqlite3.connect(output) as restored:
            assert restored.execute('SELECT value FROM records').fetchone()[0] == 'Заявка сохранена'
            assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_existing_destination_is_preserved(tmp_path):
    source, output = tmp_path / 'source.sqlite', tmp_path / 'existing.txt'
    source.touch()
    output.write_text('Не изменять')
    with pytest.raises(FileExistsError):
        backup(source, output)
    assert output.read_text() == 'Не изменять'
    with pytest.raises(FileExistsError):
        backup(source, source)


def test_missing_source_does_not_create_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup(tmp_path / 'missing.sqlite', tmp_path / 'copy.sqlite')
    assert not list(tmp_path.iterdir())
