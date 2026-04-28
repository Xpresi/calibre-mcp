"""Write operations for the Calibre library via calibredb CLI (Hito 3)."""

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("calibre_mcp")


# ── SQLite helpers for fast metadata writes ────────────────────────────────

def _compute_title_sort(title: str) -> str:
    """Remove leading articles so 'El capitán' sorts as 'capitán, El'."""
    m = re.match(
        r'^(el|la|los|las|un|una|the|a|an|das|die|der|le|les|une)\s+',
        title, re.IGNORECASE,
    )
    if m:
        return title[m.end():].strip() + ", " + title[:m.end()].strip()
    return title


def _set_metadata_sqlite(backend, book_id: int, fields: dict) -> dict:
    """
    Apply metadata changes directly via SQLite.
    Much faster than calibredb for large libraries (no library scan).
    Handles: title, authors, series, series_index, publisher.
    """
    try:

        conn = backend.connect_write(timeout=15)
        conn.execute("BEGIN")
        changed = []

        if "title" in fields:
            title = fields["title"]
            title_sort = _compute_title_sort(title)
            conn.execute(
                "UPDATE books SET title=?, sort=? WHERE id=?",
                (title, title_sort, book_id),
            )
            changed.append(f"title → {title}")

        if "authors" in fields:
            author_val = fields["authors"]

            # Accept both a single string and a list of strings
            if isinstance(author_val, str):
                author_names = [author_val]
            else:
                author_names = [str(a) for a in author_val]

            author_ids: list[int] = []
            author_sort_keys: list[str] = []

            for aname in author_names:
                # Find existing author by sort key or exact name
                row = conn.execute(
                    "SELECT id, name, sort FROM authors "
                    "WHERE sort=? OR name=? LIMIT 1",
                    (aname, aname),
                ).fetchone()

                if row:
                    aid, aname_canon, asort = row
                else:
                    # Create new author entry
                    conn.execute(
                        "INSERT INTO authors(name, sort, link) VALUES(?,?,'')",
                        (aname, aname),
                    )
                    aid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    aname_canon, asort = aname, aname

                author_ids.append(aid)
                author_sort_keys.append(asort)

            # Re-link book → authors (replace all existing links)
            conn.execute("DELETE FROM books_authors_link WHERE book=?", (book_id,))
            for aid in author_ids:
                conn.execute(
                    "INSERT INTO books_authors_link(book, author) VALUES(?,?)",
                    (book_id, aid),
                )

            # Update denormalized author_sort: Calibre joins multiple with " & "
            combined_sort = " & ".join(author_sort_keys)
            conn.execute(
                "UPDATE books SET author_sort=? WHERE id=?",
                (combined_sort, book_id),
            )
            changed.append(f"authors → {author_names} (sort: {combined_sort})")

        if "series" in fields:
            series_name = fields["series"]
            if series_name:
                # Find or create series
                row = conn.execute(
                    "SELECT id FROM series WHERE name=? LIMIT 1",
                    (series_name,),
                ).fetchone()
                if row:
                    series_id = row[0]
                else:
                    conn.execute(
                        "INSERT INTO series(name, sort) VALUES(?,?)",
                        (series_name, series_name),
                    )
                    series_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                # Re-link book → series
                conn.execute("DELETE FROM books_series_link WHERE book=?", (book_id,))
                conn.execute(
                    "INSERT INTO books_series_link(book, series) VALUES(?,?)",
                    (book_id, series_id),
                )
                changed.append(f"series → {series_name}")
            else:
                # Empty string = remove series
                conn.execute("DELETE FROM books_series_link WHERE book=?", (book_id,))
                changed.append("series → (removed)")

        if "series_index" in fields:
            idx = float(fields["series_index"])
            conn.execute(
                "UPDATE books SET series_index=? WHERE id=?",
                (idx, book_id),
            )
            changed.append(f"series_index → {idx}")

        if "publisher" in fields:
            pub_name = fields["publisher"]
            if pub_name:
                row = conn.execute(
                    "SELECT id FROM publishers WHERE name=? LIMIT 1",
                    (pub_name,),
                ).fetchone()
                if row:
                    pub_id = row[0]
                else:
                    conn.execute(
                        "INSERT INTO publishers(name, sort) VALUES(?,?)",
                        (pub_name, pub_name),
                    )
                    pub_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute("DELETE FROM books_publishers_link WHERE book=?", (book_id,))
                conn.execute(
                    "INSERT INTO books_publishers_link(book, publisher) VALUES(?,?)",
                    (book_id, pub_id),
                )
                changed.append(f"publisher → {pub_name}")
            else:
                conn.execute("DELETE FROM books_publishers_link WHERE book=?", (book_id,))
                changed.append("publisher → (removed)")

        if "tags" in fields:
            tag_val = fields["tags"]
            # Remove existing tags
            conn.execute("DELETE FROM books_tags_link WHERE book=?", (book_id,))
            if tag_val:
                tag_names = [t.strip() for t in tag_val.split(",") if t.strip()]
                for tname in tag_names:
                    row = conn.execute(
                        "SELECT id FROM tags WHERE name=? LIMIT 1", (tname,),
                    ).fetchone()
                    if row:
                        tag_id = row[0]
                    else:
                        conn.execute(
                            "INSERT INTO tags(name) VALUES(?)", (tname,),
                        )
                        tag_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        "INSERT OR IGNORE INTO books_tags_link(book, tag) VALUES(?,?)",
                        (book_id, tag_id),
                    )
                changed.append(f"tags → {tag_names}")
            else:
                changed.append("tags → (removed)")

        if "comments" in fields:
            comment_text = fields["comments"]
            conn.execute("DELETE FROM comments WHERE book=?", (book_id,))
            if comment_text:
                conn.execute(
                    "INSERT INTO comments(book, text) VALUES(?,?)",
                    (book_id, comment_text),
                )
                changed.append(f"comments → (set, {len(comment_text)} chars)")
            else:
                changed.append("comments → (removed)")

        if "languages" in fields:
            lang_val = fields["languages"]
            conn.execute("DELETE FROM books_languages_link WHERE book=?", (book_id,))
            if lang_val:
                lang_codes = [l.strip() for l in lang_val.split(",") if l.strip()]
                for lcode in lang_codes:
                    row = conn.execute(
                        "SELECT id FROM languages WHERE lang_code=? LIMIT 1", (lcode,),
                    ).fetchone()
                    if row:
                        lang_id = row[0]
                    else:
                        conn.execute(
                            "INSERT INTO languages(lang_code) VALUES(?)", (lcode,),
                        )
                        lang_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        "INSERT OR IGNORE INTO books_languages_link(book, lang_code) VALUES(?,?)",
                        (book_id, lang_id),
                    )
                changed.append(f"languages → {lang_codes}")
            else:
                changed.append("languages → (removed)")

        if "identifiers" in fields:
            id_val = fields["identifiers"]
            if id_val:
                # Format: "type:value" or "type:value,type2:value2"
                pairs = [p.strip() for p in id_val.split(",") if p.strip()]
                for pair in pairs:
                    if ":" in pair:
                        id_type, id_value = pair.split(":", 1)
                        # Remove existing identifier of this type
                        conn.execute(
                            "DELETE FROM identifiers WHERE book=? AND type=?",
                            (book_id, id_type),
                        )
                        conn.execute(
                            "INSERT INTO identifiers(book, type, val) VALUES(?,?,?)",
                            (book_id, id_type, id_value),
                        )
                changed.append(f"identifiers → {id_val}")

        if "pubdate" in fields:
            pubdate = fields["pubdate"]
            conn.execute(
                "UPDATE books SET pubdate=? WHERE id=?",
                (pubdate, book_id),
            )
            changed.append(f"pubdate → {pubdate}")

        conn.execute("COMMIT")
        logger.info(f"SQLite updated book {book_id}: {changed}")
        return {"status": "success", "changes": changed}
    except Exception as exc:
        conn.execute("ROLLBACK")
        raise exc
    finally:
        conn.close()


