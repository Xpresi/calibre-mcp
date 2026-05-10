"""Sidecar TOC cache for EPUB extractions.

Extracting a TOC from a 50+ MB EPUB takes 1-3 seconds; doing it for every
compilation candidate of an author with 100+ books makes find_compilation_coverage
the slowest tool by far. This cache stores parsed TOCs in a separate SQLite
DB inside the library folder and invalidates entries when the underlying EPUB
file's mtime or size changes.

Kept SEPARATE from metadata.db on purpose:
  - We never want to risk corrupting Calibre's DB with cache writes.
  - The cache file can be deleted at any time with no data loss.
  - It survives Calibre's own restore_database operations.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("calibre_mcp")

CACHE_FILENAME = "_calibre_mcp_toc_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS toc_cache (
    book_id           INTEGER PRIMARY KEY,
    epub_path         TEXT    NOT NULL,
    file_mtime        REAL    NOT NULL,
    file_size         INTEGER NOT NULL,
    toc_source        TEXT,
    toc_json          TEXT    NOT NULL,
    is_likely_scan    INTEGER NOT NULL,
    scan_signals_json TEXT,
    cached_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _cache_path(library_path: str) -> Path:
    return Path(library_path) / CACHE_FILENAME


def _connect(library_path: str) -> sqlite3.Connection:
    """Open the cache DB, creating the schema if needed."""
    path = _cache_path(library_path)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _file_fingerprint(epub_path: str) -> tuple[float, int] | None:
    """Return (mtime, size) of the EPUB file, or None if it doesn't exist."""
    try:
        st = os.stat(epub_path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def get_cached(library_path: str, book_id: int, epub_path: str) -> dict[str, Any] | None:
    """Return cached TOC data if the file's mtime+size match the cached entry.

    Returns dict with keys: toc, toc_source, is_likely_scan, scan_signals.
    Returns None on cache miss, stale entry, or any error (silent — cache is
    a performance optimisation, never a correctness requirement).
    """
    fingerprint = _file_fingerprint(epub_path)
    if fingerprint is None:
        return None
    mtime, size = fingerprint

    try:
        conn = _connect(library_path)
        try:
            row = conn.execute(
                "SELECT file_mtime, file_size, toc_source, toc_json, "
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
    # Mtime comparison uses a small epsilon — filesystems return mtimes with
    # varying resolution (NTFS = 100ns, FAT = 2s). Treat <1ms drift as equal.
    if abs(row["file_mtime"] - mtime) > 0.001 or row["file_size"] != size:
        return None  # stale

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
    mtime, size = fingerprint

    try:
        conn = _connect(library_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO toc_cache "
                "(book_id, epub_path, file_mtime, file_size, toc_source, "
                " toc_json, is_likely_scan, scan_signals_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(book_id),
                    str(epub_path),
                    float(mtime),
                    int(size),
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
