# Calibre MCP Server

MCP (Model Context Protocol) server for managing a Calibre ebook library. Provides 40+ tools for querying, diagnosing, and cleaning up your library — including duplicate detection, author normalization, series ordering, metadata completion, and high-level workflow helpers.

---

## ⚠️ WARNING: This server can delete files

The `remove_books` tool **deletes book files from disk**.

- Deleted book directories are sent to the OS **Recycle Bin / Trash** via [`send2trash`](https://pypi.org/project/Send2Trash/) (a required dependency), so an accidental deletion is recoverable until you empty the bin. If `send2trash` is somehow unavailable, the tool falls back to **permanent** deletion and logs a warning.
- All destructive tools default to `dry_run=true` — they show what *would* happen without doing anything
- You must explicitly pass `dry_run=false` to apply changes
- **Back up your library before using any write operation on a large dataset** — the Recycle Bin is a safety net, not a backup
- The server verifies Calibre is closed before any direct database write — but it cannot protect you from confirming a deletion you did not intend

**If you are not sure what a tool does, run it with `dry_run=true` first. Always.**

---

## Architecture

| Layer | How |
|-------|-----|
| Reads | Direct SQLite queries on `metadata.db` (fast, no lock required) |
| Writes | Via `calibredb` CLI (handles files, folders, and triggers correctly) |
| Bulk writes | Direct SQLite for operations `calibredb` cannot do in bulk (normalize, cleanup) |
| Transport | MCP stdio |

## Requirements

- Python 3.10+
- [Calibre](https://calibre-ebook.com/) installed (for `calibredb`)
- Claude Desktop or any MCP-compatible client

## Installation

```bash
git clone https://github.com/Xpresi/calibre-mcp
cd calibre-mcp

# With pip
pip install -e .

# Or with uv
uv pip install -e .
```

## Configuration

```bash
cp config.json.example config.json
```

Edit `config.json` with your paths. The file is gitignored and will never be committed.

```json
{
    "calibredb_path": "C:\\Program Files\\Calibre2\\calibredb.exe",
    "library_path":   "C:\\Users\\YourName\\Calibre Library",
    "backup_before_bulk_ops": true,
    "dry_run_by_default":     true
}
```

With `backup_before_bulk_ops` on, any tool that really writes to `metadata.db`
(`dry_run=false`) copies the database to `metadata_backup_auto_<timestamp>.db`
next to the library first, and reports the path alongside its result. A backup
younger than 30 minutes is reused instead of re-copied, so a batched job does
not produce one copy per call. If the backup cannot be written the operation is
aborted rather than run unprotected.

Platform defaults if `calibredb_path` is omitted:

| Platform | Default path |
|----------|-------------|
| Windows  | `C:\Program Files\Calibre2\calibredb.exe` |
| macOS    | `/Applications/calibre.app/Contents/MacOS/calibredb` |
| Linux    | `/usr/bin/calibredb` |

See `config.json.example` for annotated examples for all platforms.

## Claude Desktop setup

Add to `claude_desktop_config.json`:

```json
{
    "mcpServers": {
        "calibre": {
            "command": "python",
            "args": ["-m", "calibre_mcp.server"]
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
            "args": ["run", "--directory", "/path/to/calibre-mcp", "calibre-mcp"]
        }
    }
}
```

## Important: Close Calibre before write operations

`calibredb` cannot run while the Calibre GUI is open (database lock). The server detects this automatically and returns a clear error if Calibre is running. Close Calibre, then retry.

---

## Tools reference

### Query — read-only

| Tool | Description |
|------|-------------|
| `get_server_info` | Server start time, git commit, library path |
| `library_stats` | Summary counts: books, authors, series, formats, languages |
| `search_books` | Search by title, author, series, tag, or format |
| `get_book_details` | Full metadata for a single book |
| `raw_sql_query` | Run any SELECT query directly on `metadata.db` |
| `list_authors` | Pageable list of all authors |
| `list_series` | Pageable list of all series |
| `list_tags` | All tags with book counts |
| `list_publishers` | All publishers with book counts |
| `get_epub_toc` | Extract TOC from an EPUB and flag likely scan-EPUBs |

### Diagnosis — read-only

| Tool | Description |
|------|-------------|
| `find_duplicate_books` | Exact, ISBN, or fuzzy duplicates |
| `find_author_variants` | Same person, different spellings |
| `find_series_issues` | Gaps and duplicate numbering in series |
| `find_uppercase_items` | ALL CAPS titles, authors, tags, publishers, or series |
| `find_missing_metadata` | Books without cover, language, tags, or files |
| `find_orphan_links` | Broken foreign-key references in link tables |
| `find_orphan_files` | Directories on disk with no matching DB record |
| `find_ghost_books` | DB records pointing to non-existent directories |
| `find_orphaned_metadata` | Authors/series/tags/publishers with no books |
| `verify_file_integrity` | Confirm every DB-referenced file exists on disk (case-aware at both the author and book directory levels) |
| `check_library` | Run Calibre's native integrity checker |

### ⚠️ Write — modifies your library

| Tool | Risk | Description |
|------|------|-------------|
| `remove_books` | **DELETES FILES FROM DISK** | Remove books by ID. Dry-run by default. |
| `set_book_metadata` | Modifies DB | Change title, author, series, publisher, etc. for one book |
| `bulk_set_metadata` | Modifies DB | Batch metadata update across many books |
| `merge_authors` | Modifies DB | Merge variant author names into a canonical one |
| `rename_author` | Modifies DB + filesystem | Rename an author (also renames their directory) |
| `normalize_uppercase` | Modifies DB | Convert ALL CAPS titles/tags/publishers/series to Title Case |
| `fix_series_numbers` | Modifies DB | Reassign series index values |
| `fix_author_sort` | Modifies DB | Sync `author_sort` fields with actual author names |
| `fix_book_paths` | Modifies DB + filesystem | Rename book directories to match current DB metadata |
| `cleanup_orphan_links` | Modifies DB | Remove broken entries from link tables |
| `fetch_metadata` | Modifies DB | Download and apply metadata from OpenLibrary or Google Books |
| `embed_metadata` | Modifies files | Write DB metadata into epub/fb2 file headers |
| `backup_database` | None | Copy `metadata.db` to a timestamped backup file |
| `backup_metadata_opf` | None | Write per-book OPF files (resilient to DB corruption) |
| `vacuum_database` | None | Compact DB after bulk deletions |

### Reporting

| Tool | Description |
|------|-------------|
| `generate_report` | Full markdown health report of the library |
| `compare_snapshots` | Diff between two library states |
| `export_catalog` | Export library catalog as CSV, XML, EPUB, or MOBI |
| `fts_index` | Enable, disable, or reindex full-text search |
| `fts_search` | Full-text search inside book contents |

### Workflow — high-level helpers

Compose lower-level tools into one-shot answers, useful when iterating on author cleanup.

| Tool | Description |
|------|-------------|
| `analyze_author` | Mega-analysis for a single author: compilation coverage, duplicates, similar-named authors, series gaps, metadata gaps, prioritised actions |
| `find_compilation_coverage` | Detect which individual books are covered by which omnibus/compilation EPUBs (uses TOC cache) |
| `suggest_dedup_resolution` | Given a group of duplicates, propose which copy to keep (format > cover > comments > size > recency) |
| `next_author_to_process` | Return the next N authors not yet logged in `calibre-hecho.md` |
| `regenerate_pending_worklist` | Write `calibre-pendientes.md` checklist of pending authors (read/edit per session, mark `[x]` as you go) |

---

## Safety design

- **Dry-run by default**: every destructive tool returns a preview unless `dry_run=false` is explicitly set
- **Calibre-closed check**: direct SQLite write operations verify the Calibre GUI is not running before touching the database
- **`remove_books` deletes physical files**: it removes the book's DB records (via direct SQLite) and sends the book directory to the OS Recycle Bin / Trash through `send2trash`. Deletions are therefore recoverable until the bin is emptied; if `send2trash` is unavailable it falls back to permanent removal. Use `dry_run=true` to preview exactly which files will be removed before confirming.
- **`backup_database` only backs up `metadata.db`**, not the physical book files. For full library backups, copy the entire library folder manually.

## Running tests

```bash
# Unit tests (no Calibre installation needed)
pytest tests/test_normalize.py -v

# Integration tests (requires a real metadata.db)
pytest tests/test_backend.py -v
```

## License

MIT
