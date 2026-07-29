import json
import sqlite3
from pathlib import Path

from canopy_runner.main import verify_emdash

_SCHEMA = """
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
      archived_at TEXT, last_interacted_at TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, type TEXT DEFAULT 'task' NOT NULL
    );
"""


def _cfg(tmp_path, db_path) -> Path:
    p = tmp_path / "runner.json"
    p.write_text(json.dumps({
        "base_url": "http://x", "token": "t", "runner_id": "r-1", "emdash_db": str(db_path),
    }))
    return p


def _mkdb(tmp_path, schema=_SCHEMA):
    db = tmp_path / "emdash4.db"
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return db


def test_verify_exits_0_when_schema_intact(tmp_path, capsys):
    cfg = _cfg(tmp_path, _mkdb(tmp_path))
    assert verify_emdash(cfg) == 0
    assert "intact" in capsys.readouterr().out


def test_verify_exits_1_and_names_the_drift(tmp_path, capsys):
    db = _mkdb(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE tasks RENAME COLUMN last_interacted_at TO touched_at")
    conn.commit()
    conn.close()
    assert verify_emdash(_cfg(tmp_path, db)) == 1
    out = capsys.readouterr().out
    assert "tasks.last_interacted_at missing" in out
    assert "SILENTLY" in out  # the whole reason this check exists is called out


def test_verify_exits_2_when_db_absent(tmp_path, capsys):
    cfg = _cfg(tmp_path, tmp_path / "does-not-exist.db")
    assert verify_emdash(cfg) == 2
    assert "not found" in capsys.readouterr().out


def test_verify_exits_2_when_emdash_db_key_missing(tmp_path, capsys):
    p = tmp_path / "runner.json"
    p.write_text(json.dumps({"base_url": "http://x", "token": "t", "runner_id": "r-1"}))
    assert verify_emdash(p) == 2
    assert "emdash_db" in capsys.readouterr().out
