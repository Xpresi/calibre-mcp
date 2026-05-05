"""Bulk cleanup operations for the Calibre library (Hito 4)."""

import logging
import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from ..utils.titlecase import smart_title_case, is_all_uppercase

logger = logging.getLogger("calibre_mcp")


# ── Path-fixing helpers ────────────────────────────────────────────────────

def _calibre_sanitize(s: str) -> str:
    """
    Sanitize a string to a Calibre-compatible filesystem path component.
    Calibre replaces backslash, forward slash, colon, asterisk, question mark,
    double quote, angle brackets, pipe, AND periods with underscores on Windows.
    """
    s = re.sub(r'[\\/:*?"<>|.]', '_', s)
    s = s.strip()
    return s[:200]


def fix_book_paths(
    backend,
    book_ids: list[int] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Rename book directories on disk to match current author/title in DB,
    and update books.path accordingly.

    Use this after bulk metadata edits that bypassed calibredb (direct SQLite),
    which leaves directory names stale.

    Args:
        book_ids: Specific book IDs to check. If None, scans all books.
        dry_run: If True, only report what would change.
    """
    library_path = Path(backend.config.library_path)

    # Use a CTE to get each book's first author efficiently (avoids N correlated subqueries)
    if book_ids:
        placeholders = ",".join("?" * len(book_ids))
        books = backend.query_db(f"""
            WITH first_authors AS (
                SELECT book, MIN(id) AS min_id FROM books_authors_link GROUP BY book
            )
            SELECT b.id, b.title, b.path, a.name AS author_name
            FROM books b
            LEFT JOIN first_authors fa ON fa.book = b.id
            LEFT JOIN books_authors_link bal ON bal.id = fa.min_id
            LEFT JOIN authors a ON a.id = bal.author
            WHERE b.id IN ({placeholders})
        """, tuple(book_ids))
    else:
        books = backend.query_db("""
            WITH first_authors AS (
                SELECT book, MIN(id) AS min_id FROM books_authors_link GROUP BY book
            )
            SELECT b.id, b.title, b.path, a.name AS author_name
            FROM books b
            LEFT JOIN first_authors fa ON fa.book = b.id
            LEFT JOIN books_authors_link bal ON bal.id = fa.min_id
            LEFT JOIN authors a ON a.id = bal.author
        """)

    fixes = []
    _listdir_cache: dict[str, list[str]] = {}  # cache os.listdir per parent dir

    for book in books:
        if not book["author_name"]:
            continue

        current_path = book["path"]
        author_dir  = _calibre_sanitize(book["author_name"])
        book_dir    = _calibre_sanitize(f"{book['title']} ({book['id']})")
        expected_path = f"{author_dir}/{book_dir}"

        if current_path == expected_path:
            # DB path matches expected — but the disk directory might have wrong
            # casing (e.g. ALL-CAPS from a previous normalize_uppercase that only
            # updated the DB). Check BOTH levels:
            #   1. Author directory (library_root/AUTHOR_DIR) — most common mismatch
            #   2. Book directory within author dir (AUTHOR_DIR/BOOK_DIR)
            disk_dir = library_path / expected_path.replace("/", os.sep)
            author_disk = disk_dir.parent   # library_root / author_dir
            library_root = str(library_path)

            case_mismatch = False
            try:
                # Level 1: check author directory casing in library root
                if library_root not in _listdir_cache:
                    _listdir_cache[library_root] = os.listdir(library_root)
                actual_author = next(
                    (n for n in _listdir_cache[library_root]
                     if n.lower() == author_disk.name.lower()),
                    None,
                )
                if actual_author and actual_author != author_disk.name:
                    case_mismatch = True

                # Level 2: check book directory casing within author dir (if author ok)
                if not case_mismatch and author_disk.exists():
                    author_key = str(author_disk)
                    if author_key not in _listdir_cache:
                        _listdir_cache[author_key] = os.listdir(author_key)
                    actual_book = next(
                        (n for n in _listdir_cache[author_key]
                         if n.lower() == disk_dir.name.lower()),
                        None,
                    )
                    if actual_book and actual_book != disk_dir.name:
                        case_mismatch = True

            except (PermissionError, OSError):
                pass

            if case_mismatch:
                fixes.append({
                    "id":           book["id"],
                    "title":        book["title"],
                    "author":       book["author_name"],
                    "current_path": current_path,
                    "expected_path": expected_path,
                    "_case_fix":    True,
                })
            continue

        fixes.append({
            "id":           book["id"],
            "title":        book["title"],
            "author":       book["author_name"],
            "current_path": current_path,
            "expected_path": expected_path,
        })

    if dry_run:
        return {
            "status":       "dry_run",
            "checked":      len(books),
            "fixes_needed": len(fixes),
            "fixes":        fixes[:100],
            "message":      f"Found {len(fixes)} path(s) to fix. Set dry_run=false to apply.",
        }

    if not fixes:
        return {"status": "no_action", "message": "All paths are already correct."}

    # Apply fixes — use incremental commits (COMMIT_EVERY books) so that partial
    # progress is preserved if the MCP call is cancelled (e.g. client-side timeout).
    COMMIT_EVERY = 1000
    conn = backend.connect_write(timeout=15)
    applied, errors = [], []

    try:
        conn.execute("BEGIN")
        pending_in_txn = 0

        for fix in fixes:
            try:
                current_full  = library_path / fix["current_path"]
                expected_full = library_path / fix["expected_path"]

                is_case_only = (
                    fix["current_path"].lower() == fix["expected_path"].lower()
                )

                if is_case_only:
                    # On Windows NTFS, source and destination are the same physical
                    # directory when only casing differs. A direct rename() call would
                    # be a no-op. Use a two-step rename via a temporary name so the
                    # directory's actual casing on disk is updated.
                    # Without this, find_orphan_files would later mistake the directory
                    # for an orphan (case-sensitive mismatch) and could delete it.
                    actual_dir = current_full
                    # Find actual disk name (may differ in case from DB path)
                    parent = actual_dir.parent
                    actual_name = next(
                        (n for n in os.listdir(str(parent))
                         if n.lower() == actual_dir.name.lower()),
                        None,
                    )
                    if actual_name and actual_name != expected_full.name:
                        tmp_dir = parent / f"_casefix_{uuid.uuid4().hex}"
                        try:
                            os.rename(str(parent / actual_name), str(tmp_dir))
                            os.rename(str(tmp_dir), str(expected_full))
                            logger.info(
                                f"Case-fixed dir: {actual_name!r} → {expected_full.name!r}"
                            )
                        except Exception as e:
                            if tmp_dir.exists():
                                try:
                                    os.rename(str(tmp_dir), str(parent / actual_name))
                                except Exception as recovery_exc:
                                    logger.error(
                                        f"Recovery rename failed for {tmp_dir}: {recovery_exc}. "
                                        f"Residual _casefix_ directory left on disk."
                                    )
                                    errors.append({
                                        "id": fix["id"],
                                        "error": f"Case rename failed and recovery failed: {e}; {recovery_exc}",
                                        "residual_dir": str(tmp_dir),
                                    })
                                    continue
                            errors.append({"id": fix["id"], "error": f"Case rename failed: {e}"})
                            continue

                    # Level 1: also fix author-directory casing if needed.
                    # The book-level rename above only fixes the subdirectory; the
                    # author directory (e.g. "ABAD, SEBASTIAN") may still have wrong
                    # casing on disk even after the book dir is correct.
                    expected_author_dir = expected_full.parent
                    if expected_author_dir != library_path:
                        actual_author_name = next(
                            (n for n in os.listdir(str(library_path))
                             if n.lower() == expected_author_dir.name.lower()),
                            None,
                        )
                        if actual_author_name and actual_author_name != expected_author_dir.name:
                            tmp_author = library_path / f"_casefix_{uuid.uuid4().hex}"
                            try:
                                os.rename(str(library_path / actual_author_name), str(tmp_author))
                                os.rename(str(tmp_author), str(expected_author_dir))
                                logger.info(
                                    f"Case-fixed author dir: {actual_author_name!r}"
                                    f" → {expected_author_dir.name!r}"
                                )
                            except Exception as e:
                                if tmp_author.exists():
                                    try:
                                        os.rename(str(tmp_author),
                                                  str(library_path / actual_author_name))
                                    except Exception:
                                        pass
                                logger.warning(
                                    f"Failed to case-fix author dir"
                                    f" {actual_author_name!r}: {e}"
                                )

                    # Regardless of whether filesystem rename was needed, update DB path
                else:
                    if current_full.exists():
                        # Ensure target parent (author dir) exists
                        expected_full.parent.mkdir(parents=True, exist_ok=True)

                        if expected_full.exists():
                            # Target already exists: merge files (don't overwrite)
                            for child in current_full.iterdir():
                                dest = expected_full / child.name
                                if not dest.exists():
                                    shutil.move(str(child), str(dest))
                            current_full.rmdir()
                        else:
                            shutil.move(str(current_full), str(expected_full))

                        # Remove old author dir if now empty
                        old_parent = current_full.parent
                        if old_parent != library_path and old_parent.exists():
                            if not any(old_parent.iterdir()):
                                old_parent.rmdir()
                                logger.info(f"Removed empty author dir: {old_parent.name}")

                    elif not expected_full.exists():
                        errors.append({"id": fix["id"],
                                       "error": f"Directory not found: {fix['current_path']}"})
                        continue
                    # else: expected already exists → just update DB path

                # Update books.path in DB
                conn.execute("UPDATE books SET path=? WHERE id=?",
                             (fix["expected_path"], fix["id"]))
                applied.append(fix["id"])
                pending_in_txn += 1

                # Incremental commit to preserve progress on timeout/cancel
                if pending_in_txn >= COMMIT_EVERY:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN")
                    pending_in_txn = 0

            except Exception as exc:
                errors.append({"id": fix["id"], "error": str(exc)})

        conn.execute("COMMIT")  # final commit for remaining batch

    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        # Prior incremental COMMITs are preserved — report partial progress.
        return {
            "status": "partial_error",
            "message": str(exc),
            "applied": len(applied),
            "errors": len(errors),
            "details": applied,
            "error_details": errors,
        }
    finally:
        conn.close()

    return {
        "status":       "success",
        "applied":      len(applied),
        "errors":       len(errors),
        "details":      applied,
        "error_details": errors,
    }


def merge_authors(
    backend,
    canonical_id: int,
    variant_ids: list[int],
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Merge author variants into a canonical author.

    All books linked to variant authors get relinked to the canonical author.
    Uses direct SQLite writes (calibredb times out on large libraries).

    Args:
        canonical_id: The author ID to keep
        variant_ids: Author IDs to merge into canonical
        dry_run: If True, only show what would change
    """
    if not variant_ids:
        return {"status": "no_action", "message": "No variant IDs provided."}

    # Get canonical author info
    canonical = backend.query_db(
        "SELECT id, name, sort FROM authors WHERE id = ?", (canonical_id,)
    )
    if not canonical:
        return {"status": "error", "message": f"Canonical author ID {canonical_id} not found."}
    canonical = canonical[0]

    # Get variant authors and their books
    all_ids = ",".join("?" * len(variant_ids))
    variants = backend.query_db(f"""
        SELECT a.id, a.name, COUNT(bal.book) as book_count
        FROM authors a
        LEFT JOIN books_authors_link bal ON a.id = bal.author
        WHERE a.id IN ({all_ids})
        GROUP BY a.id
    """, tuple(variant_ids))

    # Get books for each variant
    affected_books = []
    for v in variants:
        books = backend.query_db("""
            SELECT b.id, b.title
            FROM books b
            JOIN books_authors_link bal ON b.id = bal.book
            WHERE bal.author = ?
        """, (v["id"],))
        for book in books:
            affected_books.append({
                "book_id": book["id"],
                "title": book["title"],
                "old_author_id": v["id"],
                "old_author": v["name"],
            })

    if dry_run:
        return {
            "status": "dry_run",
            "canonical": canonical,
            "variants": variants,
            "affected_books": affected_books,
            "message": (
                f"Would merge {len(variants)} variant(s) into '{canonical['name']}', "
                f"affecting {len(affected_books)} book(s). Set dry_run=false to execute."
            ),
        }

    if not affected_books and not variants:
        return {"status": "no_action", "message": "No books to reroute."}

    # ── Apply via direct SQLite (calibredb times out on large libraries) ────
    db_path = str(backend.db_path)
    errors = []
    try:
        conn = backend.connect_write(timeout=30)
        conn.execute("BEGIN")

        for book in affected_books:
            try:
                # Check if the book already has canonical author linked
                already = conn.execute(
                    "SELECT id FROM books_authors_link WHERE book=? AND author=?",
                    (book["book_id"], canonical_id),
                ).fetchone()

                if already:
                    # Just remove the variant link
                    conn.execute(
                        "DELETE FROM books_authors_link WHERE book=? AND author=?",
                        (book["book_id"], book["old_author_id"]),
                    )
                else:
                    # Reroute: point existing link to canonical
                    conn.execute(
                        "UPDATE books_authors_link SET author=? WHERE book=? AND author=?",
                        (canonical_id, book["book_id"], book["old_author_id"]),
                    )

                # Update denormalized author_sort on books table
                conn.execute(
                    "UPDATE books SET author_sort=? WHERE id=?",
                    (canonical["sort"], book["book_id"]),
                )
            except Exception as exc:
                errors.append({"book_id": book["book_id"], "error": str(exc)})

        # Delete variant author records (only if they have no remaining links)
        for v in variants:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM books_authors_link WHERE author=?", (v["id"],)
            ).fetchone()[0]
            if remaining == 0:
                conn.execute("DELETE FROM authors WHERE id=?", (v["id"],))

        conn.execute("COMMIT")
        conn.close()
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
            conn.close()
        except Exception:
            pass
        return {"status": "error", "message": str(exc)}

    # Check if any affected books have multiple authors (co-author situation)
    affected_ids = [b["book_id"] for b in affected_books]
    multi_author_warning = None
    if affected_ids:
        ph = ",".join("?" * len(affected_ids))
        multi = backend.query_db(f"""
            SELECT book, COUNT(*) as cnt
            FROM books_authors_link
            WHERE book IN ({ph})
            GROUP BY book
            HAVING cnt > 1
        """, tuple(affected_ids))
        if multi:
            multi_author_warning = (
                f"{len(multi)} book(s) have multiple authors. "
                f"Run fix_author_sort to correct their author_sort field."
            )

    result = {
        "status": "completed",
        "canonical": canonical,
        "merged_variants": len(variants),
        "books_updated": len(affected_books) - len(errors),
        "errors": errors,
    }
    if multi_author_warning:
        result["warning"] = multi_author_warning

    return result