def _remove_books_sqlite(backend, books: list[dict]) -> list[dict]:
    """
    Remove books directly via SQLite + filesystem.
    Faster than calibredb remove for large libraries.
    Deletes all DB records and the book directory on disk.
    """
    import shutil

    library_path = Path(backend.config.library_path)
    conn = backend.connect_write(timeout=15)
    removed = []

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")

        for book in books:
            book_id = book["id"]

            # Delete from all link/child tables
            for table in (
                "books_authors_link",
                "books_tags_link",
                "books_series_link",
                "books_publishers_link",
                "books_languages_link",
                "books_ratings_link",
                "identifiers",
                "comments",
                "data",
                "books_pages_link",    # Count Pages plugin — has real FK constraint
                "books_plugin_data",   # Plugin metadata (stores arbitrary plugin state per book)
            ):
                conn.execute(f"DELETE FROM {table} WHERE book=?", (book_id,))

            # Delete from books table
            conn.execute("DELETE FROM books WHERE id=?", (book_id,))

            # Delete book directory from disk
            book_dir = library_path / book["path"]
            if book_dir.exists():
                shutil.rmtree(book_dir)
                logger.info(f"Deleted directory: {book_dir}")

            removed.append({
                "id": book_id,
                "title": book["title"],
                "author_sort": book["author_sort"],
            })

        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        conn.close()
        raise exc

    # Books are committed; orphan cleanup is best-effort and reports its own status.
    try:
        orphan_result = _cleanup_orphans(conn)
    except Exception as exc:
        logger.exception("orphan cleanup failed after successful book removal")
        orphan_result = {"status": "error", "error": str(exc), "cleaned": {}, "total": 0}
    finally:
        conn.close()

    return removed, orphan_result


