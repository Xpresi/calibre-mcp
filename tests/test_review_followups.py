"""Regression tests for the remaining 2026-07 review findings.

- find_orphan_files truncated the SCAN at `limit`, so case_mismatch_count could
  read 0 on a library full of miscased directories — a false all-clear on the
  exact directories that must never be deleted.
- Orphan cleanup used `id NOT IN (SELECT col ...)`: one NULL in the link column
  makes that NULL for every row, so the DELETE silently matches nothing.
- set_book_metadata / bulk_set_metadata rewrote titles and authors without
  telling anyone books.path was now stale (the Incidente 1 drift), while the
  bulk tools did.
- raw_sql_query rejected WITH ... SELECT, which is read-only.
- calibre_sanitize must NOT transliterate accents: this library keeps them.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from calibre_mcp.backend import calibre_sanitize
from calibre_mcp.tools.bulk import find_orphaned_metadata
from calibre_mcp.tools.diagnose import find_orphan_files
from calibre_mcp.tools.query import raw_sql_query
from calibre_mcp.tools.write import bulk_set_metadata, set_book_metadata

SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Unknown', sort TEXT,
    series_index REAL DEFAULT 1.0, author_sort TEXT,
    path TEXT NOT NULL DEFAULT '', uuid TEXT, has_cover BOOL DEFAULT 0
);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT, link TEXT NOT NULL DEFAULT '');
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT);
CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT NOT NULL UNIQUE, item_order INTEGER NOT NULL DEFAULT 1);
CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER NOT NULL UNIQUE, text TEXT NOT NULL DEFAULT '');
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, format TEXT, name TEXT);
CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, type TEXT, val TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, author INTEGER NOT NULL);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, series INTEGER NOT NULL);
CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, tag INTEGER NOT NULL);
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, publisher INTEGER NOT NULL);
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, lang_code INTEGER);
"""


def _make_backend(db_path: Path):
    def query_db(sql, params=None):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params or ()).fetchall()]
        finally:
            conn.close()

    def connect_write(timeout: int = 15):
        return sqlite3.connect(str(db_path), timeout=timeout)

    return SimpleNamespace(
        db_path=db_path,
        config=SimpleNamespace(library_path=str(db_path.parent)),
        query_db=query_db,
        connect_write=connect_write,
    )


def _empty_db(tmp_path: Path):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db


# ── find_orphan_files: full scan, capped listing ───────────────────────────


@pytest.fixture
def library_with_many_orphans(tmp_path):
    """5 real orphans + 4 case mismatches, interleaved so a scan that stops at
    limit=2 would miss most of both categories."""
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    for i in range(4):
        conn.execute(
            "INSERT INTO books (id,title,author_sort,path) VALUES (?,?,?,?)",
            (i + 1, f"Known{i}", "Auth", f"Auth{i}/Known{i} ({i + 1})"),
        )
    conn.commit()
    conn.close()
    for i in range(4):
        # On disk with the WRONG casing → case mismatch, never an orphan
        (tmp_path / f"AUTH{i}" / f"KNOWN{i} ({i + 1})").mkdir(parents=True)
    for i in range(5):
        (tmp_path / f"Ghost{i}" / f"Nobody{i} (99{i})").mkdir(parents=True)
    return _make_backend(db)


def test_scan_is_complete_even_when_listing_is_capped(library_with_many_orphans):
    res = find_orphan_files(library_with_many_orphans, use_calibredb=False, limit=2)

    assert res["scan_status"] == "COMPLETE"
    # Counts cover the whole library even though only 2 of each are listed
    assert res["orphan_count"] == 5
    assert res["case_mismatch_count"] == 4
    assert len(res["orphans"]) == 2
    assert len(res["case_mismatches"]) == 2
    assert res["listing_truncated"] is True
    assert res["case_mismatch_warning"]


def test_no_case_mismatch_is_hidden_by_the_orphan_limit(library_with_many_orphans):
    """The old code broke out of the walk once `limit` orphans were found, so
    case mismatches discovered later were never counted."""
    res = find_orphan_files(library_with_many_orphans, use_calibredb=False, limit=1)
    assert res["case_mismatch_count"] == 4


def test_calibredb_mode_does_not_pretend_to_have_classified_anything(tmp_path):
    db = _empty_db(tmp_path)
    backend = _make_backend(db)
    backend.run_calibredb = lambda *a, **k: "some text output"

    res = find_orphan_files(backend, use_calibredb=True)

    # Explicitly null, not [] — a caller must not read this as "none found"
    assert res["orphans"] is None
    assert res["case_mismatches"] is None
    assert "WARNING" in res


