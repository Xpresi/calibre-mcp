"""Regression tests for the three safety gaps found in the 2026-07 review.

1. books.path containment (resolve_book_dir)
   `Path("D:/lib") / ""` is `Path("D:/lib")`, so a book whose path was empty
   made remove_books aim send2trash at the LIBRARY ROOT and fix_book_paths
   shutil.move it. Both incidents in the project history were "one book's
   path resolved to something much bigger than one book".

2. config.backup_before_bulk_ops was declared and read by nothing — the
   promised pre-operation backup never existed.

3. verify_file_integrity only checked the leaf directory's casing, so a
   miscased AUTHOR directory (the actual Incidente 2 trigger) still reported
   ok=True.
"""

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from calibre_mcp.backend import (
    AUTO_BACKUP_PREFIX,
    CalibreBackend,
    UnsafeLibraryPath,
    resolve_book_dir,
)
from calibre_mcp.config import Config
from calibre_mcp.tools.bulk import fix_book_paths
from calibre_mcp.tools.diagnose import verify_file_integrity
from calibre_mcp.tools.write import remove_books

SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Unknown', sort TEXT,
    series_index REAL DEFAULT 1.0, author_sort TEXT,
    path TEXT NOT NULL DEFAULT '', uuid TEXT, has_cover BOOL DEFAULT 0
);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort TEXT, link TEXT NOT NULL DEFAULT '');
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, format TEXT, name TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, author INTEGER NOT NULL);
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


# ── 1. resolve_book_dir ────────────────────────────────────────────────────


def test_empty_path_does_not_resolve_to_library_root(tmp_path):
    """The core of the bug: without the guard this returns the library itself."""
    assert Path(tmp_path) / "" == Path(tmp_path)  # the trap, still true
    for bad in ("", "   ", None):
        with pytest.raises(UnsafeLibraryPath):
            resolve_book_dir(tmp_path, bad)


@pytest.mark.parametrize("bad", [
    "..",
    "../../Windows",
    "Auth/../../escape",
    "Auth",                    # one component = a whole author directory
    "Auth/Book (1)/extra",     # three components
    "/etc/passwd",
    "C:\\Windows\\System32",
])
def test_rejects_paths_that_are_not_a_single_book_dir(tmp_path, bad):
    with pytest.raises(UnsafeLibraryPath):
        resolve_book_dir(tmp_path, bad)


def test_accepts_normal_calibre_path_and_keeps_db_casing(tmp_path):
    (tmp_path / "Auth, A").mkdir()
    (tmp_path / "Auth, A" / "Book (1)").mkdir()
    # DB casing differs from disk; the returned path must keep the DB's casing
    # so the case-fix code can still see the difference.
    got = resolve_book_dir(tmp_path, "AUTH, A/Book (1)")
    assert got.name == "Book (1)"
    assert got.parent.name == "AUTH, A"
    assert Path(tmp_path).resolve() in got.resolve().parents


def test_accepts_backslash_separated_path(tmp_path):
    got = resolve_book_dir(tmp_path, "Auth\\Book (1)")
    assert got == Path(tmp_path).resolve() / "Auth" / "Book (1)"


# ── 1b. the guard actually stops the destructive tools ─────────────────────


@pytest.fixture
def library_with_bad_path(tmp_path):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO authors (id, name, sort) VALUES (1, 'Auth', 'Auth')")
    # Book 1 is fine, book 2 has the poisoned empty path.
    conn.execute("INSERT INTO books (id,title,author_sort,path) VALUES (1,'Ok','Auth','Auth/Ok (1)')")
    conn.execute("INSERT INTO books (id,title,author_sort,path) VALUES (2,'Bad','Auth','')")
    for i in (1, 2):
        conn.execute("INSERT INTO books_authors_link (book, author) VALUES (?,1)", (i,))
    conn.commit()
    conn.close()
    d = tmp_path / "Auth" / "Ok (1)"
    d.mkdir(parents=True)
    (d / "Ok.epub").write_text("x")
    return _make_backend(db)


