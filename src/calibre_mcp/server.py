"""Calibre MCP Server — Main entry point."""

import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .backend import CalibreBackend, CalibreError
from .config import Config
from .tools import query, diagnose, write, bulk, report


# ── Server identity (captured at import time = process start) ──────────

def _get_git_commit() -> str:
    """Return the short git commit hash of the running code, or 'unknown'."""
    try:
        import os
        repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=repo_dir, timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


_SERVER_START_TIME = datetime.now(timezone.utc).isoformat()
_SERVER_GIT_COMMIT = _get_git_commit()


# ── Schema helpers ─────────────────────────────────────────────────────

def _permissive_prop(prop: dict) -> dict:
    """Allow string representation in addition to the declared type.
    Needed because Claude Code's MCP client sends all XML parameter values
    as strings; the client-side Zod validator would otherwise reject them."""
    t = prop.get("type")
    if t in ("integer", "number", "boolean"):
        p = {k: v for k, v in prop.items() if k != "type"}
        p["anyOf"] = [{"type": t}, {"type": "string"}]
        return p
    if t == "array":
        p = {k: v for k, v in prop.items() if k not in ("type", "items")}
        array_schema = {"type": "array"}
        if "items" in prop:
            array_schema["items"] = prop["items"]
        p["anyOf"] = [array_schema, {"type": "string"}]
        return p
    if t == "object":
        p = {k: v for k, v in prop.items() if k != "type"}
        p["anyOf"] = [{"type": "object"}, {"type": "string"}]
        return p
    return prop


def _permissive(schema: dict) -> dict:
    """Recursively relax a tool inputSchema so typed fields also accept strings."""
    if "properties" not in schema:
        return schema
    return {**schema, "properties": {k: _permissive_prop(v) for k, v in schema["properties"].items()}}


# ── Argument coercion ──────────────────────────────────────────────────

