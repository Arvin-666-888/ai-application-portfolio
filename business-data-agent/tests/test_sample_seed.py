import hashlib
import sqlite3


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sample_seed_is_idempotent_when_contract_is_satisfied(monkeypatch, tmp_path):
    from app import main

    sample_path = tmp_path / "sample.db"
    monkeypatch.setattr(main.settings, "SAMPLE_DB_PATH", str(sample_path))

    main._init_sample_data()
    first_hash = _sha256(sample_path)
    connection = sqlite3.connect(f"file:{sample_path.as_posix()}?mode=ro", uri=True)
    try:
        first_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in main._SAMPLE_DATA_CONTRACT
        }
    finally:
        connection.close()

    main._init_sample_data()
    second_hash = _sha256(sample_path)
    connection = sqlite3.connect(f"file:{sample_path.as_posix()}?mode=ro", uri=True)
    try:
        second_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in main._SAMPLE_DATA_CONTRACT
        }
    finally:
        connection.close()

    assert first_counts == second_counts
    assert first_hash == second_hash
