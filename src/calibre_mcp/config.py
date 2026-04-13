"""Configuration loader for Calibre MCP Server."""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _default_calibredb_path() -> str:
    """Return the most likely calibredb path for the current platform."""
    if sys.platform == "win32":
        return r"C:\Program Files\Calibre2\calibredb.exe"
    elif sys.platform == "darwin":
        return "/Applications/calibre.app/Contents/MacOS/calibredb"
    else:
        return "/usr/bin/calibredb"


@dataclass
class Config:
    calibredb_path: str = ""
    library_path: str = ""
    backup_before_bulk_ops: bool = True
    dry_run_by_default: bool = True
    operations_log: str = ""  # Path to JSONL operations log; defaults to <library>/calibre_operations.jsonl

    def __post_init__(self):
        if not self.calibredb_path:
            self.calibredb_path = _default_calibredb_path()

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Config":
        """Load config from JSON file. Falls back to defaults if file not found."""
        if config_path is None:
            # Look for config.json next to the package, then in CWD
            candidates = [
                Path(__file__).parent.parent.parent / "config.json",
                Path.cwd() / "config.json",
            ]
            for candidate in candidates:
                if candidate.exists():
                    config_path = candidate
                    break

        if config_path and Path(config_path).exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        return cls()

    @property
    def metadata_db_path(self) -> Path:
        return Path(self.library_path) / "metadata.db"

    @property
    def operations_log_path(self) -> Path:
        if self.operations_log:
            return Path(self.operations_log)
        return Path(self.library_path) / "calibre_operations.jsonl"

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = OK)."""
        errors = []
        if not Path(self.calibredb_path).exists():
            errors.append(f"calibredb not found at: {self.calibredb_path}")
        if not Path(self.library_path).exists():
            errors.append(f"Library path not found: {self.library_path}")
        if not self.metadata_db_path.exists():
            errors.append(f"metadata.db not found at: {self.metadata_db_path}")
        return errors