def _coerce(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Convert string arguments to the proper Python types expected by handlers.
    Mirrors the schema relaxation done in _permissive_prop."""
    tool = next((t for t in TOOLS if t.name == name), None)
    if not tool:
        return args
    props = tool.inputSchema.get("properties", {})
    out = dict(args)
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        # Look at anyOf to find the intended non-string type
        prop = props.get(key, {})
        intended = None
        for alt in prop.get("anyOf", []):
            if alt.get("type") != "string":
                intended = alt.get("type")
                break
        if intended is None:
            intended = prop.get("type")  # fallback for non-permissive props
        if intended == "integer":
            try:
                out[key] = int(value)
            except (ValueError, TypeError):
                pass
        elif intended == "number":
            try:
                out[key] = float(value)
            except (ValueError, TypeError):
                pass
        elif intended == "boolean":
            out[key] = value.lower() in ("true", "yes", "1")
        elif intended == "array":
            try:
                out[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
        elif intended == "object":
            try:
                out[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
    return out

# ── Logging ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("calibre_mcp")

# ── Server setup ───────────────────────────────────────────────────────

server = Server("calibre-mcp")
config = Config.load()
backend = CalibreBackend(config)


def _json_response(data: Any) -> list[TextContent]:
    """Helper to return JSON-formatted MCP text content."""
    return [TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False, default=str))]


def _error_response(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": msg}, ensure_ascii=False))]


# ── Tool definitions ───────────────────────────────────────────────────

TOOLS = [
    # ── Server info ────────────────────────────────────────────────────
    Tool(
        name="get_server_info",
        description=(
            "Return the git commit hash and start time of the RUNNING server process. "
            "Call this after committing MCP code changes to verify the server has been "
            "restarted and is running the new code. If git_commit differs from the latest "
            "commit, the server must be restarted before testing the changes."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    # ── Stats ──────────────────────────────────────────────────────────
    Tool(
        name="library_stats",
        description="Get summary statistics of the Calibre library: total books, authors, series, tags, formats, missing metadata counts, and database size.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),

    # ── Query ──────────────────────────────────────────────────────────
    Tool(
        name="search_books",
        description=(
            "Search books. Two modes: (1) Structured: use title, author, series, tag, etc. params for SQL-based search. "
            "(2) Native: pass query_string using Calibre's search syntax (e.g., 'author:King AND tag:horror'). "
            "Native mode calls calibredb search directly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Filter by title (partial match)"},
                "author": {"type": "string", "description": "Filter by author name (partial match)"},
                "series": {"type": "string", "description": "Filter by series name (partial match)"},
                "tag": {"type": "string", "description": "Filter by tag (partial match)"},
                "publisher": {"type": "string", "description": "Filter by publisher (partial match)"},
                "format": {"type": "string", "description": "Filter by format (e.g., EPUB, PDF, FB2)"},
                "identifier": {"type": "string", "description": "Filter by identifier value (ISBN, etc.)"},
                "has_cover": {"type": "boolean", "description": "Filter by cover presence"},
                "has_formats": {"type": "boolean", "description": "Filter by file presence"},
                "query_string": {"type": "string", "description": "Calibre native search query (e.g., 'author:King AND tag:horror'). If provided, other filters are ignored."},
                "limit": {"type": "integer", "default": 50, "description": "Max results (default 50)"},
                "offset": {"type": "integer", "default": 0, "description": "Offset for pagination"},
            },
        },
    ),
    Tool(
        name="get_book_details",
        description="Get complete details for a single book by its ID: title, authors, series, tags, publisher, formats, identifiers, languages, description.",
        inputSchema={
            "type": "object",
            "properties": {
                "book_id": {"type": "integer", "description": "Book ID"},
            },
            "required": ["book_id"],
        },
    ),
    Tool(
        name="list_authors",
        description="List authors with book counts. Can filter by name and minimum book count.",
        inputSchema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter by name (partial match)"},
                "min_books": {"type": "integer", "default": 0, "description": "Minimum number of books"},
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
                "sort_by": {"type": "string", "enum": ["name", "books"], "default": "name"},
            },
        },
    ),
    Tool(
        name="list_series",
        description="List series with book counts and number range.",
        inputSchema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter by name (partial match)"},
                "min_books": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    Tool(
        name="list_tags",
        description="List tags with book counts.",
        inputSchema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter by name (partial match)"},
                "min_books": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    Tool(
        name="list_publishers",
        description="List publishers with book counts.",
        inputSchema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter by name (partial match)"},
                "min_books": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),

    # ── Diagnosis ──────────────────────────────────────────────────────
    Tool(
        name="raw_sql_query",
        description=(
            "Execute a read-only SQL SELECT query directly against metadata.db. "
            "Useful for ad-hoc analysis that existing tools don't cover: finding "
            "patterns, cross-table aggregations, custom filters, etc. "
            "Only SELECT is allowed — write operations are blocked. "
            "Useful tables: books, authors, series, tags, publishers, "
            "books_authors_link, books_series_link, books_tags_link, "
            "books_publishers_link, books_languages_link, identifiers, "
            "data (formats), comments."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A SELECT query against metadata.db",
                },
                "params": {
                    "type": "array",
                    "items": {},
                    "description": "Optional positional parameters for ? placeholders",
                },
                "limit": {
                    "type": "integer",
                    "default": 200,
                    "description": "Max rows to return (hard cap: 1000)",
                },
            },
            "required": ["sql"],
        },
    ),

    Tool(
        name="find_duplicate_books",
        description="Find duplicate books. Strategies: 'exact' (same title+author), 'isbn' (same ISBN), 'fuzzy' (normalized title+author).",
        inputSchema={
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "enum": ["exact", "isbn", "fuzzy"], "default": "exact"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="find_author_variants",
        description="Find groups of authors that appear to be the same person (case variants, initials vs full name, accents). Uses prefix-indexed initials matching for performance.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="find_series_issues",
        description="Find series with problems: duplicate numbering, unnumbered books.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="find_uppercase_items",
        description="Find titles, authors, tags, publishers, or series in ALL UPPERCASE.",
        inputSchema={
            "type": "object",
            "properties": {
                "item_type": {"type": "string", "enum": ["all", "titles", "authors", "tags", "publishers", "series"], "default": "all"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="find_missing_metadata",
        description="Find books missing cover, language, tags, formats, or publisher.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="find_orphan_links",
        description="Find orphan entries in link tables (book ID no longer exists). These cause FK constraint errors.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="find_orphan_files",
        description="Find directories in the library with no matching DB record. Tries calibredb check_library first, falls back to manual filesystem scan.",
        inputSchema={
            "type": "object",
            "properties": {
                "use_calibredb": {"type": "boolean", "default": True, "description": "Use calibredb check_library (recommended) or manual scan"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="find_ghost_books",
        description=(
            "Find books in DB whose directory or format files don't exist on disk. "
            "Checks both missing directories AND missing format files within existing directories. "
            "Default (limit=0) scans ALL books — only this mode gives a trustworthy result. "
            "Partial scans (limit>0) return scan_status=INCOMPLETE and MUST NOT be interpreted as 'no ghosts found'. "
            "Set use_calibredb=true to use calibredb check_library instead (may time out on large libraries)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "use_calibredb": {"type": "boolean", "default": False, "description": "Use calibredb check_library instead of manual scan"},
                "limit":  {"type": "integer", "default": 0, "description": "Books to check (0=ALL, recommended). Use >0 only for pagination. Partial scans are INCOMPLETE."},
                "offset": {"type": "integer", "default": 0, "description": "Skip this many books (pagination only, use with limit>0)"},
            },
        },
    ),
    Tool(
        name="verify_file_integrity",
        description=(
            "Verify that every format file referenced in the DB exists on disk. "
            "Run this after any rename operation (normalize_uppercase, rename_author, merge_authors, "
            "bulk_set_metadata with title/author) to confirm no files were lost. "
            "Returns ok=true if all files are present, ok=false with a list of missing files otherwise. "
            "Pass book_ids to check only specific books (fast); omit to check all books (may take minutes)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "book_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional list of book IDs to check. If omitted, checks all books.",
                },
            },
        },
    ),

    # ── Write operations ───────────────────────────────────────────────
    Tool(
        name="remove_books",
        description="Remove books from the library by IDs (deletes files from disk). Dry-run by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "book_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of book IDs to remove"},
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["book_ids"],
        },
    ),
    Tool(
        name="set_book_metadata",
        description="Set metadata fields on a book. Supported fields: title, authors, series, series_index, tags, publisher, pubdate, comments, languages, identifiers. Dry-run by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "book_id": {"type": "integer", "description": "Book ID to modify"},
                "fields": {
                    "type": "object",
                    "description": "Dict of field_name: new_value",
                    "additionalProperties": {"type": "string"},
                },
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["book_id", "fields"],
        },
    ),
    Tool(
        name="backup_database",
        description="Create a full backup copy of metadata.db (SQLite file).",
        inputSchema={
            "type": "object",
            "properties": {
                "suffix": {"type": "string", "description": "Optional suffix for backup filename (default: timestamp)"},
            },
        },
    ),
    Tool(
        name="vacuum_database",
        description="Compact the database after bulk operations. Requires Calibre to be closed.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="check_library",
        description="Run Calibre's native integrity checker (calibredb check_library). Finds: books without files, extra files not in DB, missing covers, invalid directories.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="embed_metadata",
        description=(
            "Embed metadata from the Calibre DB into actual book files (.epub, .fb2, etc.). "
            "After changing titles/authors via set_metadata, files still contain old metadata internally. "
            "This syncs them. Can process specific books or all books."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "book_ids": {"type": "array", "items": {"type": "integer"}, "description": "Specific book IDs. If omitted, processes ALL books."},
                "only_formats": {"type": "string", "description": "Comma-separated formats to process (e.g., 'epub,fb2'). If omitted, all formats."},
                "dry_run": {"type": "boolean", "default": True},
            },
        },
    ),
    Tool(
        name="backup_metadata_opf",
        description="Export per-book metadata to OPF files in each book's directory (calibredb backup_metadata). Different from backup_database — this is per-book, useful against DB corruption.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="export_catalog",
        description="Export the library catalog to a file (CSV, XML, EPUB, or MOBI) using calibredb catalog.",
        inputSchema={
            "type": "object",
            "properties": {
                "output_file": {"type": "string", "default": "catalog.csv", "description": "Output filename (supported: .csv, .xml, .epub, .mobi)"},
                "fields": {"type": "string", "description": "Comma-separated field list (e.g., 'title,authors,series,tags'). Default: all."},
            },
        },
    ),
    Tool(
        name="fts_index",
        description="Manage the full-text search index for searching inside book contents. Actions: 'status', 'enable', 'disable', 'reindex'.",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "enable", "disable", "reindex"], "default": "status"},
            },
        },
    ),
    Tool(
        name="fts_search",
        description="Full-text search inside book contents. Requires FTS index enabled first (use fts_index action=enable).",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["query"],
        },
    ),

    # ── Bulk operations ────────────────────────────────────────────────
    Tool(
        name="rename_author",
        description=(
            "Rename an author directly in the database. "
            "Unlike set_book_metadata, this bypasses calibredb's case-insensitive "
            "author matching and can perform case-only renames (e.g. BOECIO → Boecio). "
            "Updates both the authors table and the denormalised author_sort on all "
            "books by that author. Dry-run by default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "author_id": {"type": "integer", "description": "Author ID to rename"},
                "new_name":  {"type": "string",  "description": "New display name"},
                "new_sort":  {"type": "string",  "description": "New sort key (defaults to new_name)"},
                "dry_run":   {"type": "boolean", "default": True},
            },
            "required": ["author_id", "new_name"],
        },
    ),
    Tool(
        name="bulk_set_metadata",
        description=(
            "Apply metadata changes to multiple books in a single call. "
            "Accepts a list of {book_id, fields} objects — same fields as "
            "set_book_metadata. Much more efficient than calling set_book_metadata "
            "repeatedly for series disambiguations, bulk renames, etc. Dry-run by default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "description": "List of {book_id, fields} objects",
                    "items": {
                        "type": "object",
                        "properties": {
                            "book_id": {"type": "integer"},
                            "fields":  {"type": "object", "additionalProperties": True},
                        },
                        "required": ["book_id", "fields"],
                    },
                },
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["updates"],
        },
    ),
    Tool(
        name="merge_authors",
        description="Merge author variants into a canonical author. Re-links all books via calibredb. Dry-run by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "canonical_id": {"type": "integer", "description": "Author ID to keep as canonical"},
                "variant_ids": {"type": "array", "items": {"type": "integer"}, "description": "Author IDs to merge into canonical"},
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["canonical_id", "variant_ids"],
        },
    ),
    Tool(
        name="normalize_uppercase",
        description="Convert ALL UPPERCASE titles/tags/publishers/series to Title Case (smart, with Spanish/English particles and acronym handling). For series: automatically merges into existing series if normalized name conflicts. Dry-run by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "item_type": {"type": "string", "enum": ["titles", "tags", "publishers", "series"]},
                "limit": {"type": "integer", "default": 100},
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["item_type"],
        },
    ),
    Tool(
        name="fix_series_numbers",
        description="Fix series numbering by assigning specific indices to books. Dry-run by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "series_id": {"type": "integer", "description": "Series ID to fix"},
                "assignments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "book_id": {"type": "integer"},
                            "index": {"type": "number"},
                        },
                        "required": ["book_id", "index"],
                    },
                },
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["series_id", "assignments"],
        },
    ),
    Tool(
        name="fix_author_sort",
        description="Synchronize books.author_sort with linked authors' sort fields. Uses per-book queries with consistent ordering (not GROUP_CONCAT). Dry-run by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
                "dry_run": {"type": "boolean", "default": True},
            },
        },
    ),
    Tool(
        name="find_orphaned_metadata",
        description=(
            "Find (and optionally delete) orphaned rows in authors, series, tags, and publishers — "
            "entries that no longer have any books linked to them. "
            "These accumulate after remove_books, merge_authors, or bulk deletions. "
            "Dry-run by default: lists orphans without deleting. Set dry_run=false to clean them up."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "default": True},
            },
        },
    ),
    Tool(
        name="cleanup_orphan_links",
        description="Remove orphan entries from link tables where book ID no longer exists. Direct SQLite write — requires Calibre closed. Dry-run by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "default": True},
            },
        },
    ),
    Tool(
        name="fix_book_paths",
        description=(
            "Rename book directories on disk to match current author/title in DB, "
            "and update books.path accordingly. Use after bulk metadata edits that "
            "bypassed calibredb (direct SQLite), which leaves directory names stale. "
            "Can target specific book_ids or scan all books. Dry-run by default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "book_ids": {"type": "array", "items": {"type": "integer"},
                             "description": "Specific book IDs. If omitted, scans ALL books."},
                "dry_run": {"type": "boolean", "default": True},
            },
        },
    ),

    # ── Reporting & metadata ───────────────────────────────────────────
    Tool(
        name="fetch_metadata",
        description=(
            "Fetch metadata from the internet (Open Library or Google Books) for specified books. "
            "calibredb has no fetch-metadata command — this queries APIs directly. "
            "With apply=true and dry_run=false, writes found metadata back via calibredb set_metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "book_ids": {"type": "array", "items": {"type": "integer"}},
                "source": {"type": "string", "enum": ["openlibrary", "googlebooks"], "default": "openlibrary"},
                "apply": {"type": "boolean", "default": False, "description": "If true, write found metadata back to the library"},
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["book_ids"],
        },
    ),
    Tool(
        name="generate_report",
        description="Generate a comprehensive markdown report of the library's current state including stats, issues, and recommendations.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="compare_snapshots",
        description="Compare current library state with a previous snapshot from library_stats.",
        inputSchema={
            "type": "object",
            "properties": {
                "old_stats": {"type": "object", "description": "Previous output from library_stats"},
            },
            "required": ["old_stats"],
        },
    ),
]

# Apply permissive schemas so Claude Code's client accepts string-typed params
TOOLS = [
    Tool(name=t.name, description=t.description, inputSchema=_permissive(t.inputSchema))
    for t in TOOLS
]


# ── Tool listing ───────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


# ── Tool dispatch ──────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    arguments = _coerce(name, arguments)
    try:
        match name:
            # Server info
            case "get_server_info":
                return _json_response({
                    "git_commit": _SERVER_GIT_COMMIT,
                    "started_at": _SERVER_START_TIME,
                    "note": (
                        "If git_commit does not match the latest commit in the repo, "
                        "restart the MCP server before testing code changes."
                    ),
                })

            # Stats
            case "library_stats":
                return _json_response(backend.get_library_stats())

            # Query
            case "raw_sql_query":
                return _json_response(query.raw_sql_query(backend, **arguments))
            case "search_books":
                return _json_response(query.search_books(backend, **arguments))
            case "get_book_details":
                result = query.get_book_details(backend, **arguments)
                if result is None:
                    return _error_response(f"Book ID {arguments.get('book_id')} not found.")
                return _json_response(result)
            case "list_authors":
                return _json_response(query.list_authors(backend, **arguments))
            case "list_series":
                return _json_response(query.list_series(backend, **arguments))
            case "list_tags":
                return _json_response(query.list_tags(backend, **arguments))
            case "list_publishers":
                return _json_response(query.list_publishers(backend, **arguments))

            # Diagnosis
            case "find_duplicate_books":
                return _json_response(diagnose.find_duplicate_books(backend, **arguments))
            case "find_author_variants":
                return _json_response(diagnose.find_author_variants(backend, **arguments))
            case "find_series_issues":
                return _json_response(diagnose.find_series_issues(backend, **arguments))
            case "find_uppercase_items":
                return _json_response(diagnose.find_uppercase_items(backend, **arguments))
            case "find_missing_metadata":
                return _json_response(diagnose.find_missing_metadata(backend, **arguments))
            case "find_orphan_links":
                return _json_response(diagnose.find_orphan_links(backend))
            case "find_orphan_files":
                return _json_response(diagnose.find_orphan_files(backend, **arguments))
            case "find_ghost_books":
                return _json_response(diagnose.find_ghost_books(backend, **arguments))
            case "verify_file_integrity":
                return _json_response(diagnose.verify_file_integrity(backend, **arguments))

            # Write operations
            case "remove_books":
                result = write.remove_books(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "set_book_metadata":
                result = write.set_book_metadata(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "backup_database":
                return _json_response(write.backup_database(backend, **arguments))
            case "vacuum_database":
                return _json_response(write.vacuum_database(backend))
            case "check_library":
                return _json_response(write.check_library(backend))
            case "embed_metadata":
                return _json_response(write.embed_metadata(backend, **arguments))
            case "backup_metadata_opf":
                return _json_response(write.backup_metadata_opf(backend))
            case "export_catalog":
                return _json_response(write.export_catalog(backend, **arguments))
            case "fts_index":
                return _json_response(write.fts_index(backend, **arguments))
            case "fts_search":
                return _json_response(write.fts_search(backend, **arguments))

            # Bulk
            case "rename_author":
                result = bulk.rename_author(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "bulk_set_metadata":
                result = write.bulk_set_metadata(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "merge_authors":
                result = bulk.merge_authors(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "normalize_uppercase":
                result = bulk.normalize_uppercase(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "fix_series_numbers":
                result = bulk.fix_series_numbers(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "fix_author_sort":
                result = bulk.fix_author_sort(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "find_orphaned_metadata":
                result = bulk.find_orphaned_metadata(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "cleanup_orphan_links":
                result = bulk.cleanup_orphan_links(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)
            case "fix_book_paths":
                result = bulk.fix_book_paths(backend, **arguments)
                backend.log_operation(name, arguments, result)
                return _json_response(result)

            # Report
            case "fetch_metadata":
                return _json_response(report.fetch_metadata(backend, **arguments))
            case "generate_report":
                return [TextContent(type="text", text=report.generate_report(backend))]
            case "compare_snapshots":
                return _json_response(report.compare_snapshots(backend, **arguments))

            case _:
                return _error_response(f"Unknown tool: {name}")

    except CalibreError as e:
        logger.error(f"CalibreError in {name}: {e}")
        return _error_response(str(e))
    except Exception as e:
        logger.exception(f"Unexpected error in {name}")
        return _error_response(f"Internal error: {type(e).__name__}: {e}")


# ── Entry point ────────────────────────────────────────────────────────

async def run():
    """Run the MCP server via stdio transport."""
    errors = config.validate()
    if errors:
        logger.warning(f"Configuration warnings: {errors}")
        logger.warning("Some tools may fail. Fix config.json and restart.")

    logger.info(f"Starting Calibre MCP Server v0.2.0")
    logger.info(f"Library: {config.library_path}")
    logger.info(f"calibredb: {config.calibredb_path}")
    logger.info(f"Dry-run by default: {config.dry_run_by_default}")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    main()
