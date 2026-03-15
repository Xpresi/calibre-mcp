# Calibre MCP Server

MCP (Model Context Protocol) server for managing a Calibre ebook library (~71,000 books). Provides 25+ tools for querying, diagnosing problems, and cleaning up the library.

## Architecture

- **Reads**: Direct SQLite queries against `metadata.db` (fast, flexible)
- **Writes**: Via `calibredb.exe` CLI (handles files, folders, triggers correctly)
- **Protocol**: MCP stdio transport
- **Platform**: Primary platform Windows; calibredb also available on Linux/macOS (path is typically `/usr/bin/calibredb`)

## Installation

```bash
# Clone or copy the project
cd calibre-mcp

# Install with pip
pip install -e .

# Or with uv
uv pip install -e .
```

## Getting Started

1. Clone the repo
2. Copy `config.json.example` to `config.json`
3. Edit `config.json` with your paths (`config.json` is gitignored and will not be committed)
4. Install: `pip install -e .` or `uv pip install -e .`

## Configuration

Edit `config.json`:

```json
{
    "calibredb_path": "C:\\Program Files\\Calibre2\\calibredb.exe",
    "library_path": "C:\\Users\\YourName\\Calibre Library",
    "backup_before_bulk_ops": true,
    "dry_run_by_default": true
}
```

## Usage

### As MCP server (Claude Desktop / Cowork)

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
    "mcpServers": {
        "calibre": {
            "command": "python",
            "args": ["-m", "calibre_mcp.server"],
            "cwd": "C:\\path\\to\\calibre-mcp\\src"
        }
    }
}
```

Or with uv:

```json
{
    "mcpServers": {
        "calibre": {
            "command": "uv",
            "args": ["run", "--directory", "C:\\path\\to\\calibre-mcp", "calibre-mcp"]
        }
    }
}
```

### Run tests

```bash
# Unit tests (no Calibre needed)
pytest tests/test_normalize.py -v

# Integration tests (needs access to metadata.db)
pytest tests/test_backend.py -v
```

## Tools

### Query (read-only)
- `library_stats` — Summary of the entire library
- `search_books` — Search by title, author, series, tag, format
- `get_book_details` — Full details for a single book
- `list_authors`, `list_series`, `list_tags`, `list_publishers`

### Diagnosis (read-only)
- `find_duplicate_books` — Exact, ISBN, or fuzzy duplicates
- `find_author_variants` — Same person, different spellings
- `find_series_issues` — Duplicate numbering, gaps
- `find_uppercase_items` — ALL CAPS titles/tags/publishers
- `find_missing_metadata` — No cover, language, tags, files
- `find_orphan_links` — Broken FK references in link tables
- `find_orphan_files` — Directories on disk with no DB record
- `find_ghost_books` — DB records with no directory on disk

### Write (via calibredb)
- `remove_books` — Delete books (dry-run by default)
- `set_book_metadata` — Change title, author, series, etc.
- `backup_database` — Backup metadata.db
- `vacuum_database` — Compact DB after bulk ops

### Bulk cleanup (via calibredb)
- `merge_authors` — Merge variant authors into canonical
- `normalize_uppercase` — Title Case conversion
- `fix_series_numbers` — Reassign series indices
- `fix_author_sort` — Sync author_sort fields
- `cleanup_orphan_links` — Clean broken link table entries

### Reporting
- `fetch_metadata` — Download metadata from internet
- `generate_report` — Full markdown health report
- `compare_snapshots` — Diff between two states

## Safety

- All destructive operations are **dry-run by default**
- Database backup available before bulk operations
- Write operations go through `calibredb.exe` to maintain file/folder integrity
- Direct SQLite writes are only used for `cleanup_orphan_links` (no calibredb equivalent)

## Important: Close Calibre First

`calibredb.exe` cannot operate while Calibre GUI is running (database lock). Close Calibre before using write operations.
