"""Regression tests for orphan-row cleanup after metadata writes.

Triggered by a post-mortem DB diff that found a series row ("El Millonario del
Mes") left orphan after a book had its series re-set, plus a missing 'eng'
language row whose 13 books had been switched to 'spa' in bulk. Both pointed at
the same root cause: _set_metadata_sqlite re-linked relational fields without
pruning the rows it left behind.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from calibre_mcp.tools.write import _set_metadata_sqlite, bulk_set_metadata


CALIBRE_SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Unknown',
    sort TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pubdate TIMESTAMP DEFAULT '0101-01-01 00:00:00+00:00',
    series_index REAL DEFAULT 1.0,
    author_sort TEXT,
    isbn TEXT DEFAULT '',
    lccn TEXT DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    flags INTEGER DEFAULT 1,
    uuid TEXT,
    has_cover BOOL DEFAULT 0,
    last_modified TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE authors (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT, link TEXT NOT NULL DEFAULT ''
);
CREATE TABLE series (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT
);
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
"""


def _make_backend(db_path: Path):
    """Build a minimal CalibreBackend-shaped object that wires _set_metadata_sqlite."""
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


@pytest.fixture
def db_with_book(tmp_path):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(CALIBRE_SCHEMA)
    # One book linked to a unique series, publisher, tag and language.
    conn.execute("INSERT INTO books (id, title, path) VALUES (1, 'Test', 'a/t (1)')")
    conn.execute("INSERT INTO series (id, name) VALUES (10, 'OldSeries')")
    conn.execute("INSERT INTO books_series_link (book, series) VALUES (1, 10)")
    conn.execute("INSERT INTO publishers (id, name) VALUES (20, 'OldPublisher')")
    conn.execute("INSERT INTO books_publishers_link (book, publisher) VALUES (1, 20)")
    conn.execute("INSERT INTO tags (id, name) VALUES (30, 'OldTag')")
    conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (1, 30)")
    conn.execute("INSERT INTO languages (id, lang_code) VALUES (40, 'eng')")
    conn.execute("INSERT INTO books_languages_link (book, lang_code) VALUES (1, 40)")
    conn.commit()
    conn.close()
    return db


def _orphans(db, table, link_table, link_col):
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute(
            f"SELECT name FROM {table} WHERE id NOT IN (SELECT {link_col} FROM {link_table})"
            if table != "languages"
            else f"SELECT lang_code FROM {table} WHERE id NOT IN (SELECT {link_col} FROM {link_table})"
        ).fetchall()]
    finally:
        conn.close()


def test_set_metadata_swapping_series_cleans_old(db_with_book):
    """Re-pointing the only book in a series must not leave the old series row."""
    backend = _make_backend(db_with_book)
    _set_metadata_sqlite(backend, 1, {"series": "NewSeries"})
    assert _orphans(db_with_book, "series", "books_series_link", "series") == [], \
        "old series row should have been pruned"


def test_set_metadata_swapping_publisher_cleans_old(db_with_book):
    backend = _make_backend(db_with_book)
    _set_metadata_sqlite(backend, 1, {"publisher": "NewPub"})
    assert _orphans(db_with_book, "publishers", "books_publishers_link", "publisher") == []


def test_set_metadata_swapping_tags_cleans_old(db_with_book):
    backend = _make_backend(db_with_book)
    _set_metadata_sqlite(backend, 1, {"tags": "NewTag"})
    assert _orphans(db_with_book, "tags", "books_tags_link", "tag") == []


def test_set_metadata_swapping_language_cleans_old(db_with_book):
    """The 'eng → spa' bulk swap that orphaned 'eng' in production."""
    backend = _make_backend(db_with_book)
    _set_metadata_sqlite(backend, 1, {"languages": "spa"})
    assert _orphans(db_with_book, "languages", "books_languages_link", "lang_code") == [], \
        "old language row should have been pruned"


def test_non_relational_change_skips_cleanup(db_with_book):
    """Setting only title must not trigger orphan cleanup (cheap path)."""
    # Inject an orphan tag manually; a title-only change should leave it alone
    # (this is desired — only the touched table family should be swept, and
    # title isn't relational, so we skip the sweep entirely).
    conn = sqlite3.connect(str(db_with_book))
    conn.execute("INSERT INTO tags (id, name) VALUES (99, 'PreexistingOrphan')")
    conn.commit()
    conn.close()

    backend = _make_backend(db_with_book)
    _set_metadata_sqlite(backend, 1, {"title": "Renamed"})

    orphans = _orphans(db_with_book, "tags", "books_tags_link", "tag")
    assert "PreexistingOrphan" in orphans, \
        "title-only edit must not pay for an orphan sweep"


def test_bulk_set_metadata_runs_single_global_cleanup(db_with_book):
    """bulk_set_metadata should sweep orphans once at the end, not per row."""
    # Add a second book sharing the publisher so the bulk has multiple updates
    conn = sqlite3.connect(str(db_with_book))
    conn.execute("INSERT INTO books (id, title, path) VALUES (2, 'Test2', 'a/t2 (2)')")
    conn.execute("INSERT INTO books_publishers_link (book, publisher) VALUES (2, 20)")
    conn.commit()
    conn.close()

    backend = _make_backend(db_with_book)
    result = bulk_set_metadata(
        backend,
        [
            {"book_id": 1, "fields": {"series": "NewSeries1"}},
            {"book_id": 2, "fields": {"series": "NewSeries2"}},
        ],
        dry_run=False,
    )
    assert result["status"] == "completed"
    assert "orphans_cleaned" in result, "bulk should report a cleanup pass"
    assert _orphans(db_with_book, "series", "books_series_link", "series") == []
