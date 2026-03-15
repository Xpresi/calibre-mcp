"""Diagnostic tools for detecting problems in the Calibre library (Hito 2)."""

import os
from collections import defaultdict
from typing import Any

from ..utils.normalize import normalize_for_grouping, initials_compatible, parse_author_tokens
from ..utils.titlecase import is_all_uppercase


def find_duplicate_books(
    backend,
    strategy: str = "exact",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Find duplicate books using different strategies.

    Strategies:
        exact   — Same title + same author(s) (case-insensitive)
        isbn    — Same ISBN
        fuzzy   — Normalized title + same primary author
    """
    if strategy == "exact":
        rows = backend.query_db("""
            SELECT GROUP_CONCAT(b.id) as ids,
                   LOWER(b.title) as ltitle,
                   LOWER(b.author_sort) as lauthor,
                   COUNT(*) as cnt
            FROM books b
            GROUP BY LOWER(b.title), LOWER(b.author_sort)
            HAVING cnt > 1
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,))
        return [
            {"ids": r["ids"].split(","), "title": r["ltitle"],
             "author_sort": r["lauthor"], "count": r["cnt"]}
            for r in rows
        ]

    elif strategy == "isbn":
        rows = backend.query_db("""
            SELECT i.val as isbn, GROUP_CONCAT(i.book) as ids, COUNT(*) as cnt
            FROM identifiers i
            WHERE i.type = 'isbn'
            GROUP BY i.val
            HAVING cnt > 1
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,))
        return [
            {"isbn": r["isbn"], "ids": r["ids"].split(","), "count": r["cnt"]}
            for r in rows
        ]

    elif strategy == "fuzzy":
        all_books = backend.query_db("""
            SELECT b.id, b.title, b.author_sort
            FROM books b ORDER BY b.id
        """)

        groups: dict[str, list] = defaultdict(list)
        for book in all_books:
            key = normalize_for_grouping(
                f"{book['title']}|||{book['author_sort']}"
            )
            groups[key].append(book)

        dupes = []
        for key, books in groups.items():
            if len(books) > 1:
                dupes.append({
                    "ids": [b["id"] for b in books],
                    "books": [{"id": b["id"], "title": b["title"],
                              "author_sort": b["author_sort"]} for b in books],
                    "count": len(books),
                })
        dupes.sort(key=lambda x: -x["count"])
        return dupes[:limit]

    else:
        raise ValueError(f"Unknown strategy: {strategy}. Use 'exact', 'isbn', or 'fuzzy'.")


def find_author_variants(
    backend,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Find groups of authors that appear to be the same person.
    Detects: case variants, initial vs full name, accent differences, pipe format.

    Uses a prefix-based index for initials matching to avoid O(n²) full scan.
    """
    authors = backend.query_db("""
        SELECT a.id, a.name, COUNT(bal.book) as book_count
        FROM authors a
        LEFT JOIN books_authors_link bal ON a.id = bal.author
        GROUP BY a.id
        ORDER BY a.name
    """)

    # ── Phase 1: Group by normalized key (exact match after normalization) ──
    groups: dict[str, list] = defaultdict(list)
    for a in authors:
        key = normalize_for_grouping(a["name"])
        groups[key].append(a)

    variants = []
    for key, members in groups.items():
        if len(members) > 1:
            variants.append({
                "normalized_key": key,
                "authors": members,
                "count": len(members),
                "total_books": sum(m["book_count"] for m in members),
                "match_type": "normalized",
            })

    # ── Phase 2: Initials matching with prefix index ──
    # Instead of O(n²), we build a prefix index: for each key, extract a
    # "surname token" (the longest token, likely the surname) and group by it.
    # Then we only compare initials within the same surname group.
    # This reduces from ~15K² = 225M comparisons to typically ~5-20 per group.

    # Only consider single-author groups (groups of size 1) for cross-matching
    single_keys = {k: members[0] for k, members in groups.items() if len(members) == 1}

    # Build surname index: longest token -> list of (key, tokens)
    surname_index: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    for key in single_keys:
        tokens = key.split()
        if not tokens:
            continue
        # Use the longest token as likely surname
        surname = max(tokens, key=len)
        surname_index[surname].append((key, tokens))

    # Check initials only within same-surname groups
    seen_pairs = set()
    for surname, entries in surname_index.items():
        if len(entries) < 2:
            continue
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                key_i, tokens_i = entries[i]
                key_j, tokens_j = entries[j]
                if tokens_i == tokens_j:
                    continue  # Already caught in Phase 1
                pair = (min(key_i, key_j), max(key_i, key_j))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                if initials_compatible(tokens_i, tokens_j):
                    combined = groups[key_i] + groups[key_j]
                    variants.append({
                        "normalized_key": f"{key_i} \u2248 {key_j}",
                        "authors": combined,
                        "count": len(combined),
                        "total_books": sum(m["book_count"] for m in combined),
                        "match_type": "initials",
                    })

    # ── Assign confidence scores ──────────────────────────────────────────
    for v in variants:
        if v["match_type"] == "normalized":
            # Exact match after accent/case/punctuation normalization → very reliable
            v["confidence"] = 0.95
        else:
            # Initials match: score by proportion of fully-expanded tokens
            keys = v["normalized_key"].split(" \u2248 ")
            if len(keys) == 2:
                toks_a = keys[0].split()
                toks_b = keys[1].split()
                full_matches = sum(
                    1 for a, b in zip(toks_a, toks_b) if a == b
                )
                total_tokens = max(len(toks_a), len(toks_b))
                base = full_matches / total_tokens if total_tokens else 0
                v["confidence"] = round(0.5 + 0.4 * base, 2)
            else:
                v["confidence"] = 0.7

    variants.sort(key=lambda x: (-x["confidence"], -x["total_books"]))
    return variants[:limit]


def find_series_issues(
    backend,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Find series with problems:
    - Duplicate numbers (two books with same series_index)
    - Books without a number (series_index = 0 or NULL)
    """
    dupes = backend.query_db("""
        SELECT s.id as series_id, s.name as series_name,
               b.series_index,
               GROUP_CONCAT(b.id || ':' || b.title, ' ||| ') as books,
               COUNT(*) as cnt
        FROM books_series_link bsl
        JOIN series s ON bsl.series = s.id
        JOIN books b ON bsl.book = b.id
        GROUP BY s.id, b.series_index
        HAVING cnt > 1
        ORDER BY s.name, b.series_index
        LIMIT ?
    """, (limit,))

    issues = []
    for r in dupes:
        book_entries = []
        for entry in r["books"].split(" ||| "):
            bid, title = entry.split(":", 1)
            book_entries.append({"id": int(bid), "title": title})
        issues.append({
            "type": "duplicate_number",
            "series_id": r["series_id"],
            "series_name": r["series_name"],
            "series_index": r["series_index"],
            "books": book_entries,
            "count": r["cnt"],
        })

    unnumbered = backend.query_db("""
        SELECT s.id as series_id, s.name as series_name,
               b.id as book_id, b.title, b.series_index
        FROM books_series_link bsl
        JOIN series s ON bsl.series = s.id
        JOIN books b ON bsl.book = b.id
        WHERE b.series_index = 0 OR b.series_index IS NULL
        ORDER BY s.name
        LIMIT ?
    """, (limit,))

    for r in unnumbered:
        issues.append({
            "type": "unnumbered",
            "series_id": r["series_id"],
            "series_name": r["series_name"],
            "book_id": r["book_id"],
            "title": r["title"],
            "series_index": r["series_index"],
        })

    return issues


def find_uppercase_items(
    backend,
    item_type: str = "all",
    limit: int = 100,
) -> dict[str, list[dict[str, Any]]]:
    """
    Find items that are ALL UPPERCASE.
    item_type: 'titles', 'authors', 'tags', 'publishers', or 'all'
    """
    results = {}

    if item_type in ("all", "titles"):
        rows = backend.query_db("SELECT id, title FROM books")
        results["titles"] = [
            {"id": r["id"], "title": r["title"]}
            for r in rows if is_all_uppercase(r["title"])
        ][:limit]

    if item_type in ("all", "authors"):
        rows = backend.query_db("SELECT id, name FROM authors")
        results["authors"] = [
            {"id": r["id"], "name": r["name"]}
            for r in rows if is_all_uppercase(r["name"])
        ][:limit]

    if item_type in ("all", "tags"):
        rows = backend.query_db("""
            SELECT t.id, t.name, COUNT(btl.book) as book_count
            FROM tags t LEFT JOIN books_tags_link btl ON t.id = btl.tag
            GROUP BY t.id
        """)
        results["tags"] = [
            {"id": r["id"], "name": r["name"], "book_count": r["book_count"]}
            for r in rows if is_all_uppercase(r["name"])
        ][:limit]

    if item_type in ("all", "publishers"):
        rows = backend.query_db("""
            SELECT p.id, p.name, COUNT(bpl.book) as book_count
            FROM publishers p LEFT JOIN books_publishers_link bpl ON p.id = bpl.publisher
            GROUP BY p.id
        """)
        results["publishers"] = [
            {"id": r["id"], "name": r["name"], "book_count": r["book_count"]}
            for r in rows if is_all_uppercase(r["name"])
        ][:limit]

    return results


def find_missing_metadata(
    backend,
    limit: int = 100,
) -> dict[str, Any]:
    """Find books missing important metadata."""
    result = {}

    result["no_cover"] = backend.query_db("""
        SELECT b.id, b.title, b.author_sort
        FROM books b WHERE b.has_cover = 0
        ORDER BY b.title LIMIT ?
    """, (limit,))

    result["no_language"] = backend.query_db("""
        SELECT b.id, b.title, b.author_sort
        FROM books b
        WHERE NOT EXISTS (
            SELECT 1 FROM books_languages_link bl WHERE bl.book = b.id
        )
        ORDER BY b.title LIMIT ?
    """, (limit,))

    result["no_tags"] = backend.query_db("""
        SELECT b.id, b.title, b.author_sort
        FROM books b
        WHERE NOT EXISTS (
            SELECT 1 FROM books_tags_link bt WHERE bt.book = b.id
        )
        ORDER BY b.title LIMIT ?
    """, (limit,))

    result["no_formats"] = backend.query_db("""
        SELECT b.id, b.title, b.author_sort
        FROM books b
        WHERE NOT EXISTS (
            SELECT 1 FROM data d WHERE d.book = b.id
        )
        ORDER BY b.title LIMIT ?
    """, (limit,))

    result["no_publisher"] = backend.query_db("""
        SELECT b.id, b.title, b.author_sort
        FROM books b
        WHERE NOT EXISTS (
            SELECT 1 FROM books_publishers_link bp WHERE bp.book = b.id
        )
        ORDER BY b.title LIMIT ?
    """, (limit,))

    result["counts"] = {
        k: len(v) for k, v in result.items() if k != "counts"
    }

    return result


def find_orphan_links(backend) -> dict[str, int]:
    """
    Find orphan entries in link tables (book ID doesn't exist in books table).
    These cause FK constraint errors when Calibre tries to operate on them.
    """
    link_tables = [
        ("books_authors_link", "book"),
        ("books_series_link", "book"),
        ("books_tags_link", "book"),
        ("books_publishers_link", "book"),
        ("books_languages_link", "book"),
        ("data", "book"),
        ("comments", "book"),
        ("identifiers", "book"),
    ]

    results = {}
    for table, col in link_tables:
        count = backend.query_db_scalar(f"""
            SELECT COUNT(*) FROM {table} t
            WHERE NOT EXISTS (SELECT 1 FROM books b WHERE b.id = t.{col})
        """)
        if count and count > 0:
            results[table] = count

    return results


def find_orphan_files(
    backend,
    use_calibredb: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Find directories/files in the library that don't correspond to any book in the DB.

    Two modes:
    - use_calibredb=True (default): runs 'calibredb check_library' which is more reliable
    - use_calibredb=False: manual filesystem scan (fallback)
    """
    if use_calibredb:
        try:
            output = backend.run_calibredb("check_library", timeout=300)
            return {
                "source": "calibredb_check_library",
                "raw_output": output.strip(),
                "message": "Parsed from calibredb check_library. See raw_output for details.",
            }
        except Exception as e:
            # Fall through to manual scan
            pass

    # Manual filesystem scan fallback
    library_path = backend.config.library_path
    db_paths = set()
    rows = backend.query_db("SELECT path FROM books")
    for r in rows:
        db_paths.add(r["path"].replace("/", os.sep))

    orphans = []
    try:
        for author_dir in os.listdir(library_path):
            author_path = os.path.join(library_path, author_dir)
            if not os.path.isdir(author_path) or author_dir.startswith('.'):
                continue
            for book_dir in os.listdir(author_path):
                book_path = os.path.join(author_dir, book_dir)
                full_path = os.path.join(library_path, book_path)
                if not os.path.isdir(full_path):
                    continue
                if book_path not in db_paths:
                    file_count = sum(
                        1 for f in os.listdir(full_path)
                        if os.path.isfile(os.path.join(full_path, f))
                    )
                    orphans.append({
                        "path": book_path,
                        "full_path": full_path,
                        "file_count": file_count,
                    })
                    if len(orphans) >= limit:
                        return {"source": "filesystem_scan", "orphans": orphans}
    except PermissionError as e:
        return {"source": "filesystem_scan", "error": f"Permission denied: {e}"}

    return {"source": "filesystem_scan", "orphans": orphans, "count": len(orphans)}


def find_ghost_books(
    backend,
    use_calibredb: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Find books in the DB whose directory doesn't exist on disk.

    Two modes:
    - use_calibredb=False (default): manual filesystem check — pageable,
      never times out, returns structured data.
    - use_calibredb=True: runs 'calibredb check_library' (may time out on
      large libraries).

    Args:
        use_calibredb: Use calibredb check_library instead of manual scan.
        limit:  Max books to check per call (for pagination).
        offset: Skip this many books before checking (for pagination).
    """
    if use_calibredb:
        try:
            output = backend.run_calibredb("check_library", timeout=300)
            return {
                "source": "calibredb_check_library",
                "raw_output": output.strip(),
                "message": "Parsed from calibredb check_library. See raw_output for details.",
            }
        except Exception:
            pass  # Fall through to manual

    # Manual check with pagination
    library_path = backend.config.library_path

    total_books = backend.query_db_scalar("SELECT COUNT(*) FROM books") or 0

    rows = backend.query_db("""
        SELECT b.id, b.title, b.author_sort, b.path
        FROM books b ORDER BY b.id
        LIMIT ? OFFSET ?
    """, (limit, offset))

    ghosts = []
    for r in rows:
        book_dir = os.path.join(library_path, r["path"])
        if not os.path.exists(book_dir):
            ghosts.append({
                "id": r["id"],
                "title": r["title"],
                "author_sort": r["author_sort"],
                "expected_path": r["path"],
            })

    return {
        "source": "filesystem_check",
        "ghosts": ghosts,
        "count": len(ghosts),
        "checked": len(rows),
        "offset": offset,
        "total_books": total_books,
        "has_more": (offset + len(rows)) < total_books,
        "next_offset": offset + len(rows) if (offset + len(rows)) < total_books else None,
    }