def test_remove_books_aborts_before_deleting_anything(library_with_bad_path, tmp_path, monkeypatch):
    backend = library_with_bad_path
    import send2trash as s2t
    monkeypatch.setattr(s2t, "send2trash", lambda p: pytest.fail(f"trashed {p}!"))

    res = remove_books(backend, [1, 2], dry_run=False)

    assert res["status"] == "aborted_unsafe_path"
    assert [u["id"] for u in res["unsafe_paths"]] == [2]
    # Nothing removed — not the library, not the good book, not even DB rows.
    assert tmp_path.exists()
    assert (tmp_path / "Auth" / "Ok (1)" / "Ok.epub").exists()
    assert backend.query_db("SELECT COUNT(*) AS c FROM books")[0]["c"] == 2


def test_fix_book_paths_skips_unsafe_and_moves_nothing(library_with_bad_path, tmp_path):
    res = fix_book_paths(library_with_bad_path, dry_run=True)

    assert res["unsafe_path_count"] == 1
    assert res["unsafe_paths"][0]["id"] == 2
    assert all(f["id"] != 2 for f in res["fixes"])
    assert (tmp_path / "Auth" / "Ok (1)" / "Ok.epub").exists()


def test_fix_book_paths_rejects_author_that_sanitizes_to_nothing(tmp_path):
    """An author name of only whitespace collapses the destination to the
    library root — the move would swallow the library."""
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO authors (id, name, sort) VALUES (1, '   ', '   ')")
    conn.execute("INSERT INTO books (id,title,author_sort,path) VALUES (1,'T','x','Old/T (1)')")
    conn.execute("INSERT INTO books_authors_link (book, author) VALUES (1,1)")
    conn.commit()
    conn.close()
    (tmp_path / "Old" / "T (1)").mkdir(parents=True)

    res = fix_book_paths(_make_backend(db), dry_run=False)

    assert res.get("unsafe_path_count", 0) == 1 or res["status"] == "no_action"
    assert (tmp_path / "Old" / "T (1)").exists()


# ── 2. backup_before_bulk_ops is honoured ──────────────────────────────────


def _backend_for_backup(tmp_path, enabled: bool):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    cfg = Config(
        calibredb_path="calibredb",
        library_path=str(tmp_path),
        backup_before_bulk_ops=enabled,
    )
    return CalibreBackend(cfg)


def test_auto_backup_creates_a_copy(tmp_path):
    backend = _backend_for_backup(tmp_path, enabled=True)
    res = backend.auto_backup_if_due("remove_books")

    assert res["status"] == "created"
    backup = Path(res["backup_path"])
    assert backup.exists()
    assert backup.name.startswith(AUTO_BACKUP_PREFIX)
    # Copied through SQLite's backup API: compare contents, not raw bytes.
    assert sqlite3.connect(str(backup)).execute("SELECT x FROM t").fetchall() == [(1,)]


def test_auto_backup_reuses_a_recent_one(tmp_path):
    backend = _backend_for_backup(tmp_path, enabled=True)
    first = backend.auto_backup_if_due("remove_books")
    second = backend.auto_backup_if_due("fix_book_paths")

    assert first["status"] == "created"
    assert second["status"] == "reused"
    assert second["backup_path"] == first["backup_path"]


def test_auto_backup_makes_a_new_one_once_stale(tmp_path, monkeypatch):
    backend = _backend_for_backup(tmp_path, enabled=True)
    first = backend.auto_backup_if_due("remove_books")
    # Age the existing backup past the reuse window.
    import calibre_mcp.backend as backend_mod
    monkeypatch.setattr(backend_mod, "AUTO_BACKUP_MAX_AGE_S", 0)
    time.sleep(1.1)  # filename stamp has 1-second resolution
    second = backend.auto_backup_if_due("remove_books")

    assert second["status"] == "created"
    assert second["backup_path"] != first["backup_path"]


def test_auto_backup_respects_the_setting_being_off(tmp_path):
    backend = _backend_for_backup(tmp_path, enabled=False)
    assert backend.auto_backup_if_due("remove_books")["status"] == "disabled"
    assert not list(tmp_path.glob(f"{AUTO_BACKUP_PREFIX}*"))