# ── NOT EXISTS instead of NOT IN ───────────────────────────────────────────


def test_orphan_metadata_still_found_when_link_table_has_a_null(tmp_path):
    """With NOT IN, one NULL lang_code made every orphan invisible."""
    db = _empty_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO books (id,title,path) VALUES (1,'T','A/T (1)')")
    conn.execute("INSERT INTO languages (id, lang_code) VALUES (1,'eng')")
    conn.execute("INSERT INTO languages (id, lang_code) VALUES (2,'spa')")
    conn.execute("INSERT INTO books_languages_link (book, lang_code) VALUES (1, 1)")
    conn.execute("INSERT INTO books_languages_link (book, lang_code) VALUES (1, NULL)")
    conn.commit()
    conn.close()
    backend = _make_backend(db)

    found = find_orphaned_metadata(backend, dry_run=True)
    assert found["orphans"]["languages"] == ["spa"]

    deleted = find_orphaned_metadata(backend, dry_run=False)
    assert deleted["deleted"]["languages"] == 1
    remaining = backend.query_db("SELECT lang_code FROM languages")
    assert [r["lang_code"] for r in remaining] == ["eng"]


# ── path-drift next_steps on the direct-write tools ────────────────────────


@pytest.fixture
def one_book(tmp_path):
    db = _empty_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO books (id,title,author_sort,path) VALUES (1,'Old','A','A/Old (1)')")
    conn.commit()
    conn.close()
    return _make_backend(db)


def test_set_book_metadata_warns_when_title_changes(one_book):
    res = set_book_metadata(one_book, 1, {"title": "New"}, dry_run=False)
    assert res["status"] == "success"
    assert res["affected_book_ids"] == [1]
    assert any("fix_book_paths" in s for s in res["next_steps"])


def test_set_book_metadata_stays_quiet_for_non_path_fields(one_book):
    res = set_book_metadata(one_book, 1, {"comments": "hi"}, dry_run=False)
    assert res["status"] == "success"
    assert "next_steps" not in res


def test_bulk_set_metadata_lists_only_the_drifted_books(one_book):
    conn = sqlite3.connect(str(one_book.db_path))
    conn.execute("INSERT INTO books (id,title,author_sort,path) VALUES (2,'Two','A','A/Two (2)')")
    conn.commit()
    conn.close()

    res = bulk_set_metadata(
        one_book,
        [
            {"book_id": 1, "fields": {"title": "Renamed"}},
            {"book_id": 2, "fields": {"comments": "no path impact"}},
        ],
        dry_run=False,
    )
    assert res["applied"] == 2
    assert res["affected_book_ids"] == [1]


# ── raw_sql_query accepts read-only CTEs ───────────────────────────────────


def test_with_cte_is_allowed(tmp_path):
    db = _empty_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO books (id,title,path) VALUES (1,'T','A/T (1)')")
    conn.commit()
    conn.close()

    res = raw_sql_query(
        _make_backend(db),
        "WITH x AS (SELECT id, title FROM books) SELECT * FROM x",
    )
    assert res["status"] == "success", res
    assert res["rows"] == [{"id": 1, "title": "T"}]


@pytest.mark.parametrize("sql", [
    "WITH x AS (SELECT 1) DELETE FROM books",
    "DROP TABLE books",
    "UPDATE books SET title='x'",
])
def test_writes_are_still_refused(tmp_path, sql):
    res = raw_sql_query(_make_backend(_empty_db(tmp_path)), sql)
    assert res["status"] == "error"


# ── calibre_sanitize keeps accents ─────────────────────────────────────────


def test_accents_are_preserved_not_transliterated():
    """This library stores 'Gorodischer, Angélica' on disk verbatim. Asciizing
    would mark ~11.9k books' paths as wrong and mass-rename their directories."""
    assert calibre_sanitize("Gorodischer, Angélica") == "Gorodischer, Angélica"
    assert calibre_sanitize("Piñera, Virgilio") == "Piñera, Virgilio"
    assert calibre_sanitize("Žižek, Slavoj") == "Žižek, Slavoj"


def test_forbidden_characters_still_become_underscores():
    assert calibre_sanitize("Fernández, Xavier B.") == "Fernández, Xavier B_"
    assert calibre_sanitize('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_truncation_does_not_leave_a_trailing_space_or_dot():
    # 199 chars then a space: the cut lands on the space, which Windows drops
    # from the created directory name — the DB path would never match on disk.
    assert not calibre_sanitize("x" * 199 + "   y").endswith(" ")
    assert len(calibre_sanitize("y" * 300)) == 200
