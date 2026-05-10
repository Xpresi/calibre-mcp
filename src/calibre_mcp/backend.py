"""Backend for Calibre operations: calibredb CLI for writes, SQLite for reads."""

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config

logger = logging.getLogger("calibre_mcp")


# ── Calibre custom SQLite functions ───────────────────────────────────────────
# Calibre registers these at runtime when its process opens the DB. Without
# them, triggers on books/series (books_update_trg, series_update_trg, etc.)
# fail with "no such function". We register Python equivalents so direct SQLite
# writes work even when Calibre is closed.

_ARTICLE_RE = re.compile(
    r'^(el|la|los|las|un|una|the|a|an|das|die|der|le|les|une)\s+',
    re.IGNORECASE,
)


def _calibre_title_sort(title: str) -> str:
    """Move leading articles to the end: 'El capitán' → 'capitán, El'."""
    if not title:
        return title or ""
    m = _ARTICLE_RE.match(title)
    if m:
        return title[m.end():].strip() + ", " + title[: m.end()].strip()
    return title


def _calibre_uuid4() -> str:
    """Return a new UUID4 string (used by books_insert_trg)."""
    return str(uuid.uuid4())


def calibre_sanitize(s: str) -> str:
    """
    Sanitize a string to a Calibre-compatible filesystem path component.
    Calibre replaces backslash, forward slash, colon, asterisk, question mark,
    double quote, angle brackets, pipe, AND periods with underscores on Windows.
    """
    s = re.sub(r'[\\/:*?"<>|.]', '_', s)
    return s.strip()[:200]


# ── Schema constants ────────────────────────────────────────────────────────
# Single source of truth for the per-book child tables and metadata tables.
# Used by remove_books, find/cleanup_orphan_links, and find_orphaned_metadata
# to guarantee they all touch the same set of tables.

# (table_name, book-id column) — every table that has a foreign key to books.id.
# When a book is removed, every row referencing it in these tables must go.
# When orphan links are detected, every entry whose book.id no longer exists
# in `books` is an orphan.
BOOK_CHILD_TABLES: list[tuple[str, str]] = [
    ("books_authors_link",     "book"),
    ("books_series_link",      "book"),
    ("books_tags_link",        "book"),
    ("books_publishers_link",  "book"),
    ("books_languages_link",   "book"),
    ("books_ratings_link",     "book"),
    ("books_pages_link",       "book"),  # Count Pages plugin — has real FK constraint
    ("books_plugin_data",      "book"),  # Plugin metadata
    ("data",                   "book"),
    ("comments",               "book"),
    ("identifiers",            "book"),
    ("last_read_positions",    "book"),  # Reading positions (per book/format/user/device)
    ("annotations",            "book"),  # Highlights, notes, bookmarks
    ("annotations_dirtied",    "book"),  # Calibre dirty-tracking for annotations
    ("metadata_dirtied",       "book"),  # Calibre dirty-tracking for metadata
    ("conversion_options",     "book"),  # Per-book conversion settings
]

# (entity_table, link_col_in_link_table, link_table) — the metadata entity
# tables that can become orphan when no books reference them. languages MUST
# be included: a stale 'eng' row triggers FK violations in the Calibre GUI
# when it tries its own cleanup pass after a deletion.
METADATA_TABLES: list[tuple[str, str, str]] = [
    ("authors",    "author",    "books_authors_link"),
    ("series",     "series",    "books_series_link"),
    ("tags",       "tag",       "books_tags_link"),
    ("publishers", "publisher", "books_publishers_link"),
    ("languages",  "lang_code", "books_languages_link"),
]


class CalibreError(Exception):
    """Raised when a calibredb operation fails."""
    pass


