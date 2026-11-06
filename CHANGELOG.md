# Changelog

## Unreleased

### Fixed — data loss risks

- **`fix_book_paths` no longer splits a book across two directories.** When the
  destination already existed it moved the free names, skipped the rest, then
  `rmdir()` failed on the leftover `metadata.opf`/`cover.jpg` — after the epub
  had moved. `books.path` kept the old directory (a ghost book) and every
  re-run hit the same error. The merge is now planned first: identical
  duplicates are dropped, any differing file or directory collision aborts
  with nothing moved.
- **`fetch_metadata(apply=true)` only fills empty fields, from verified hits.**
  It used to delete all of a book's tags and overwrite publisher/language with
  the API's top hit, even when that hit was a different book. Hits now need a
  title and author match (`match_rejected` otherwise), searches use the
  author's display name, and the call takes an automatic backup.
- **The "Calibre is closed" guard fails closed.** If `tasklist`/`pgrep` could
  not run it answered "closed". It now refuses to write, also detects
  `calibre-server`, and `vacuum_database` checks it too.
- **Backups use SQLite's backup API** instead of a raw file copy, which missed
  committed pages still in `metadata.db-wal`. Backups are written to a
  `.partial` name and renamed on success.
- **Write connections no longer switch the library to WAL.** `journal_mode` is
  persistent; the library's own mode is now left alone.
- **Boolean arguments are parsed strictly.** An unrecognised string such as
  `dry_run="dry"` became `False` — a real run. It is now rejected. A
  `dry_run=0` now also triggers the automatic backup.

### Fixed — other

- `mcp` is pinned to `<2`: mcp 2.x renamed `Tool.inputSchema`, so a fresh
  install could not even import the server.
- Authors created by `set_book_metadata`/`bulk_set_metadata` get a proper
  `Last, First` sort instead of their display name; existing-author lookup
  prefers an exact name match.
- `remove_books` dry run lists the directory each book would remove.

### Removed

- `dry_run_by_default`. It was never read by anything; wiring it up would have
  let a call that omits `dry_run` delete books. A config that still sets it
  logs a warning.

## 0.3.0 — 2026-07-25

Safety release. Every change below came out of auditing the server against a
real 68,006-book library, and several close a path to data loss.

### Fixed — data loss risks

- **`books.path` is validated before any filesystem operation.** `library / ""`
  evaluates to the library root itself, so a book whose `path` was empty made
  `remove_books` aim the Recycle Bin — and `fix_book_paths` a move — at the
  **entire library**. Paths must now be relative, traversal-free and exactly
  `Author/Title (id)`. `remove_books` screens every path up front and aborts
  without deleting so much as a database row; `fix_book_paths` skips and
  reports offenders.
- **`verify_file_integrity` now checks directory casing at every level.** It
  only inspected the book directory, so a miscased *author* directory
  (`ABAD, MERCEDES` vs `Abad, Mercedes`) still returned `ok: true` on Windows —
  the false all-clear that preceded a mass deletion of "orphans". The result
  reports which level mismatched.
- **Orphan emptiness is measured recursively.** `file_count` came from a
  top-level `os.listdir`, so a directory whose files sat in a subdirectory
  reported `0` — indistinguishable from empty, and empty is what callers delete
  on. Entries now carry `file_count` (any depth), `subdir_count` and
  `recursively_empty`; an unreadable directory reports `-1`, never `0`.
- **`fix_book_paths` no longer repoints `books.path` at a directory it has not
  verified.** The "source gone, destination exists" branch updated the database
  without checking the book was actually in the destination, which manufactures
  a ghost book *and* makes the directory still holding the only copy look like a
  deletable orphan. It now verifies every format file from `data` and errors out
  otherwise.

### Fixed — results that could not be trusted

- **`find_orphan_files` always scans the whole library.** It used to stop the
  walk once `limit` orphans were found, which truncated the case-mismatch list
  too: `case_mismatch_count: 0` could appear for a library full of them. `limit`
  now caps only the listing; counts are complete and `scan_status` is explicit.
  In `use_calibredb=true` mode `orphans` and `case_mismatches` are `null` rather
  than absent, so unstructured output cannot be read as "none found".
- **`fix_author_sort` scans every book.** It fetched only `limit * 10` books
  before filtering, so the default `limit=100` examined 1,000 books out of
  68,006 and could report a clean library while ~15,000 entries were stale.
  `limit` now caps only how many fixes are applied, and `total_found` /
  `remaining` are reported.
- **Orphan metadata cleanup uses `NOT EXISTS` instead of `NOT IN`.** A single
  NULL in a link column made the condition NULL for every row, so the delete
  silently matched nothing.

### Added

- **`backup_before_bulk_ops` finally does something.** The setting was declared
  in `config.json` and read by nothing. A backup is now taken before any
  database-writing tool runs for real (`dry_run: false`), written as
  `metadata_backup_auto_<timestamp>.db`, reused if younger than 30 minutes so
  batched jobs do not copy per call, and reported alongside the result. **If the
  backup cannot be written, the operation is aborted.**
- `set_book_metadata` and `bulk_set_metadata` report the `next_steps` for path
  drift when `title` or `authors` change. They were the tools most used for that
  edit and the only write tools that said nothing about the stale `books.path`
  they leave behind.

### Changed

- `raw_sql_query` accepts read-only `WITH ... SELECT` CTEs. Writes are still
  refused, and the connection remains read-only.
- `fix_author_sort` is roughly 250× faster on a full pass (two set-based queries
  instead of one query per book: 0.4 s instead of ~17 minutes on 68k books).
- `verify_file_integrity` caches directory listings instead of re-listing each
  author directory once per format row.
- `calibre_sanitize` re-strips after truncating at 200 characters, so a cut
  landing on a space or period cannot produce a name Windows silently alters.
  Non-ASCII characters are still **not** transliterated — verified against the
  reference library, whose directories keep their accents.

### Notes for upgraders

Result shapes changed in `find_orphan_files` (recursive counts, `scan_status`),
`fix_author_sort` (`total_found`, `remaining`) and `remove_books` (new
`aborted_unsafe_path` status). Anything parsing those outputs should be
rechecked. Automatic backups are on by default whenever
`backup_before_bulk_ops` is true.

## 0.2.0

- `remove_books` deletes and commits all database rows before touching any
  file, so a failed file removal leaves recoverable orphan directories instead
  of ghost books.
- `fix_book_paths` batch pagination advances past errored entries only, instead
  of by the full batch size, which used to skip pending fixes.
