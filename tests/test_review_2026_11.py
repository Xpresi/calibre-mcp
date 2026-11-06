"""Regression tests for the 2026-11 review.

1. fix_book_paths merged into an existing destination by moving the free
   names and skipping the rest, then rmdir() failed on the leftover
   metadata.opf/cover.jpg — after the epub had moved. books.path kept pointing
   at the old directory: a ghost book, and a re-run stuck on the same error.
2. fetch_metadata(apply=true) wiped every tag and overwrote publisher/language
   with the API's top hit, even when that hit was a different book.
3. The safety guards failed open: an unreadable process list meant "Calibre is
   closed", and an unrecognised dry_run string became False.
4. Backups were a raw file copy that ignores pages still in the -wal file.
"""

import sqlite3
from pathlib import Path

import pytest

from calibre_mcp.backend import CalibreBackend, CalibreError
from calibre_mcp.config import Config
from calibre_mcp.tools import report
from calibre_mcp.tools.bulk import fix_book_paths, _merge_book_dir
from calibre_mcp.tools.write import _author_sort_key

from .test_remove_and_paths import SCHEMA, _make_backend


# ── 1. fix_book_paths merge is all-or-nothing ──────────────────────────────


def _stale_book_with_existing_destination(tmp_path, dest_cover: bytes):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO authors (id, name, sort) VALUES (1, 'New', 'New')")
    conn.execute(
        "INSERT INTO books (id, title, author_sort, path) VALUES (1, 'Book', 'New', 'Old/Book (1)')"
    )
    conn.execute("INSERT INTO books_authors_link (book, author) VALUES (1, 1)")
    conn.execute("INSERT INTO data (book, format, name) VALUES (1, 'EPUB', 'Book')")
    conn.commit()
    conn.close()

    src = tmp_path / "Old" / "Book (1)"
    src.mkdir(parents=True)
    (src / "Book.epub").write_bytes(b"epub")
    (src / "metadata.opf").write_bytes(b"opf")
    (src / "cover.jpg").write_bytes(b"cover")

    dst = tmp_path / "New" / "Book (1)"
    dst.mkdir(parents=True)
    (dst / "metadata.opf").write_bytes(b"opf")       # identical
    (dst / "cover.jpg").write_bytes(dest_cover)      # identical or not
    return _make_backend(db), src, dst


def test_merge_with_identical_leftovers_completes(tmp_path):
    backend, src, dst = _stale_book_with_existing_destination(tmp_path, b"cover")

    res = fix_book_paths(backend, dry_run=False)

    assert res["applied"] == 1, res
    assert not src.exists()
    assert (dst / "Book.epub").read_bytes() == b"epub"
    path = backend.query_db("SELECT path FROM books WHERE id=1")[0]["path"]
    assert path == "New/Book (1)"


def test_merge_with_conflict_moves_nothing(tmp_path):
    backend, src, dst = _stale_book_with_existing_destination(tmp_path, b"OTHER cover")

    res = fix_book_paths(backend, dry_run=False)

    assert res["applied"] == 0
    assert res["errors"] == 1
    assert "cover.jpg" in res["error_details"][0]["error"]
    # The book is still whole where the DB says it is.
    assert (src / "Book.epub").exists()
    assert not (dst / "Book.epub").exists()
    path = backend.query_db("SELECT path FROM books WHERE id=1")[0]["path"]
    assert path == "Old/Book (1)"


def test_merge_helper_refuses_directory_collisions(tmp_path):
    src, dst = tmp_path / "s", tmp_path / "d"
    (src / "data").mkdir(parents=True)
    (dst / "data").mkdir(parents=True)
    (src / "a.epub").write_bytes(b"x")

    with pytest.raises(CalibreError):
        _merge_book_dir(src, dst)
    assert (src / "a.epub").exists()


# ── 2. fetch_metadata only fills empty fields from verified matches ────────


def _book_for_fetch(tmp_path):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO authors (id, name, sort) VALUES (1, 'Julio Cortázar', 'Cortázar, Julio')")
    conn.execute("INSERT INTO tags (id, name) VALUES (1, 'Novela')")
    conn.execute(
        "INSERT INTO books (id, title, author_sort, path) VALUES (1, 'Rayuela', 'Cortázar, Julio', 'C/R (1)')"
    )
    conn.execute("INSERT INTO books_authors_link (book, author) VALUES (1, 1)")
    conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (1, 1)")
    conn.commit()
    conn.close()
    return _make_backend(db)


