"""Backend for Calibre operations: calibredb CLI for writes, SQLite for reads."""

import json
import logging
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config

logger = logging.getLogger("calibre_mcp")


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

        # Books without any format file
        stats["books_without_files"] = self.query_db_scalar("""
            SELECT COUNT(*) FROM books b
            WHERE NOT EXISTS (SELECT 1 FROM data d WHERE d.book = b.id)
        """) or 0

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