def test_dispatcher_only_backs_up_real_writes():
    from calibre_mcp.server import _needs_auto_backup

    assert _needs_auto_backup("remove_books", {"dry_run": False}) is True
    assert _needs_auto_backup("remove_books", {"dry_run": True}) is False
    assert _needs_auto_backup("remove_books", {}) is False  # dry_run defaults True
    assert _needs_auto_backup("vacuum_database", {}) is True  # has no dry_run
    assert _needs_auto_backup("search_books", {"dry_run": False}) is False


# ── 3. verify_file_integrity sees author-level casing ──────────────────────


@pytest.fixture
def library_with_miscased_author_dir(tmp_path):
    """DB says 'Abad, Mercedes'; disk has 'ABAD, MERCEDES' — the exact shape of
    Incidente 2, where 14k directories were judged orphans and deleted."""
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO books (id,title,author_sort,path) VALUES "
        "(1,'Libro','Abad, Mercedes','Abad, Mercedes/Libro (1)')"
    )
    conn.execute("INSERT INTO data (book, format, name) VALUES (1,'EPUB','Libro')")
    conn.commit()
    conn.close()
    d = tmp_path / "ABAD, MERCEDES" / "Libro (1)"
    d.mkdir(parents=True)
    (d / "Libro.epub").write_text("x")
    return _make_backend(db)


def _fs_is_case_insensitive(where: Path) -> bool:
    probe = where / "_CaseProbe"
    probe.mkdir()
    try:
        return (where / "_caseprobe").exists()
    finally:
        probe.rmdir()


def test_author_dir_case_mismatch_is_not_reported_ok(library_with_miscased_author_dir):
    """The whole scenario needs a case-insensitive, case-preserving filesystem
    (Windows/macOS): that is what makes the file reachable while the name on
    disk differs — the false 'ok' that cost 14k files."""
    lib = Path(library_with_miscased_author_dir.config.library_path)
    if not _fs_is_case_insensitive(lib):
        pytest.skip("filesystem is case-sensitive; this failure mode cannot occur")

    res = verify_file_integrity(library_with_miscased_author_dir)

    assert res["ok"] is False, "a miscased author dir must never report ok=True"
    assert res["missing_count"] == 0  # the file IS reachable — that was the trap
    assert res["case_mismatch_count"] == 1
    cm = res["case_mismatches"][0]
    assert cm["mismatch_level"] == "author"
    assert cm["expected_dir_name"] == "Abad, Mercedes"
    assert cm["actual_dir_name"] == "ABAD, MERCEDES"
    assert res["case_mismatch_levels"] == {"author": 1}


def test_book_dir_case_mismatch_still_detected(tmp_path):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO books (id,title,author_sort,path) VALUES "
        "(1,'Libro','Auth','Auth/Libro (1)')"
    )
    conn.execute("INSERT INTO data (book, format, name) VALUES (1,'EPUB','Libro')")
    conn.commit()
    conn.close()
    d = tmp_path / "Auth" / "LIBRO (1)"
    d.mkdir(parents=True)
    (d / "Libro.epub").write_text("x")
    if not _fs_is_case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem (Windows/macOS)")

    res = verify_file_integrity(_make_backend(db))
    assert res["ok"] is False
    assert res["case_mismatches"][0]["mismatch_level"] == "book"


def test_correct_casing_reports_ok(tmp_path):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO books (id,title,author_sort,path) VALUES "
        "(1,'Libro','Auth','Auth/Libro (1)')"
    )
    conn.execute("INSERT INTO data (book, format, name) VALUES (1,'EPUB','Libro')")
    conn.commit()
    conn.close()
    d = tmp_path / "Auth" / "Libro (1)"
    d.mkdir(parents=True)
    (d / "Libro.epub").write_text("x")

    res = verify_file_integrity(_make_backend(db))
    assert res["ok"] is True
    assert res["case_mismatch_count"] == 0