def test_fetch_rejects_a_different_book(tmp_path, monkeypatch):
    backend = _book_for_fetch(tmp_path)
    monkeypatch.setattr(report, "_query_openlibrary", lambda t, a: {
        "source": "openlibrary", "title": "Cooking for Beginners",
        "authors": ["Someone Else"], "publisher": "Wrong Press",
        "subjects": ["Cooking"],
    })

    res = report.fetch_metadata(backend, [1], apply=True, dry_run=False)

    assert res["applied"] == 0
    assert res["results"][0]["match_rejected"]
    assert backend.query_db("SELECT COUNT(*) AS c FROM publishers")[0]["c"] == 0


def test_fetch_keeps_existing_tags_and_fills_empty_publisher(tmp_path, monkeypatch):
    backend = _book_for_fetch(tmp_path)
    seen = {}

    def fake(title, author):
        seen["author"] = author
        return {"source": "openlibrary", "title": "Rayuela",
                "authors": ["Julio Cortazar"], "publisher": "Sudamericana",
                "subjects": ["Fiction, general"]}

    monkeypatch.setattr(report, "_query_openlibrary", fake)
    res = report.fetch_metadata(backend, [1], apply=True, dry_run=False)

    assert seen["author"] == "Julio Cortázar"  # display name, not "Cortázar, Julio"
    assert res["applied"] == 1
    assert "tags" not in res["results"][0]["applied_fields"]
    tags = backend.query_db(
        "SELECT t.name FROM tags t JOIN books_tags_link l ON l.tag = t.id WHERE l.book = 1"
    )
    assert [t["name"] for t in tags] == ["Novela"]
    pubs = backend.query_db("SELECT name FROM publishers")
    assert [p["name"] for p in pubs] == ["Sudamericana"]


# ── 3. guards fail closed ──────────────────────────────────────────────────


def _real_backend(tmp_path, backup=True):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()
    return CalibreBackend(Config(calibredb_path="calibredb", library_path=str(tmp_path),
                                 backup_before_bulk_ops=backup))


def test_process_check_failure_blocks_writes(tmp_path, monkeypatch):
    backend = _real_backend(tmp_path)
    import calibre_mcp.backend as bmod

    def boom(*a, **k):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(bmod.subprocess, "run", boom)
    with pytest.raises(CalibreError, match="Could not verify"):
        backend.require_calibre_closed()
    with pytest.raises(CalibreError):
        backend.vacuum_database()


def test_coerce_rejects_ambiguous_booleans():
    from calibre_mcp.server import _coerce

    assert _coerce("remove_books", {"book_ids": "[1]", "dry_run": "False"})["dry_run"] is False
    assert _coerce("remove_books", {"book_ids": "[1]", "dry_run": " true "})["dry_run"] is True
    with pytest.raises(ValueError):
        _coerce("remove_books", {"book_ids": "[1]", "dry_run": "dry"})


def test_auto_backup_covers_zero_and_fetch_apply():
    from calibre_mcp.server import _needs_auto_backup

    assert _needs_auto_backup("remove_books", {"dry_run": 0}) is True
    assert _needs_auto_backup("fetch_metadata", {"dry_run": False}) is False
    assert _needs_auto_backup("fetch_metadata", {"dry_run": False, "apply": True}) is True


def test_dry_run_by_default_is_ignored(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"library_path": "x", "dry_run_by_default": false}')
    loaded = Config.load(cfg)
    assert not hasattr(loaded, "dry_run_by_default")


# ── 4. backups go through SQLite and do not change journal mode ────────────


def test_backup_captures_uncheckpointed_wal_pages(tmp_path):
    backend = _real_backend(tmp_path)
    db = tmp_path / "metadata.db"
    writer = sqlite3.connect(str(db))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO t VALUES (99)")
    writer.commit()  # committed, but only in metadata.db-wal while writer is open
    try:
        backup = backend.backup_database("walcheck")
        rows = sqlite3.connect(str(backup)).execute("SELECT x FROM t ORDER BY x").fetchall()
        assert rows == [(42,), (99,)]
    finally:
        writer.close()


def test_connect_write_keeps_journal_mode(tmp_path, monkeypatch):
    backend = _real_backend(tmp_path)
    monkeypatch.setattr(backend, "is_calibre_running", lambda: False)
    backend.connect_write().close()
    mode = sqlite3.connect(str(tmp_path / "metadata.db")).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "delete"


# ── minor ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("Stephen King", "King, Stephen"),
    ("Martin Luther King Jr.", "King, Martin Luther Jr."),
    ("King, Stephen", "King, Stephen"),
    ("Homero", "Homero"),
])
def test_author_sort_key(name, expected):
    assert _author_sort_key(name) == expected
