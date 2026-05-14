# Graph Report - C:\Users\xpres\claude\calibre-mcp  (2026-04-18)

## Corpus Check
- 16 files · ~17,535 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 214 nodes · 402 edges · 13 communities detected
- Extraction: 57% EXTRACTED · 43% INFERRED · 0% AMBIGUOUS · INFERRED: 174 edges (avg confidence: 0.73)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `call_tool()` - 43 edges
2. `Config` - 28 edges
3. `CalibreBackend` - 25 edges
4. `CalibreError` - 14 edges
5. `generate_report()` - 10 edges
6. `normalize_for_grouping()` - 9 edges
7. `initials_compatible()` - 9 edges
8. `normalize_uppercase()` - 8 edges
9. `find_author_variants()` - 7 edges
10. `_set_metadata_sqlite()` - 7 edges

## Surprising Connections (you probably didn't know these)
- `Execute a read-only SQL query against metadata.db.          Returns list of dict` --uses--> `Config`  [INFERRED]
  C:\Users\xpres\claude\calibre-mcp\src\calibre_mcp\backend.py → C:\Users\xpres\claude\calibre-mcp\src\calibre_mcp\config.py
- `Execute a query and return the first column of the first row, or None.` --uses--> `Config`  [INFERRED]
  C:\Users\xpres\claude\calibre-mcp\src\calibre_mcp\backend.py → C:\Users\xpres\claude\calibre-mcp\src\calibre_mcp\config.py
- `Open a writable SQLite connection to metadata.db.          Verifies Calibre is c` --uses--> `Config`  [INFERRED]
  C:\Users\xpres\claude\calibre-mcp\src\calibre_mcp\backend.py → C:\Users\xpres\claude\calibre-mcp\src\calibre_mcp\config.py
- `TestQueryDb` --uses--> `CalibreBackend`  [INFERRED]
  C:\Users\xpres\claude\calibre-mcp\tests\test_backend.py → C:\Users\xpres\claude\calibre-mcp\src\calibre_mcp\backend.py
- `backend()` --calls--> `CalibreBackend`  [INFERRED]
  C:\Users\xpres\claude\calibre-mcp\tests\test_backend.py → C:\Users\xpres\claude\calibre-mcp\src\calibre_mcp\backend.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.1
Nodes (26): CalibreBackend, CalibreError, Backend for Calibre operations: calibredb CLI for writes, SQLite for reads., Return True if the Calibre GUI process is currently running., Raise CalibreError if the Calibre GUI is open., Append a structured entry to the operations log (JSONL)., Raised when a calibredb operation fails., Create a backup of metadata.db. Returns path to backup file. (+18 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (31): backup_database(), backup_metadata_opf(), bulk_set_metadata(), check_library(), _cleanup_orphans(), _compute_title_sort(), export_catalog(), fts_index() (+23 more)

### Community 2 - "Community 2"
Cohesion: 0.1
Nodes (24): Execute a query and return the first column of the first row, or None., Execute a read-only SQL query against metadata.db.          Returns list of dict, find_author_variants(), find_duplicate_books(), find_ghost_books(), find_missing_metadata(), find_orphan_files(), find_orphan_links() (+16 more)

### Community 3 - "Community 3"
Cohesion: 0.1
Nodes (13): initials_compatible(), normalize_for_grouping(), parse_author_tokens(), Normalization utilities for author names, titles, and other metadata., Check if two token lists represent the same name with initials.     'H. G.' is c, Remove accents/diacritics from a string., Parse an author name into tokens, handling 'Last, First' and 'Last| First' forma, Normalize a name for duplicate detection.     Strips accents, punctuation, lower (+5 more)

### Community 4 - "Community 4"
Cohesion: 0.11
Nodes (23): Exception, get_book_details(), list_authors(), list_publishers(), list_series(), list_tags(), Query and search tools for the Calibre library (Hito 1)., Get complete details for a single book. (+15 more)

### Community 5 - "Community 5"
Cohesion: 0.15
Nodes (16): Open a writable SQLite connection to metadata.db.          Verifies Calibre is c, _calibre_sanitize(), cleanup_orphan_links(), fix_author_sort(), fix_book_paths(), fix_series_numbers(), merge_authors(), Bulk cleanup operations for the Calibre library (Hito 4). (+8 more)

### Community 6 - "Community 6"
Cohesion: 0.16
Nodes (9): normalize_uppercase(), Convert ALL UPPERCASE titles/tags/publishers to Title Case.      Apply uses di, TestIsAllUppercase, TestSmartTitleCase, is_all_uppercase(), Intelligent title case for Spanish and English text., Convert text to title case with intelligence:     - Spanish/English particles st, Check if a string is ALL UPPERCASE (ignoring non-alpha characters). (+1 more)

### Community 7 - "Community 7"
Cohesion: 0.2
Nodes (9): compare_snapshots(), fetch_metadata(), _query_googlebooks(), _query_openlibrary(), Reporting and metadata fetch tools (Hito 5)., Query Open Library search API. Returns first result or None., Compare current library state with a previous snapshot.      Args:         old_s, Query Google Books API. Returns first result or None. No API key needed for basi (+1 more)

### Community 8 - "Community 8"
Cohesion: 0.25
Nodes (4): _default_calibredb_path(), load(), Configuration loader for Calibre MCP Server., Return the most likely calibredb path for the current platform.

### Community 9 - "Community 9"
Cohesion: 0.67
Nodes (1): MCP tools for Calibre library management.

### Community 10 - "Community 10"
Cohesion: 1.0
Nodes (1): Load config from JSON file. Falls back to defaults if file not found.

### Community 11 - "Community 11"
Cohesion: 1.0
Nodes (0): 

### Community 12 - "Community 12"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **62 isolated node(s):** `Configuration loader for Calibre MCP Server.`, `Return the most likely calibredb path for the current platform.`, `Load config from JSON file. Falls back to defaults if file not found.`, `Return list of validation errors (empty = OK).`, `Bulk cleanup operations for the Calibre library (Hito 4).` (+57 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 10`** (1 nodes): `Load config from JSON file. Falls back to defaults if file not found.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 11`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 12`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `call_tool()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.342) - this node is a cross-community bridge._
- **Why does `CalibreBackend` connect `Community 0` to `Community 2`, `Community 5`?**
  _High betweenness centrality (0.139) - this node is a cross-community bridge._
- **Why does `find_author_variants()` connect `Community 2` to `Community 3`, `Community 4`?**
  _High betweenness centrality (0.121) - this node is a cross-community bridge._
- **Are the 39 inferred relationships involving `call_tool()` (e.g. with `.get_library_stats()` and `raw_sql_query()`) actually correct?**
  _`call_tool()` has 39 INFERRED edges - model-reasoned connections that need verification._
- **Are the 25 inferred relationships involving `Config` (e.g. with `CalibreError` and `CalibreBackend`) actually correct?**
  _`Config` has 25 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `CalibreBackend` (e.g. with `Config` and `Calibre MCP Server — Main entry point.`) actually correct?**
  _`CalibreBackend` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `CalibreError` (e.g. with `Config` and `Calibre MCP Server — Main entry point.`) actually correct?**
  _`CalibreError` has 7 INFERRED edges - model-reasoned connections that need verification._