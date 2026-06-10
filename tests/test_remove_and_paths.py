"""Regression tests for remove_books transactional ordering and
fix_book_paths batch pagination.

Two bugs found in the 2026-06 audit:

1. _remove_books_sqlite trashed book directories INSIDE the open DB
   transaction: a mid-loop failure rolled the DB back while earlier books'
   directories were already in the Recycle Bin — ghost books (DB rows
   pointing at deleted files). Files must only be touched after COMMIT, so
   a file-phase failure leaves intact orphan dirs instead.

2. fix_book_paths' documented pagination protocol (offset += batch_size)
   skipped still-pending fixes, because applied entries vanish from the
   next scan. Following the protocol on a 10k-fix backlog left 4,000
   unfixed while reporting more_remaining=false. next_offset must advance
   only past errored (persisting) entries.

Also covers: NCX docTitle exclusion in get_epub_toc, scalar→list coercion
in server._coerce, and the ISO-639-1→2 language map for fetch_metadata.
"""

import shutil
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from calibre_mcp.tools.write import remove_books
from calibre_mcp.tools.bulk import fix_book_paths
from calibre_mcp.tools.epub import _extract_toc_entries


# Mirrors tests/test_orphan_cleanup.py, extended with every table listed in
# backend.BOOK_CHILD_TABLES so remove_books can sweep them all.
SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Unknown',
    sort TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pubdate TIMESTAMP DEFAULT '0101-01-01 00:00:00+00:00',
    series_index REAL DEFAULT 1.0,
    author_sort TEXT,
    path TEXT NOT NULL DEFAULT '',
    uuid TEXT,
    has_cover BOOL DEFAULT 0,
    last_modified TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT, link TEXT NOT NULL DEFAULT '');
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT);
CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT NOT NULL UNIQUE, item_order INTEGER NOT NULL DEFAULT 1);
CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER NOT NULL UNIQUE, text TEXT NOT NULL DEFAULT '');
CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, type TEXT, val TEXT);
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, format TEXT, uncompressed_size INTEGER, name TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, author INTEGER NOT NULL);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, series INTEGER NOT NULL);
CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, tag INTEGER NOT NULL);
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, publisher INTEGER NOT NULL);
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, lang_code INTEGER NOT NULL);
CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, rating INTEGER NOT NULL);
CREATE TABLE books_pages_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, pagecount INTEGER);
CREATE TABLE books_plugin_data (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, name TEXT, val TEXT);
CREATE TABLE last_read_positions (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, format TEXT, user TEXT, device TEXT);
CREATE TABLE annotations (id INTEGER PRIMARY KEY, book INTEGER NOT NULL);
CREATE TABLE annotations_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL);
CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL);
CREATE TABLE conversion_options (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, format TEXT, data BLOB);
"""


def _make_backend(db_path: Path):
    """Minimal CalibreBackend-shaped object (same pattern as test_orphan_cleanup)."""
    def query_db(sql, params=None):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params or ()).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def connect_write(timeout: int = 15):
        # Skip is_calibre_running() check — we're talking to a tmp DB.
        return sqlite3.connect(str(db_path), timeout=timeout)

    return SimpleNamespace(
        db_path=db_path,
        config=SimpleNamespace(library_path=str(db_path.parent)),
        query_db=query_db,
        connect_write=connect_write,
    )


# ── remove_books: files only touched after COMMIT ──────────────────────────


@pytest.fixture
def library_with_two_books(tmp_path):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    for i in (1, 2):
        conn.execute(
            "INSERT INTO books (id, title, author_sort, path) VALUES (?,?,?,?)",
            (i, f"Book{i}", "Auth", f"Auth/Book{i} ({i})"),
        )
        conn.execute(
            "INSERT INTO data (book, format, name) VALUES (?, 'EPUB', ?)",
            (i, f"Book{i}"),
        )
    conn.commit()
    conn.close()
    for i in (1, 2):
        d = tmp_path / "Auth" / f"Book{i} ({i})"
        d.mkdir(parents=True)
        (d / f"Book{i}.epub").write_text("x")
    return _make_backend(db)


def test_remove_books_success_trashes_dirs(library_with_two_books, monkeypatch):
    backend = library_with_two_books
    import send2trash as s2t

    trashed = []

    def fake_trash(path):
        trashed.append(path)
        shutil.rmtree(path)

    monkeypatch.setattr(s2t, "send2trash", fake_trash)

    res = remove_books(backend, [1, 2], dry_run=False)
    assert res["status"] == "success"
    assert len(trashed) == 2
    assert backend.query_db("SELECT COUNT(*) AS c FROM books")[0]["c"] == 0


def test_remove_books_commits_db_even_if_file_removal_fails(
    library_with_two_books, monkeypatch, tmp_path
):
    """Regression: file deletion used to run INSIDE the DB transaction. A trash
    failure rolled the DB back after earlier directories were already gone →
    ghost books. New invariant: DB deletions are committed first; a file-phase
    failure leaves intact orphan directories and a partial_success report."""
    backend = library_with_two_books
    import send2trash as s2t

    def always_fail(path):
        raise OSError("simulated: directory locked")

    monkeypatch.setattr(s2t, "send2trash", always_fail)

    res = remove_books(backend, [1, 2], dry_run=False)

    # DB rows are gone despite every file removal failing
    assert backend.query_db("SELECT COUNT(*) AS c FROM books")[0]["c"] == 0
    assert res["status"] == "partial_success"
    assert res["file_removal_errors"] == 2
    assert all(r.get("file_removal_error") for r in res["removed"])
    # Directories still on disk with contents intact (orphans, recoverable)
    for i in (1, 2):
        assert (tmp_path / "Auth" / f"Book{i} ({i})" / f"Book{i}.epub").exists()


# ── fix_book_paths: pagination protocol completes the whole backlog ────────


def test_fix_book_paths_batch_protocol_fixes_everything(tmp_path):
    """Regression: following the documented loop (pass the returned next_offset)
    must fix ALL stale paths. The old next_offset = offset + batch_size skipped
    entries that shifted forward when applied fixes vanished from the rescan."""
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO authors (id, name, sort) VALUES (1, 'New, Author', 'New, Author')")
    n_books = 5
    for i in range(1, n_books + 1):
        stale = f"Old, Author/Book{i} ({i})"
        conn.execute(
            "INSERT INTO books (id, title, author_sort, path) VALUES (?,?,?,?)",
            (i, f"Book{i}", "New, Author", stale),
        )
        conn.execute("INSERT INTO books_authors_link (book, author) VALUES (?, 1)", (i,))
        d = tmp_path / "Old, Author" / f"Book{i} ({i})"
        d.mkdir(parents=True)
        (d / f"Book{i}.epub").write_text("x")
    conn.commit()
    conn.close()
    backend = _make_backend(db)

    offset, rounds = 0, 0
    while True:
        rounds += 1
        assert rounds < 20, "batch protocol did not converge"
        res = fix_book_paths(backend, dry_run=False, batch_size=2, offset=offset)
        if res["status"] == "no_action" or not res.get("more_remaining"):
            break
        offset = res["next_offset"]

    final = fix_book_paths(backend, dry_run=True)
    assert final["fixes_needed"] == 0, f"unfixed paths left behind: {final['fixes']}"
    for i in range(1, n_books + 1):
        assert (tmp_path / "New, Author" / f"Book{i} ({i})" / f"Book{i}.epub").exists()


# ── get_epub_toc: docTitle/docAuthor must not leak into TOC entries ─────────


def test_ncx_doctitle_and_docauthor_excluded(tmp_path):
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <docTitle><text>Cuentos completos</text></docTitle>
  <docAuthor><text>Poe, Edgar Allan</text></docAuthor>
  <navMap>
    <navPoint id="n1"><navLabel><text>El gato negro</text></navLabel><content src="a.html"/></navPoint>
    <navPoint id="n2"><navLabel><text>Ligeia</text></navLabel><content src="b.html"/></navPoint>
  </navMap>
</ncx>"""
    epub = tmp_path / "t.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("toc.ncx", ncx)

    entries, source = _extract_toc_entries(str(epub))
    assert source == "ncx"
    assert entries == ["El gato negro", "Ligeia"]


# ── server._coerce: scalar JSON for an array param gets wrapped ─────────────


def test_coerce_wraps_scalar_into_list():
    from calibre_mcp import server

    out = server._coerce("remove_books", {"book_ids": "38608"})
    assert out["book_ids"] == [38608]
    out = server._coerce("remove_books", {"book_ids": "[1, 2]"})
    assert out["book_ids"] == [1, 2]


# ── fetch_metadata: ISO-639-1 codes map to the library's ISO-639-2 keys ─────


def test_lang_iso1_to_iso3_contract():
    from calibre_mcp.tools.report import _LANG_ISO1_TO_ISO3

    assert _LANG_ISO1_TO_ISO3["es"] == "spa"
    assert _LANG_ISO1_TO_ISO3["en"] == "eng"