def _cleanup_orphans(conn: "sqlite3.Connection") -> dict:
    """
    Delete orphaned rows from series/authors/tags/publishers.

    An entity with no linked books is always safe to remove — if new books are
    added later for that author/series, Calibre will recreate the record. No
    safety guards: empty = delete.
    """
    TABLES = [
        ("series",     "series",    "books_series_link"),
        ("authors",    "author",    "books_authors_link"),
        ("tags",       "tag",       "books_tags_link"),
        ("publishers", "publisher", "books_publishers_link"),
    ]

    cleaned: dict[str, int] = {}
    for table, link_col, link_table in TABLES:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE id NOT IN (SELECT {link_col} FROM {link_table})"
        )
        cleaned[table] = cursor.rowcount if cursor.rowcount >= 0 else 0

    conn.commit()
    return {
        "status": "cleaned",
        "cleaned": cleaned,
        "total": sum(cleaned.values()),
    }


def remove_books(
    backend,
    book_ids: list[int],
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Remove books from the library (deletes files from disk too).

    Args:
        book_ids: List of book IDs to remove
        dry_run: If True, only show what would be removed without doing it
    """
    if not book_ids:
        return {"status": "no_action", "message": "No book IDs provided."}

    # First, get details of books to be removed
    placeholders = ",".join("?" * len(book_ids))
    books = backend.query_db(f"""
        SELECT b.id, b.title, b.author_sort, b.path
        FROM books b WHERE b.id IN ({placeholders})
    """, tuple(book_ids))

    found_ids = {b["id"] for b in books}
    missing_ids = [bid for bid in book_ids if bid not in found_ids]

    if dry_run:
        return {
            "status": "dry_run",
            "would_remove": [
                {"id": b["id"], "title": b["title"], "author_sort": b["author_sort"]}
                for b in books
            ],
            "missing_ids": missing_ids,
            "message": f"Would remove {len(books)} books. Set dry_run=false to execute.",
        }

    # Execute removal via direct SQLite + filesystem (avoids calibredb timeout)
    try:
        removed, orphan_result = _remove_books_sqlite(backend, books)
        logger.info(f"Removed books: {[b['id'] for b in books]}")
        return {
            "status": "success",
            "removed": removed,
            "missing_ids": missing_ids,
            "orphans_cleaned": orphan_result,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def set_book_metadata(
    backend,
    book_id: int,
    fields: dict[str, str],
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Set metadata fields on a book using calibredb set_metadata.

    Args:
        book_id: ID of the book to modify
        fields: Dict of field_name -> new_value. Supported fields:
                title, authors, series, series_index, tags, publisher,
                pubdate, comments, languages, identifiers
        dry_run: If True, only show what would change
    """
    if not fields:
        return {"status": "no_action", "message": "No fields provided."}

    # Get current values
    current = backend.query_db("""
        SELECT b.id, b.title, b.author_sort
        FROM books b WHERE b.id = ?
    """, (book_id,))

    if not current:
        return {"status": "error", "message": f"Book ID {book_id} not found."}

    if dry_run:
        return {
            "status": "dry_run",
            "book_id": book_id,
            "current_title": current[0]["title"],
            "changes": fields,
            "message": f"Would update {len(fields)} field(s). Set dry_run=false to execute.",
        }

    # Use direct SQLite writes (fast — avoids calibredb library scan timeout)
    try:
        result = _set_metadata_sqlite(backend, book_id, fields)
        result["book_id"] = book_id
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}


