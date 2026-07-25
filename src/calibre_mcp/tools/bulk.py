"""Bulk cleanup operations for the Calibre library (Hito 4)."""

import logging
import os
import shutil
import sqlite3
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..backend import (
    BOOK_CHILD_TABLES,
    METADATA_TABLES,
    UnsafeLibraryPath,
    calibre_sanitize as _calibre_sanitize,
    resolve_book_dir,
)
from ..utils.titlecase import smart_title_case, is_all_uppercase

logger = logging.getLogger("calibre_mcp")


def _case_only_rename(parent: Path, current_name: str, target_name: str) -> dict[str, Any]:
    """
    Two-step rename for case-only directory changes on Windows NTFS.

    A direct os.rename() to a path that differs only in case is a no-op on
    NTFS. To force the casing to update on disk, rename to a temporary name
    first, then to the target. Discovers the actual on-disk name (which may
    differ in case from `current_name`) before renaming.

    Returns one of:
      {"status": "noop"}                  — disk name already matches target,
                                            or no matching directory exists.
      {"status": "renamed", "from": str}  — rename succeeded; "from" is the
                                            previous on-disk name.
      {"status": "error", "error": str,
       "residual_dir": str | None}        — both rename attempts failed.
                                            residual_dir is set only when the
                                            tmp dir was created but recovery
                                            failed (orphan left on disk).
    """
    actual_name = next(
        (n for n in os.listdir(str(parent)) if n.lower() == current_name.lower()),
        None,
    )
    if not actual_name or actual_name == target_name:
        return {"status": "noop"}

    tmp_dir = parent / f"_casefix_{uuid.uuid4().hex}"
    try:
        os.rename(str(parent / actual_name), str(tmp_dir))
        os.rename(str(tmp_dir), str(parent / target_name))
        logger.info(f"Case-fixed dir: {actual_name!r} → {target_name!r}")
        return {"status": "renamed", "from": actual_name}
    except Exception as e:
        if tmp_dir.exists():
            try:
                os.rename(str(tmp_dir), str(parent / actual_name))
            except Exception as recovery_exc:
                logger.error(
                    f"Recovery rename failed for {tmp_dir}: {recovery_exc}. "
                    f"Residual _casefix_ directory left on disk."
                )
                return {
                    "status": "error",
                    "error": f"Case rename failed and recovery failed: {e}; {recovery_exc}",
                    "residual_dir": str(tmp_dir),
                }
        return {"status": "error", "error": f"Case rename failed: {e}"}


def _missing_format_files(backend, book_id: int, directory: Path) -> list[str]:
    """
    Return the book's format filenames that are NOT present in `directory`.

    Used before repointing books.path at a directory the mover did not fill
    itself: an existing directory is not evidence that the book is in it.
    """
    rows = backend.query_db(
        "SELECT format, name FROM data WHERE book = ?", (book_id,)
    )
    missing = []
    for r in rows:
        filename = f"{r['name']}.{r['format'].lower()}"
        if not (directory / filename).exists():
            missing.append(filename)
    return missing