class CalibreBackend:
    """Provides access to a Calibre library via calibredb CLI and direct SQLite reads."""

    def __init__(self, config: Config):
        self.config = config
        self.calibredb = config.calibredb_path
        self.library = config.library_path
        self.db_path = config.metadata_db_path

    # ── calibredb CLI ──────────────────────────────────────────────────

    def run_calibredb(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: int = 120,
    ) -> str:
        """
        Run a calibredb command and return stdout.

        Args:
            command: calibredb subcommand (e.g., 'list', 'remove', 'set_metadata')
            args: Additional arguments for the command
            timeout: Timeout in seconds (default 120)

        Returns:
            stdout as string

        Raises:
            CalibreError: If the command fails
        """
        cmd = [
            self.calibredb,
            command,
            "--with-library", self.library,
        ]
        if args:
            cmd.extend(args)

        logger.info(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
            )
        except subprocess.TimeoutExpired:
            raise CalibreError(f"calibredb {command} timed out after {timeout}s")
        except FileNotFoundError:
            raise CalibreError(f"calibredb not found at: {self.calibredb}")

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Common error: Calibre GUI is open and has the DB locked
            if "locked" in stderr.lower() or "already running" in stderr.lower():
                raise CalibreError(
                    f"calibredb failed — Calibre GUI may be open and locking the database.\n"
                    f"Close Calibre and retry.\nError: {stderr}"
                )
            raise CalibreError(f"calibredb {command} failed (rc={result.returncode}): {stderr}")

        return result.stdout

    # ── SQLite direct reads ────────────────────────────────────────────

    def query_db(
        self,
        sql: str,
        params: tuple | dict | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a read-only SQL query against metadata.db.

        Returns list of dicts (column_name → value).
        Connection is opened and closed per call (stateless).
        """
        db_path = str(self.db_path)
        uri = f"file:{db_path}?mode=ro"

        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql, params or ())
            rows = [dict(row) for row in cursor.fetchall()]
            return rows
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise CalibreError(
                    f"Database is locked. Close Calibre if it's open.\nError: {e}"
                )
            raise CalibreError(f"SQLite query failed: {e}\nSQL: {sql}")
        finally:
            if conn:
                conn.close()

    def query_db_scalar(self, sql: str, params: tuple | dict | None = None) -> Any:
        """Execute a query and return the first column of the first row, or None."""
        rows = self.query_db(sql, params)
        if rows:
            first_key = next(iter(rows[0]))
            return rows[0][first_key]
        return None

    # ── Calibre process detection ──────────────────────────────────────

    def is_calibre_running(self) -> bool:
        """Return True if the Calibre GUI process is currently running."""
        try:
            if sys.platform == "win32":
                # Use shell=True so cmd.exe handles the /FI flag correctly
                # regardless of whether we're running under Git bash or cmd.
                result = subprocess.run(
                    'tasklist /FI "IMAGENAME eq calibre.exe" /NH',
                    shell=True,
                    capture_output=True, text=True, timeout=5,
                )
                return "calibre.exe" in result.stdout.lower()
            else:
                # Linux / macOS
                result = subprocess.run(
                    ["pgrep", "-x", "calibre"],
                    capture_output=True, timeout=5,
                )
                return result.returncode == 0
        except Exception:
            return False  # Can't check → assume closed, let SQLite catch real locks

    def require_calibre_closed(self) -> None:
        """Raise CalibreError if the Calibre GUI is open."""
        if self.is_calibre_running():
            raise CalibreError(
                "Calibre is currently open. Close it before running write operations "
                "that access the database directly."
            )

    def log_operation(
        self,
        tool: str,
        args: dict[str, Any],
        result: Any = None,
    ) -> None:
        """Append a structured entry to the operations log (JSONL)."""
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "args": args,
            "result": result,
        }
        try:
            log_path = self.config.operations_log_path
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write operations log: {e}")

    def connect_write(self, timeout: int = 30) -> sqlite3.Connection:
        """
        Open a writable SQLite connection to metadata.db.

        Verifies Calibre is closed first, then opens the connection in WAL mode.
        Use this instead of sqlite3.connect() for all direct write operations.

        Registers Calibre's custom SQLite functions so DB triggers don't fail:
          - title_sort(text) — used by books_update_trg / series_update_trg
          - uuid4()          — used by books_insert_trg
        These functions are normally registered by the Calibre process at runtime;
        without them, any UPDATE on books.title raises "no such function: title_sort".
        """
        self.require_calibre_closed()
        conn = sqlite3.connect(str(self.db_path), timeout=timeout)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.create_function("title_sort", 1, _calibre_title_sort)
        conn.create_function("uuid4", 0, _calibre_uuid4)
        return conn

    @contextmanager
    def write_transaction(self, timeout: int = 30):
        """
        Context manager for a managed write transaction on metadata.db.

        Wraps connect_write + explicit BEGIN. On normal exit: COMMIT and close.
        On any exception: best-effort ROLLBACK (never raises from rollback) +
        close, then re-raises the original exception.

        Use this everywhere you'd otherwise hand-roll the
            try: conn = backend.connect_write(); conn.execute("BEGIN")
                ...
                conn.execute("COMMIT")
            except: conn.execute("ROLLBACK"); raise
            finally: conn.close()
        boilerplate. The hand-rolled versions had drifted: some forgot to
        close on the rollback path, some swallowed the original exception
        when rollback itself failed.
        """
        conn = self.connect_write(timeout=timeout)
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                logger.exception(
                    "ROLLBACK failed (connection may already be broken); "
                    "original error will still be raised"
                )
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── Utility methods ────────────────────────────────────────────────

    def backup_database(self, suffix: str | None = None) -> Path:
        """Create a backup of metadata.db. Returns path to backup file."""
        if suffix is None:
            suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.db_path.parent / f"metadata_backup_{suffix}.db"
        shutil.copy2(self.db_path, backup_path)
        logger.info(f"Database backed up to: {backup_path}")
        return backup_path

    def vacuum_database(self) -> str:
        """Compact the database. Requires exclusive access (no Calibre running)."""
        # VACUUM needs write access, so we connect normally (not read-only)
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            size_before = self.db_path.stat().st_size
            conn.execute("VACUUM")
            conn.close()
            size_after = self.db_path.stat().st_size
            saved = size_before - size_after
            msg = (
                f"VACUUM complete. "
                f"Before: {size_before / 1024 / 1024:.1f} MB, "
                f"After: {size_after / 1024 / 1024:.1f} MB, "
                f"Saved: {saved / 1024:.0f} KB"
            )
            logger.info(msg)
            return msg
        except sqlite3.OperationalError as e:
            raise CalibreError(f"VACUUM failed (is Calibre open?): {e}")

    def get_library_stats(self) -> dict[str, Any]:
        """Get summary statistics of the library."""
        stats = {}

        # Basic counts
        stats["total_books"] = self.query_db_scalar("SELECT COUNT(*) FROM books") or 0
        stats["total_authors"] = self.query_db_scalar("SELECT COUNT(*) FROM authors") or 0
        stats["total_series"] = self.query_db_scalar("SELECT COUNT(*) FROM series") or 0
        stats["total_tags"] = self.query_db_scalar("SELECT COUNT(*) FROM tags") or 0
        stats["total_publishers"] = self.query_db_scalar("SELECT COUNT(*) FROM publishers") or 0

        # Format counts
        formats = self.query_db(
            "SELECT format, COUNT(*) as cnt FROM data GROUP BY format ORDER BY cnt DESC"
        )
        stats["formats"] = {r["format"]: r["cnt"] for r in formats}

        # Language counts (top 10)
        languages = self.query_db("""
            SELECT l.lang_code, COUNT(*) as cnt
            FROM books_languages_link bl
            JOIN languages l ON bl.lang_code = l.id
            GROUP BY l.lang_code
            ORDER BY cnt DESC
            LIMIT 10
        """)
        stats["top_languages"] = {r["lang_code"]: r["cnt"] for r in languages}

        # Books with no format records in the DB (SQL-only — does NOT verify files exist on disk).
        # A book can show 0 here and still be a ghost (file deleted but data row intact).
        # Use find_ghost_books() for a real filesystem check.
        stats["books_without_data_records"] = self.query_db_scalar("""
            SELECT COUNT(*) FROM books b
            WHERE NOT EXISTS (SELECT 1 FROM data d WHERE d.book = b.id)
        """) or 0
        stats["_warning_books_without_data_records"] = (
            "SQL-only check. Does NOT verify files exist on disk. "
            "Run find_ghost_books() for a real filesystem check."
        )

        # Books without cover
        stats["books_without_cover"] = self.query_db_scalar("""
            SELECT COUNT(*) FROM books WHERE has_cover = 0
        """) or 0

        # Books without tags
        stats["books_without_tags"] = self.query_db_scalar("""
            SELECT COUNT(*) FROM books b
            WHERE NOT EXISTS (SELECT 1 FROM books_tags_link bt WHERE bt.book = b.id)
        """) or 0

        # Books without language
        stats["books_without_language"] = self.query_db_scalar("""
            SELECT COUNT(*) FROM books b
            WHERE NOT EXISTS (SELECT 1 FROM books_languages_link bl WHERE bl.book = b.id)
        """) or 0

        # Database file size
        db_size = self.db_path.stat().st_size
        stats["database_size_mb"] = round(db_size / 1024 / 1024, 1)

        # Library path
        stats["library_path"] = self.library

        return stats