def normalize_uppercase(
    backend,
    item_type: str = "titles",
    limit: int = 100,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Convert ALL UPPERCASE titles/tags/publishers to Title Case.

    Apply uses direct SQLite (fast for large libraries; calibredb times out).

    Args:
        item_type: 'titles', 'tags', or 'publishers'
        limit: Maximum items to process
        dry_run: If True, only show what would change
    """
    from .write import _compute_title_sort

    changes = []

    if item_type == "titles":
        rows = backend.query_db("SELECT id, title FROM books")
        for r in rows:
            if is_all_uppercase(r["title"]):
                new_title = smart_title_case(r["title"])
                if new_title != r["title"]:
                    changes.append({
                        "id": r["id"],
                        "old": r["title"],
                        "new": new_title,
                    })
                    if len(changes) >= limit:
                        break

    elif item_type == "tags":
        rows = backend.query_db("""
            SELECT t.id, t.name, COUNT(btl.book) as book_count
            FROM tags t LEFT JOIN books_tags_link btl ON t.id = btl.tag
            GROUP BY t.id
        """)
        for r in rows:
            if is_all_uppercase(r["name"]):
                new_name = smart_title_case(r["name"])
                if new_name != r["name"]:
                    changes.append({
                        "id": r["id"],
                        "old": r["name"],
                        "new": new_name,
                        "book_count": r["book_count"],
                    })
                    if len(changes) >= limit:
                        break

    elif item_type == "publishers":
        rows = backend.query_db("""
            SELECT p.id, p.name, COUNT(bpl.book) as book_count
            FROM publishers p LEFT JOIN books_publishers_link bpl ON p.id = bpl.publisher
            GROUP BY p.id
        """)
        for r in rows:
            if is_all_uppercase(r["name"]):
                new_name = smart_title_case(r["name"])
                if new_name != r["name"]:
                    changes.append({
                        "id": r["id"],
                        "old": r["name"],
                        "new": new_name,
                        "book_count": r["book_count"],
                    })
                    if len(changes) >= limit:
                        break

    elif item_type == "series":
        rows = backend.query_db("""
            SELECT s.id, s.name, COUNT(bsl.book) as book_count
            FROM series s LEFT JOIN books_series_link bsl ON s.id = bsl.series
            GROUP BY s.id
        """)
        # Build a name->id map to detect conflicts (new name matches existing series)
        all_series = backend.query_db("SELECT id, name FROM series")
        name_to_id = {r["name"].lower(): r["id"] for r in all_series}
        for r in rows:
            if is_all_uppercase(r["name"]):
                new_name = smart_title_case(r["name"])
                if new_name != r["name"]:
                    existing_id = name_to_id.get(new_name.lower())
                    conflict_id = existing_id if existing_id and existing_id != r["id"] else None
                    changes.append({
                        "id": r["id"],
                        "old": r["name"],
                        "new": new_name,
                        "book_count": r["book_count"],
                        "conflict_id": conflict_id,
                    })
                    if len(changes) >= limit:
                        break

    else:
        return {"status": "error", "message": f"Unknown item_type: {item_type}. Use 'titles', 'tags', 'publishers', or 'series'."}

    if dry_run:
        return {
            "status": "dry_run",
            "item_type": item_type,
            "changes": changes,
            "total": len(changes),
            "message": f"Would normalize {len(changes)} {item_type}. Set dry_run=false to execute.",
        }

    if not changes:
        return {"status": "no_action", "message": "No uppercase items found."}

    # ── Apply via direct SQLite (calibredb times out on large libraries) ────
    db_path = str(backend.db_path)
    try:
        conn = backend.connect_write(timeout=30)
        conn.execute("BEGIN")

        if item_type == "titles":
            for ch in changes:
                new_sort = _compute_title_sort(ch["new"])
                conn.execute(
                    "UPDATE books SET title=?, sort=? WHERE id=?",
                    (ch["new"], new_sort, ch["id"]),
                )

        elif item_type == "tags":
            # Tags table has a unique constraint on name — rename directly
            for ch in changes:
                conn.execute(
                    "UPDATE tags SET name=? WHERE id=?",
                    (ch["new"], ch["id"]),
                )

        elif item_type == "publishers":
            # Publishers table has a unique constraint on name — rename directly
            for ch in changes:
                conn.execute(
                    "UPDATE publishers SET name=? WHERE id=?",
                    (ch["new"], ch["id"]),
                )

        elif item_type == "series":
            merges = 0
            for ch in changes:
                if ch["conflict_id"]:
                    # Merge: move all books to existing series, delete old
                    conn.execute(
                        "UPDATE books_series_link SET series=? WHERE series=?",
                        (ch["conflict_id"], ch["id"]),
                    )
                    conn.execute("DELETE FROM series WHERE id=?", (ch["id"],))
                    merges += 1
                else:
                    conn.execute(
                        "UPDATE series SET name=?, sort=? WHERE id=?",
                        (ch["new"], ch["new"], ch["id"]),
                    )

        conn.execute("COMMIT")
        conn.close()
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
            conn.close()
        except Exception:
            pass
        return {"status": "error", "message": str(exc)}

    result = {
        "status": "completed",
        "item_type": item_type,
        "total_changes": len(changes),
        "successful": len(changes),
        "errors": [],
    }
    if item_type == "series":
        conflicts = [ch for ch in changes if ch["conflict_id"]]
        if conflicts:
            result["merges"] = len(conflicts)
            result["merged"] = [{"old": ch["old"], "merged_into_id": ch["conflict_id"]} for ch in conflicts]
    return result


def fix_series_numbers(
    backend,
    series_id: int,
    assignments: list[dict[str, Any]],
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Fix series numbering by assigning specific indices to books.

    Args:
        series_id: The series to fix
        assignments: List of {"book_id": int, "index": float} dicts
        dry_run: If True, only show what would change
    """
    # Verify series exists
    series = backend.query_db("SELECT id, name FROM series WHERE id = ?", (series_id,))
    if not series:
        return {"status": "error", "message": f"Series ID {series_id} not found."}
    series_name = series[0]["name"]

    # Get current state
    current = backend.query_db("""
        SELECT b.id, b.title, b.series_index
        FROM books b
        JOIN books_series_link bsl ON b.id = bsl.book
        WHERE bsl.series = ?
        ORDER BY b.series_index
    """, (series_id,))

    current_map = {b["id"]: b for b in current}

    changes = []
    for a in assignments:
        book_id = a["book_id"]
        new_index = a["index"]
        if book_id in current_map:
            old_index = current_map[book_id]["series_index"]
            if old_index != new_index:
                changes.append({
                    "book_id": book_id,
                    "title": current_map[book_id]["title"],
                    "old_index": old_index,
                    "new_index": new_index,
                })

    if dry_run:
        return {
            "status": "dry_run",
            "series": series_name,
            "current_books": current,
            "changes": changes,
            "message": f"Would update {len(changes)} book(s) in '{series_name}'. Set dry_run=false to execute.",
        }

    # ── Apply via direct SQLite (calibredb times out on large libraries) ────
    db_path = str(backend.db_path)
    try:
        conn = backend.connect_write(timeout=30)
        conn.execute("BEGIN")
        for ch in changes:
            conn.execute(
                "UPDATE books SET series_index=? WHERE id=?",
                (ch["new_index"], ch["book_id"]),
            )
        conn.execute("COMMIT")
        conn.close()
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
            conn.close()
        except Exception:
            pass
        return {"status": "error", "message": str(exc), "series": series_name}

    return {
        "status": "completed",
        "series": series_name,
        "updated": len(changes),
        "errors": [],
    }


def rename_author(
    backend,
    author_id: int,
    new_name: str,
    new_sort: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Rename an author directly in the database, bypassing calibredb's
    case-insensitive author matching (which prevents case-only renames like
    BOECIO → Boecio).

    This does a direct SQL UPDATE on the authors table — no books are
    re-linked, only the author's name and sort fields change.

    Args:
        author_id: ID of the author to rename
        new_name:  New display name (e.g. "Boecio" instead of "BOECIO")
        new_sort:  New sort key. Defaults to new_name if omitted.
        dry_run:   If True, only show what would change
    """
    if new_sort is None:
        new_sort = new_name

    current = backend.query_db(
        "SELECT id, name, sort FROM authors WHERE id = ?", (author_id,)
    )
    if not current:
        return {"status": "error", "message": f"Author ID {author_id} not found."}
    current = current[0]

    if dry_run:
        return {
            "status": "dry_run",
            "author_id": author_id,
            "old_name": current["name"],
            "old_sort": current["sort"],
            "new_name": new_name,
            "new_sort": new_sort,
            "message": (
                f"Would rename '{current['name']}' → '{new_name}' "
                f"(sort: '{new_sort}'). Set dry_run=false to execute."
            ),
        }

    db_path = str(backend.db_path)
    try:
        conn = backend.connect_write(timeout=15)
        conn.execute("BEGIN")

        conn.execute(
            "UPDATE authors SET name=?, sort=? WHERE id=?",
            (new_name, new_sort, author_id),
        )
        # Refresh the denormalised author_sort on all books by this author
        conn.execute(
            "UPDATE books SET author_sort=? "
            "WHERE id IN (SELECT book FROM books_authors_link WHERE author=?)",
            (new_sort, author_id),
        )

        conn.execute("COMMIT")
        conn.close()
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
            conn.close()
        except Exception:
            pass
        return {"status": "error", "message": str(exc)}

    # Check if any affected books have multiple authors
    multi_author_books = backend.query_db("""
        SELECT b.id, b.title
        FROM books_authors_link bal
        JOIN books b ON b.id = bal.book
        WHERE bal.author = ?
          AND (SELECT COUNT(*) FROM books_authors_link bal2 WHERE bal2.book = b.id) > 1
    """, (author_id,))

    result = {
        "status": "completed",
        "author_id": author_id,
        "old_name": current["name"],
        "new_name": new_name,
        "new_sort": new_sort,
    }

    if multi_author_books:
        result["warning"] = (
            f"{len(multi_author_books)} book(s) have multiple authors. "
            f"Run fix_author_sort to correct their author_sort field."
        )
        result["multi_author_book_ids"] = [b["id"] for b in multi_author_books]

    return result


def fix_author_sort(
    backend,
    limit: int = 100,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Synchronize books.author_sort with the actual linked author's sort field.
    Fixes cases where author_sort is stale after author merges.

    Instead of using GROUP_CONCAT (non-deterministic order), we query each book's
    authors individually and build the correct sort string in Python with consistent
    ordering by the link table's id column.
    """
    # Get all books that have authors linked
    all_books = backend.query_db("""
        SELECT b.id, b.title, b.author_sort
        FROM books b
        WHERE EXISTS (SELECT 1 FROM books_authors_link bal WHERE bal.book = b.id)
        LIMIT ?
    """, (limit * 10,))  # Fetch more than limit since most won't be mismatches

    mismatches = []
    for book in all_books:
        # Get authors in consistent order (by link table id = insertion order)
        authors = backend.query_db("""
            SELECT a.name, a.sort FROM books_authors_link bal
            JOIN authors a ON bal.author = a.id
            WHERE bal.book = ?
            ORDER BY bal.id
        """, (book["id"],))

        if not authors:
            continue

        correct_sort = " & ".join(a["sort"] for a in authors)
        if book["author_sort"] != correct_sort:
            mismatches.append({
                "id": book["id"],
                "title": book["title"],
                "current": book["author_sort"],
                "correct": correct_sort,
                "author_names": [a["name"] for a in authors],
            })
            if len(mismatches) >= limit:
                break

    if dry_run:
        return {
            "status": "dry_run",
            "mismatches": [
                {"id": m["id"], "title": m["title"],
                 "current": m["current"], "correct": m["correct"]}
                for m in mismatches
            ],
            "total": len(mismatches),
            "message": f"Found {len(mismatches)} book(s) with stale author_sort. Set dry_run=false to fix.",
        }

    # ── Apply via direct SQLite (calibredb times out on large libraries) ────
    db_path = str(backend.db_path)
    try:
        conn = backend.connect_write(timeout=30)
        conn.execute("BEGIN")
        for m in mismatches:
            conn.execute(
                "UPDATE books SET author_sort=? WHERE id=?",
                (m["correct"], m["id"]),
            )
        conn.execute("COMMIT")
        conn.close()
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
            conn.close()
        except Exception:
            pass
        return {"status": "error", "message": str(exc)}

    return {
        "status": "completed",
        "fixed": len(mismatches),
        "errors": [],
    }


def cleanup_orphan_links(
    backend,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Remove orphan entries from link tables where the book ID no longer exists.
    This prevents FK constraint errors in Calibre operations.

    WARNING: This writes directly to SQLite. Only use when Calibre is closed.
    """
    link_tables = [
        ("books_authors_link", "book"),
        ("books_series_link", "book"),
        ("books_tags_link", "book"),
        ("books_publishers_link", "book"),
        ("books_languages_link", "book"),
        ("books_ratings_link", "book"),
        ("books_pages_link", "book"),      # Count Pages plugin — has real FK constraint
        ("books_plugin_data", "book"),     # Plugin metadata
        ("data", "book"),
        ("comments", "book"),
        ("identifiers", "book"),
        ("last_read_positions", "book"),   # Reading positions (per book/format/user/device)
        ("annotations", "book"),           # Highlights, notes, bookmarks
        ("annotations_dirtied", "book"),   # Calibre dirty-tracking for annotations
        ("metadata_dirtied", "book"),      # Calibre dirty-tracking for metadata
        ("conversion_options", "book"),    # Per-book conversion settings
    ]

    # Count orphans
    counts = {}
    for table, col in link_tables:
        count = backend.query_db_scalar(f"""
            SELECT COUNT(*) FROM {table} t
            WHERE NOT EXISTS (SELECT 1 FROM books b WHERE b.id = t.{col})
        """)
        if count and count > 0:
            counts[table] = count

    if dry_run:
        return {
            "status": "dry_run",
            "orphan_counts": counts,
            "total": sum(counts.values()),
            "message": f"Found {sum(counts.values())} orphan link(s). Set dry_run=false to clean.",
        }

    if not counts:
        return {"status": "no_action", "message": "No orphan links found."}

    # Direct SQLite write (dangerous but necessary — calibredb has no tool for this)
    try:
        conn = backend.connect_write(timeout=10)
        deleted = {}
        for table, col in link_tables:
            if table in counts:
                cursor = conn.execute(f"""
                    DELETE FROM {table}
                    WHERE NOT EXISTS (SELECT 1 FROM books b WHERE b.id = {table}.{col})
                """)
                deleted[table] = cursor.rowcount
        conn.commit()
        conn.close()

        return {
            "status": "completed",
            "deleted": deleted,
            "total": sum(deleted.values()),
        }
    except sqlite3.OperationalError as e:
        return {"status": "error", "message": f"SQLite write failed (is Calibre open?): {e}"}


def find_orphaned_metadata(
    backend,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Find (and optionally delete) orphaned rows in authors, series, tags, and
    publishers — i.e. entries that no longer have any books linked to them.

    These accumulate after remove_books, merge_authors, or bulk deletions.
    Unlike cleanup_orphan_links (which fixes broken link-table references),
    this targets the metadata tables themselves.

    Args:
        dry_run: If True (default), only list orphans. If False, delete them.
    """
    TABLES = [
        ("authors",    "author",    "books_authors_link"),
        ("series",     "series",    "books_series_link"),
        ("tags",       "tag",       "books_tags_link"),
        ("publishers", "publisher", "books_publishers_link"),
    ]

    found: dict[str, list[str]] = {}
    for table, link_col, link_table in TABLES:
        rows = backend.query_db(
            f"SELECT name FROM {table} "
            f"WHERE id NOT IN (SELECT {link_col} FROM {link_table}) "
            f"ORDER BY name"
        )
        names = [r["name"] for r in rows]
        if names:
            found[table] = names

    total = sum(len(v) for v in found.values())

    if dry_run:
        return {
            "status": "dry_run",
            "orphans": found,
            "total": total,
            "message": (
                f"Found {total} orphaned metadata row(s). Set dry_run=false to delete."
                if total else "No orphaned metadata found."
            ),
        }

    if not found:
        return {"status": "no_action", "message": "No orphaned metadata found."}

    try:
        conn = backend.connect_write(timeout=10)
        deleted: dict[str, int] = {}
        for table, link_col, link_table in TABLES:
            if table in found:
                cursor = conn.execute(
                    f"DELETE FROM {table} "
                    f"WHERE id NOT IN (SELECT {link_col} FROM {link_table})"
                )
                deleted[table] = cursor.rowcount
        conn.commit()
        conn.close()
        return {
            "status": "completed",
            "deleted": deleted,
            "total": sum(deleted.values()),
        }
    except sqlite3.OperationalError as e:
        return {"status": "error", "message": f"SQLite write failed (is Calibre open?): {e}"}
