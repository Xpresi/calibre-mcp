"""Sidecar TOC cache for EPUB extractions.

Extracting a TOC from a 50+ MB EPUB takes 1-3 seconds; doing it for every
compilation candidate of an author with 100+ books makes find_compilation_coverage
the slowest tool by far. This cache stores parsed TOCs in a separate SQLite
DB inside the library folder and invalidates entries when the underlying EPUB
file's mtime, size, or content fingerprint changes.

Kept SEPARATE from metadata.db on purpose:
  - We never want to risk corrupting Calibre's DB with cache writes.
  - The cache file can be deleted at any time with no data loss.
  - It survives Calibre's own restore_database operations.

Stored in a hidden subdirectory `.calibre_mcp_cache/` so:
  - `find_orphan_files` (which already skips dotfiles) ignores it.
  - `calibredb check_library` doesn't flag it as an unknown root entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("calibre_mcp")

CACHE_SUBDIR  = ".calibre_mcp_cache"
CACHE_FILENAME = "toc.db"
LEGACY_CACHE_FILENAME = "_calibre_mcp_toc_cache.db"  # pre-2026-05 location at lib root

# How many bytes from the head + tail of the EPUB go into the content fingerprint.
# Catches "byte-identical re-save in the same FAT 2-second window" (mtime didn't
# move and size didn't change). 4 KiB is enough — EPUB headers + central
# directory records always differ if any entry was rewritten.
_FINGERPRINT_CHUNK = 4096

_SCHEMA = """
CREATE TABLE IF NOT EXISTS toc_cache (
    book_id           INTEGER PRIMARY KEY,
    epub_path         TEXT    NOT NULL,
    file_mtime_ns     INTEGER NOT NULL,
    file_size         INTEGER NOT NULL,
    content_hash      TEXT    NOT NULL,
    toc_source        TEXT,
    toc_json          TEXT    NOT NULL,
    is_likely_scan    INTEGER NOT NULL,
    scan_signals_json TEXT,
    cached_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _cache_dir(library_path: str) -> Path:
    return Path(library_path) / CACHE_SUBDIR


def _cache_path(library_path: str) -> Path:
    return _cache_dir(library_path) / CACHE_FILENAME


def _legacy_cache_path(library_path: str) -> Path:
    """Old cache location at the library root — migrated on first connect."""
    return Path(library_path) / LEGACY_CACHE_FILENAME


def _connect(library_path: str) -> sqlite3.Connection:
    """Open the cache DB, creating the schema (and the cache subdir) if needed."""
    cache_dir = _cache_dir(library_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(library_path)

    # One-time silent migration of the legacy cache file. Best-effort: if the
    # rename fails for any reason we just proceed with a fresh cache (the old
    # file becomes an orphan that the user can delete).
    legacy = _legacy_cache_path(library_path)
    if legacy.exists() and not path.exists():
        try:
            legacy.rename(path)
            logger.info(f"Migrated TOC cache: {legacy} → {path}")
        except OSError as exc:
            logger.warning(f"Could not migrate legacy TOC cache {legacy}: {exc}")

    conn = sqlite3.connect(str(path), timeout=10)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _content_fingerprint(epub_path: str, size: int) -> str:
    """Hash of the first + last _FINGERPRINT_CHUNK bytes of the file.

    Catches byte-identical re-saves where mtime didn't advance (FAT/exFAT
    have 2-second mtime resolution; on the same window a quick re-save can
    leave both mtime and size untouched while content changes).
    """
    try:
        h = hashlib.blake2b(digest_size=16)
        with open(epub_path, "rb") as f:
            h.update(f.read(_FINGERPRINT_CHUNK))
            if size > _FINGERPRINT_CHUNK * 2:
                f.seek(-_FINGERPRINT_CHUNK, os.SEEK_END)
                h.update(f.read(_FINGERPRINT_CHUNK))
        return h.hexdigest()
    except OSError:
        return ""  # caller treats empty hash as cache miss


def _file_fingerprint(epub_path: str) -> tuple[int, int, str] | None:
    """Return (mtime_ns, size, content_hash) of the EPUB file, or None on error."""
    try:
        st = os.stat(epub_path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, _content_fingerprint(epub_path, st.st_size))


def get_cached(library_path: str, book_id: int, epub_path: str) -> dict[str, Any] | None:
    """Return cached TOC data if the file's fingerprint matches the cached entry.

    Returns dict with keys: toc, toc_source, is_likely_scan, scan_signals.
    Returns None on cache miss, stale entry, or any error (silent — cache is
    a performance optimisation, never a correctness requirement).
    """
    fingerprint = _file_fingerprint(epub_path)
    if fingerprint is None:
        return None
    mtime_ns, size, content_hash = fingerprint

    try:
        conn = _connect(library_path)
        try:
            row = conn.execute(
                "SELECT file_mtime_ns, file_size, content_hash, toc_source, toc_json, "
                "       is_likely_scan, scan_signals_json "
                "FROM toc_cache WHERE book_id = ?",
                (int(book_id),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(f"TOC cache read failed for book {book_id}: {exc}")
        return None

    if not row:
        return None
    # Exact integer comparison on mtime_ns, exact size, AND content fingerprint.
    # Any one of them differing → stale.
    if (
        row["file_mtime_ns"] != mtime_ns
        or row["file_size"] != size
        or row["content_hash"] != content_hash
    ):
        return None

    try:
        toc = json.loads(row["toc_json"])
        signals = json.loads(row["scan_signals_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return None  # corrupt cache row → re-extract

    return {
        "toc": toc,
        "toc_source": row["toc_source"],
        "is_likely_scan": bool(row["is_likely_scan"]),
        "scan_signals": signals,
        "_from_cache": True,
    }


def store(
    library_path: str,
    book_id: int,
    epub_path: str,
    toc: list[str],
    toc_source: str,
    is_likely_scan: bool,
    scan_signals: list[str],
) -> None:
    """Persist a TOC extraction result. Silent on error — cache is best-effort."""
    fingerprint = _file_fingerprint(epub_path)
    if fingerprint is None:
        return
    mtime_ns, size, content_hash = fingerprint

    try:
        conn = _connect(library_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO toc_cache "
                "(book_id, epub_path, file_mtime_ns, file_size, content_hash, "
                " toc_source, toc_json, is_likely_scan, scan_signals_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(book_id),
                    str(epub_path),
                    int(mtime_ns),
                    int(size),
                    content_hash,
                    toc_source,
                    json.dumps(toc, ensure_ascii=False),
                    1 if is_likely_scan else 0,
                    json.dumps(scan_signals, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(f"TOC cache write failed for book {book_id}: {exc}")


def stats(library_path: str) -> dict[str, Any]:
    """Return cache size + entry count. Used by diagnostic / housekeeping tools."""
    cache_file = _cache_path(library_path)
    if not cache_file.exists():
        return {"exists": False, "entries": 0, "size_bytes": 0}
    try:
        conn = _connect(library_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM toc_cache").fetchone()[0]
            scans = conn.execute(
                "SELECT COUNT(*) FROM toc_cache WHERE is_likely_scan = 1"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return {"exists": True, "entries": 0, "size_bytes": cache_file.stat().st_size,
                "error": "cache_db_unreadable"}
    return {
        "exists": True,
        "entries": n,
        "scan_entries": scans,
        "size_bytes": cache_file.stat().st_size,
        "path": str(cache_file),
    }