def fix_book_paths(
    backend,
    book_ids: list[int] | None = None,
    dry_run: bool = True,
    batch_size: int = 0,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Rename book directories on disk to match current author/title in DB,
    and update books.path accordingly.

    Use this after bulk metadata edits that bypassed calibredb (direct SQLite),
    which leaves directory names stale.

    For large libraries (>10k fixes), use batch_size to avoid MCP timeouts:
      fix_book_paths(dry_run=false, batch_size=3000, offset=0)   # first batch
      fix_book_paths(dry_run=false, batch_size=3000, offset=3000) # second batch
      ...repeat until more_remaining=false...

    Args:
        book_ids: Specific book IDs to check. If None, scans all books.
        dry_run: If True, only report what would change.
        batch_size: Max fixes to apply per call (0 = unlimited). Use 3000 for large libs.
        offset: Skip the first N fixes (for pagination). Ignored when dry_run=True.
    """
    # abspath (not resolve) so this matches the root form resolve_book_dir
    # returns — the `old_parent != library_path` guards below compare against it.
    library_path = Path(os.path.abspath(backend.config.library_path))

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
            ORDER BY b.id
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
            ORDER BY b.id
        """)

    fixes = []
    unsafe_paths = []
    _listdir_cache: dict[str, list[str]] = {}  # cache os.listdir per parent dir

    for book in books:
        if not book["author_name"]:
            continue

        current_path = book["path"]
        author_dir  = _calibre_sanitize(book["author_name"])
        book_dir    = _calibre_sanitize(f"{book['title']} ({book['id']})")
        expected_path = f"{author_dir}/{book_dir}"

        # Both sides get validated: an empty books.path would make the move
        # source the library root, and an author/title that sanitizes to an
        # empty component would make the destination collapse the same way.
        try:
            resolve_book_dir(library_path, current_path)
            resolve_book_dir(library_path, expected_path)
        except UnsafeLibraryPath as exc:
            unsafe_paths.append({
                "id": book["id"],
                "title": book["title"],
                "author": book["author_name"],
                "current_path": current_path,
                "expected_path": expected_path,
                "reason": str(exc),
            })
            continue

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
        result = {
            "status":       "dry_run",
            "checked":      len(books),
            "fixes_needed": len(fixes),
            "fixes":        fixes[:100],
            "message":      f"Found {len(fixes)} path(s) to fix. Set dry_run=false to apply.",
        }
        if unsafe_paths:
            result["unsafe_paths"] = unsafe_paths[:100]
            result["unsafe_path_count"] = len(unsafe_paths)
            result["unsafe_path_warning"] = (
                f"{len(unsafe_paths)} book(s) were SKIPPED because their path does not "
                "name a directory inside the library. They are never moved or deleted; "
                "repair them in Calibre."
            )
        return result

    if not fixes:
        return {
            "status": "no_action",
            "message": "All paths are already correct.",
            "unsafe_path_count": len(unsafe_paths),
        }

    # Apply fixes — optionally in a batch window to avoid MCP timeouts on large libs.
    total_fixes = len(fixes)
    if batch_size > 0:
        fixes = fixes[offset: offset + batch_size]

    # Use incremental commits (COMMIT_EVERY books) so that partial progress is
    # preserved if the MCP call is cancelled (e.g. client-side timeout).
    COMMIT_EVERY = 1000
    conn = backend.connect_write(timeout=15)
    applied, errors = [], []

    try:
        conn.execute("BEGIN")
        pending_in_txn = 0

        for fix in fixes:
            try:
                # Re-validated here too: this is the last point before an
                # irreversible shutil.move/rmdir.
                current_full  = resolve_book_dir(library_path, fix["current_path"])
                expected_full = resolve_book_dir(library_path, fix["expected_path"])

                is_case_only = (
                    fix["current_path"].lower() == fix["expected_path"].lower()
                )

                if is_case_only:
                    # On Windows NTFS, source and destination are the same physical
                    # directory when only casing differs. A direct rename() is a no-op.
                    # _case_only_rename does a two-step rename via a temp name so the
                    # directory's casing on disk actually updates. Without this,
                    # find_orphan_files later mistakes the dir for an orphan.

                    # Level 2: book directory casing
                    book_result = _case_only_rename(
                        current_full.parent, current_full.name, expected_full.name
                    )
                    if book_result["status"] == "error":
                        err = {"id": fix["id"], "error": book_result["error"]}
                        if "residual_dir" in book_result:
                            err["residual_dir"] = book_result["residual_dir"]
                        errors.append(err)
                        continue

                    # Level 1: author directory casing (book-level rename above only
                    # fixes the subdir; author dir e.g. "ABAD, SEBASTIAN" may still
                    # have wrong casing). Failure here is non-fatal — log + proceed.
                    expected_author_dir = expected_full.parent
                    if expected_author_dir != library_path:
                        author_result = _case_only_rename(
                            library_path, expected_author_dir.name, expected_author_dir.name
                        )
                        if author_result["status"] == "error":
                            logger.warning(
                                f"Failed to case-fix author dir "
                                f"{expected_author_dir.name!r}: {author_result['error']}"
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
                    else:
                        # Source gone, destination already there. Pointing the DB
                        # at it is only correct if the book's format files are
                        # actually inside — otherwise this blesses a ghost book:
                        # books.path says "here", the files are somewhere else,
                        # and the directory still holding them now looks like a
                        # deletable orphan. That is the shape of book 44419
                        # (Cortázar) found in the 2026-07 audit: DB path pointed
                        # at a directory holding only metadata.opf while the epub
                        # sat in the old author directory.
                        missing = _missing_format_files(backend, fix["id"], expected_full)
                        if missing:
                            errors.append({
                                "id": fix["id"],
                                "error": (
                                    f"Refusing to repoint books.path to "
                                    f"{fix['expected_path']!r}: the directory exists but "
                                    f"does not contain {missing}. The source "
                                    f"{fix['current_path']!r} is gone, so the files are "
                                    "elsewhere — find them (find_orphan_files) and move "
                                    "them in before re-running."
                                ),
                            })
                            continue

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

    result: dict[str, Any] = {
        "status":       "success",
        "applied":      len(applied),
        "errors":       len(errors),
        "details":      applied,
        "error_details": errors,
    }
    if unsafe_paths:
        result["unsafe_paths"] = unsafe_paths[:100]
        result["unsafe_path_count"] = len(unsafe_paths)
    if batch_size > 0:
        # The next scan no longer contains the fixes just applied — only the
        # errored entries persist (at their ORDER BY b.id positions). So the
        # next window must start at offset + len(errors), NOT offset +
        # batch_size: advancing by the full batch size skips still-pending
        # fixes that shifted forward when this batch's applied entries
        # disappeared from the rescan (10k-fix simulation: the old protocol
        # left 4,000 unfixed while reporting more_remaining=false).
        untouched_tail = total_fixes - offset - len(fixes)
        more_remaining = untouched_tail > 0
        result["more_remaining"] = more_remaining
        result["total_fixes_found"] = total_fixes
        if more_remaining:
            next_offset = offset + len(errors)
            result["next_offset"] = next_offset
            result["message"] = (
                f"Batch complete: applied {len(applied)}, {len(errors)} error(s), "
                f"{untouched_tail} still pending. "
                f"Call again with offset={next_offset} (the returned next_offset)."
            )
    return result


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
    errors = []
    try:
        with backend.write_transaction(timeout=30) as conn:
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
    except Exception as exc:
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

    # The merged author's name (used as a directory) may differ from the variant
    # names — directories on disk stay stale until fix_book_paths is run, and OPF
    # files stay stale until embed_metadata is run. Surface this so the caller
    # doesn't end up with DB ↔ disk ↔ OPF drift.
    result["next_steps"] = [
        "Run fix_book_paths(dry_run=true) to rename book directories on disk",
        "Run verify_file_integrity to confirm files still resolve after the path update",
        "Run embed_metadata on the affected book IDs so OPF files reflect the merge "
        "(otherwise restore_database from OPFs will recreate the variant authors)",
    ]
    result["affected_book_ids"] = [b["book_id"] for b in affected_books]

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
    try:
        with backend.write_transaction(timeout=30) as conn:
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
                for ch in changes:
                    if ch["conflict_id"]:
                        # Merge: move all books to existing series, delete old
                        conn.execute(
                            "UPDATE books_series_link SET series=? WHERE series=?",
                            (ch["conflict_id"], ch["id"]),
                        )
                        conn.execute("DELETE FROM series WHERE id=?", (ch["id"],))
                    else:
                        conn.execute(
                            "UPDATE series SET name=?, sort=? WHERE id=?",
                            (ch["new"], ch["new"], ch["id"]),
                        )
    except Exception as exc:
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

    # Renaming titles changes the book directory name (Title (id)) on disk, and
    # any of these renames also leaves OPF files stale. Surface that explicitly
    # so the caller knows there's a follow-up step.
    if item_type == "titles":
        affected_book_ids = [ch["id"] for ch in changes]
        result["next_steps"] = [
            "Run fix_book_paths(dry_run=true) to rename book directories on disk",
            "Run verify_file_integrity to confirm files still resolve after the path update",
            "Run embed_metadata on the affected book IDs so OPF files reflect the new titles",
        ]
        result["affected_book_ids"] = affected_book_ids
    elif item_type in ("tags", "publishers", "series"):
        result["next_steps"] = [
            f"Run embed_metadata on books that use the renamed {item_type} so OPF files "
            "stay in sync (otherwise restore_database from OPFs will recreate the old names)",
        ]
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
    try:
        with backend.write_transaction(timeout=30) as conn:
            for ch in changes:
                conn.execute(
                    "UPDATE books SET series_index=? WHERE id=?",
                    (ch["new_index"], ch["book_id"]),
                )
    except Exception as exc:
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
        with backend.write_transaction(timeout=15) as conn:
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
    except Exception as exc:
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

    # Renaming an author changes the directory name (Author/Title (id)) on disk
    # and leaves OPF files stale. Surface the follow-up steps explicitly.
    result["next_steps"] = [
        "Run fix_book_paths(dry_run=true) to rename the author directory on disk",
        "Run verify_file_integrity to confirm files still resolve after the path update",
        "Run embed_metadata on the author's books so OPF files reflect the new name "
        "(otherwise restore_database from OPFs will recreate the old name)",
    ]

    return result


def fix_author_sort(
    backend,
    limit: int = 100,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Synchronize books.author_sort with the actual linked author's sort field.
    Fixes cases where author_sort is stale after author merges.

    Instead of using GROUP_CONCAT (non-deterministic order), the correct sort
    string is built in Python with consistent ordering by the link table's id
    column.

    The whole library is always scanned. The previous version fetched only
    `limit * 10` books ("most won't be mismatches") and issued one query per
    book: with the default limit=100 it examined 1,000 of 68,006 books, so a
    clean result meant nothing, and a full pass cost ~17 minutes of per-book
    round trips. Two set-based queries now cover every book in well under a
    second, and `limit` caps only how many fixes are applied.

    Args:
        limit:   Max books to fix in this call (<= 0 means all of them).
        dry_run: If True, only report.
    """
    links = backend.query_db("""
        SELECT bal.book AS book, a.name AS name, a.sort AS sort
        FROM books_authors_link bal
        JOIN authors a ON a.id = bal.author
        ORDER BY bal.book, bal.id
    """)
    authors_by_book: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in links:
        authors_by_book[r["book"]].append(r)

    all_books = backend.query_db("SELECT id, title, author_sort FROM books")

    mismatches = []
    for book in all_books:
        authors = authors_by_book.get(book["id"])
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

    total_found = len(mismatches)
    if limit > 0:
        mismatches = mismatches[:limit]

    if dry_run:
        return {
            "status": "dry_run",
            "mismatches": [
                {"id": m["id"], "title": m["title"],
                 "current": m["current"], "correct": m["correct"]}
                for m in mismatches[:100]
            ],
            "total": total_found,
            "would_fix": len(mismatches),
            "books_scanned": len(all_books),
            "message": (
                f"Found {total_found} book(s) with stale author_sort across all "
                f"{len(all_books)} books; this call would fix {len(mismatches)}. "
                "Set dry_run=false to apply."
            ),
        }

    # ── Apply via direct SQLite (calibredb times out on large libraries) ────
    try:
        with backend.write_transaction(timeout=30) as conn:
            for m in mismatches:
                conn.execute(
                    "UPDATE books SET author_sort=? WHERE id=?",
                    (m["correct"], m["id"]),
                )
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    result = {
        "status": "completed",
        "fixed": len(mismatches),
        "total_found": total_found,
        "errors": [],
    }
    if total_found > len(mismatches):
        result["remaining"] = total_found - len(mismatches)
        result["message"] = (
            f"Fixed {len(mismatches)} of {total_found} stale author_sort values. "
            f"{result['remaining']} remain — call again (limit=0 fixes all)."
        )
    return result


def cleanup_orphan_links(
    backend,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Remove orphan entries from link tables where the book ID no longer exists.
    This prevents FK constraint errors in Calibre operations.

    WARNING: This writes directly to SQLite. Only use when Calibre is closed.
    """
    link_tables = BOOK_CHILD_TABLES

    # Count orphans and collect orphaned book IDs per table
    counts = {}
    orphan_ids: dict[str, list[int]] = {}
    for table, col in link_tables:
        rows = backend.query_db(f"""
            SELECT DISTINCT t.{col} as book_id FROM {table} t
            WHERE NOT EXISTS (SELECT 1 FROM books b WHERE b.id = t.{col})
        """)
        if rows:
            ids = [r["book_id"] for r in rows]
            counts[table] = len(ids)
            orphan_ids[table] = ids

    if dry_run:
        return {
            "status": "dry_run",
            "orphan_counts": counts,
            "orphan_book_ids": orphan_ids,
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
    found: dict[str, list[str]] = {}
    for table, link_col, link_table, display_col in METADATA_TABLES:
        # display_col varies per table — 'name' for authors/series/tags/publishers,
        # 'lang_code' for languages (the languages table has no name column).
        # NOT EXISTS rather than NOT IN — see _cleanup_orphans: one NULL in the
        # link column makes NOT IN match zero rows and the check silently lies.
        rows = backend.query_db(
            f"SELECT {display_col} AS display FROM {table} "
            f"WHERE NOT EXISTS ("
            f"  SELECT 1 FROM {link_table} l WHERE l.{link_col} = {table}.id) "
            f"ORDER BY {display_col}"
        )
        names = [r["display"] for r in rows]
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
        for table, link_col, link_table, _display in METADATA_TABLES:
            if table in found:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE NOT EXISTS ("
                    f"  SELECT 1 FROM {link_table} l WHERE l.{link_col} = {table}.id)"
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