def bulk_set_metadata(
    backend,
    updates: list[dict[str, Any]],
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Apply metadata changes to multiple books in a single operation.

    Instead of calling set_book_metadata N times, this batches all changes
    and applies them in one pass — useful for series disambiguation, bulk
    renames, etc.

    Args:
        updates:  List of {"book_id": int, "fields": dict} objects.
                  Same fields accepted as set_book_metadata.
        dry_run:  If True, only show what would change
    """
    if not updates:
        return {"status": "no_action", "message": "No updates provided."}

    book_ids = [u["book_id"] for u in updates if "book_id" in u]
    if not book_ids:
        return {"status": "error", "message": "Every update must include a 'book_id'."}

    placeholders = ",".join("?" * len(book_ids))
    existing = backend.query_db(
        f"SELECT id, title FROM books WHERE id IN ({placeholders})",
        tuple(book_ids),
    )
    existing_map = {r["id"]: r["title"] for r in existing}
    missing = [bid for bid in book_ids if bid not in existing_map]
    valid_updates = [u for u in updates if u.get("book_id") in existing_map]

    if dry_run:
        return {
            "status": "dry_run",
            "total": len(updates),
            "valid": len(valid_updates),
            "missing_ids": missing,
            "preview": [
                {
                    "book_id": u["book_id"],
                    "title": existing_map.get(u["book_id"], "?"),
                    "fields": u.get("fields", {}),
                }
                for u in valid_updates[:20]
            ],
            "message": (
                f"Would update {len(valid_updates)} book(s)"
                + (f" ({len(missing)} IDs not found)." if missing else ".")
                + " Set dry_run=false to execute."
            ),
        }

    applied, errors = [], []
    for u in valid_updates:
        try:
            result = _set_metadata_sqlite(backend, u["book_id"], u.get("fields", {}))
            applied.append({
                "book_id": u["book_id"],
                "title": existing_map[u["book_id"]],
                "changes": result.get("changes", []),
            })
        except Exception as exc:
            errors.append({"book_id": u["book_id"], "error": str(exc)})

    return {
        "status": "completed",
        "applied": len(applied),
        "errors": len(errors),
        "missing_ids": missing,
        "error_details": errors,
    }


def backup_database(backend, suffix: str | None = None) -> dict[str, Any]:
    """Create a backup copy of metadata.db (full SQLite file)."""
    try:
        backup_path = backend.backup_database(suffix)
        return {
            "status": "success",
            "backup_path": str(backup_path),
            "size_mb": round(backup_path.stat().st_size / 1024 / 1024, 1),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def vacuum_database(backend) -> dict[str, Any]:
    """Compact the database after bulk operations."""
    try:
        msg = backend.vacuum_database()
        return {"status": "success", "message": msg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── New tools using real calibredb commands ─────────────────────────────

def check_library(backend) -> dict[str, Any]:
    """
    Run calibredb check_library to detect integrity issues.
    This is Calibre's native integrity checker — finds:
    - Books without formats on disk
    - Extra files not in the database
    - Missing cover files
    - Invalid book directories
    """
    try:
        output = backend.run_calibredb("check_library", timeout=300)
        # Parse the output into structured categories
        result = {
            "status": "success",
            "raw_output": output.strip(),
            "issues": _parse_check_library_output(output),
        }
        total = sum(len(v) for v in result["issues"].values())
        result["total_issues"] = total
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _parse_check_library_output(output: str) -> dict[str, list[str]]:
    """Parse calibredb check_library output into categories."""
    issues: dict[str, list[str]] = {}
    current_category = None

    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Category headers end with ":"
        if line.endswith(":") and not line.startswith(" "):
            current_category = line.rstrip(":")
            issues[current_category] = []
        elif current_category is not None:
            issues[current_category].append(line)

    # If no categories were parsed, return the raw output as one block
    if not issues and output.strip():
        issues["uncategorized"] = [l.strip() for l in output.strip().splitlines() if l.strip()]

    return issues


def embed_metadata(
    backend,
    book_ids: list[int] | None = None,
    only_formats: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Embed metadata from the Calibre database into the actual book files.
    After changing titles/authors via set_metadata, the .epub/.fb2 files
    still contain old metadata internally. This syncs them.

    Uses: calibredb embed_metadata

    Args:
        book_ids: Specific book IDs to process. If None, processes ALL books.
        only_formats: Comma-separated format list (e.g., "epub,fb2"). If None, all formats.
        dry_run: If True, only show what would be processed.
    """
    if dry_run:
        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            books = backend.query_db(f"""
                SELECT b.id, b.title, b.author_sort,
                       (SELECT GROUP_CONCAT(d.format, ', ') FROM data d WHERE d.book = b.id) as formats
                FROM books b WHERE b.id IN ({placeholders})
            """, tuple(book_ids))
        else:
            total = backend.query_db_scalar("SELECT COUNT(*) FROM books")
            return {
                "status": "dry_run",
                "scope": "all_books",
                "total_books": total,
                "only_formats": only_formats,
                "message": (
                    f"Would embed metadata into files for ALL {total:,} books. "
                    f"This is a long operation. Set dry_run=false to execute."
                ),
            }
        return {
            "status": "dry_run",
            "books": [{"id": b["id"], "title": b["title"], "formats": b["formats"]} for b in books],
            "only_formats": only_formats,
            "message": f"Would embed metadata into {len(books)} book(s). Set dry_run=false to execute.",
        }

    # Build args
    args = []
    if only_formats:
        args.extend(["--only-formats", only_formats])
    if book_ids:
        args.append(",".join(str(bid) for bid in book_ids))

    try:
        output = backend.run_calibredb("embed_metadata", args, timeout=600)
        return {
            "status": "success",
            "scope": f"{len(book_ids)} books" if book_ids else "all books",
            "only_formats": only_formats,
            "calibredb_output": output.strip(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def backup_metadata_opf(backend) -> dict[str, Any]:
    """
    Export per-book metadata to OPF files using calibredb backup_metadata.
    Creates/updates .opf files inside each book's directory.
    This is different from backup_database — it's per-book OPF, useful as
    a safety net against database corruption.
    """
    try:
        output = backend.run_calibredb("backup_metadata", timeout=600)
        return {
            "status": "success",
            "calibredb_output": output.strip(),
            "message": "Metadata backed up to OPF files in each book's directory.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def export_catalog(
    backend,
    output_file: str = "catalog.csv",
    fields: str | None = None,
) -> dict[str, Any]:
    """
    Export the library catalog using calibredb catalog.
    Produces a CSV file with the specified fields.

    Args:
        output_file: Output filename (default: catalog.csv).
                     Supported extensions: .csv, .xml, .epub, .mobi
        fields: Comma-separated field list (default: all fields).
                Example: "title,authors,series,tags,formats"
    """
    # Output path next to the library
    out_path = Path(backend.config.library_path) / output_file

    args = [str(out_path)]
    if fields:
        args.extend(["--fields", fields])

    try:
        output = backend.run_calibredb("catalog", args, timeout=300)
        size = out_path.stat().st_size if out_path.exists() else 0
        return {
            "status": "success",
            "output_file": str(out_path),
            "size_kb": round(size / 1024, 1),
            "calibredb_output": output.strip(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def fts_index(
    backend,
    action: str = "status",
) -> dict[str, Any]:
    """
    Manage the full-text search index.

    Args:
        action: 'enable' to start indexing, 'disable' to remove index,
                'reindex' to rebuild, 'status' to check state.
    """
    action_map = {
        "status":  (["indexing", "status"], 120),
        "enable":  (["enable"], 120),
        "disable": (["disable"], 120),
        "reindex": (["reindex"], 600),
    }

    if action not in action_map:
        return {"status": "error", "message": f"Unknown action: {action}. Use 'status', 'enable', 'disable', or 'reindex'."}

    args, timeout = action_map[action]
    try:
        output = backend.run_calibredb("fts_index", args, timeout=timeout)
        return {"status": "success", "action": action, "output": output.strip()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def fts_search(
    backend,
    query: str,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Full-text search inside book contents.
    Requires FTS index to be enabled (use fts_index action=enable first).

    Args:
        query: Search query string
        limit: Maximum results to return
    """
    try:
        output = backend.run_calibredb("fts_search", [query], timeout=120)
        # Parse output — calibredb fts_search returns matched book IDs and snippets
        lines = [l.strip() for l in output.strip().splitlines() if l.strip()]

        results = []
        for line in lines[:limit]:
            results.append(line)

        return {
            "status": "success",
            "query": query,
            "raw_results": results,
            "count": len(results),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
