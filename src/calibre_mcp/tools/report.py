"""Reporting and metadata fetch tools (Hito 5)."""

import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Any

from .diagnose import (
    find_duplicate_books,
    find_author_variants,
    find_series_issues,
    find_uppercase_items,
    find_missing_metadata,
    find_orphan_links,
)
from .write import _set_metadata_sqlite

logger = logging.getLogger("calibre_mcp")


def _query_openlibrary(title: str, author: str) -> dict | None:
    """Query Open Library search API. Returns first result or None."""
    q = urllib.parse.urlencode({"title": title, "author": author, "limit": 1})
    url = f"https://openlibrary.org/search.json?{q}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        docs = data.get("docs", [])
        if not docs:
            return None
        doc = docs[0]
        return {
            "source": "openlibrary",
            "title": doc.get("title"),
            "authors": doc.get("author_name", []),
            "publish_year": doc.get("first_publish_year"),
            "isbn": (doc.get("isbn") or [None])[0],
            "publisher": (doc.get("publisher") or [None])[0],
            "language": (doc.get("language") or [None])[0],
            "subjects": (doc.get("subject") or [])[:5],
        }
    except Exception as e:
        return {"error": str(e)}


def _query_googlebooks(title: str, author: str) -> dict | None:
    """Query Google Books API. Returns first result or None. No API key needed for basic search."""
    q = urllib.parse.urlencode({"q": f"intitle:{title}+inauthor:{author}", "maxResults": 1})
    url = f"https://www.googleapis.com/books/v1/volumes?{q}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        items = data.get("items", [])
        if not items:
            return None
        info = items[0].get("volumeInfo", {})
        isbn = None
        for iid in info.get("industryIdentifiers", []):
            if iid.get("type") == "ISBN_13":
                isbn = iid["identifier"]
                break
        return {
            "source": "googlebooks",
            "title": info.get("title"),
            "authors": info.get("authors", []),
            "publish_date": info.get("publishedDate"),
            "isbn": isbn,
            "publisher": info.get("publisher"),
            "language": info.get("language"),
            "categories": info.get("categories", [])[:5],
            "description": (info.get("description") or "")[:300],
        }
    except Exception as e:
        return {"error": str(e)}


def fetch_metadata(
    backend,
    book_ids: list[int],
    source: str = "openlibrary",
    apply: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Fetch metadata from the internet for specified books.

    NOTE: calibredb has no fetch-metadata command. This tool queries
    Open Library and Google Books APIs directly.

    Args:
        book_ids: List of book IDs to look up
        source: 'openlibrary' or 'googlebooks' (default: openlibrary)
        apply: If True and dry_run=False, apply found metadata to the books
               via calibredb set_metadata
        dry_run: If True, only show what would be fetched/changed
    """
    if not book_ids:
        return {"status": "no_action", "message": "No book IDs provided."}

    placeholders = ",".join("?" * len(book_ids))
    books = backend.query_db(f"""
        SELECT b.id, b.title, b.author_sort,
               (SELECT i.val FROM identifiers i WHERE i.book = b.id AND i.type = 'isbn' LIMIT 1) as isbn
        FROM books b WHERE b.id IN ({placeholders})
    """, tuple(book_ids))

    if not books:
        return {"status": "error", "message": "No books found for given IDs."}

    if dry_run:
        return {
            "status": "dry_run",
            "books": [{"id": b["id"], "title": b["title"],
                       "author_sort": b["author_sort"], "isbn": b["isbn"]}
                      for b in books],
            "source": source,
            "message": (
                f"Would query {source} for {len(books)} book(s). "
                f"Set dry_run=false to execute. "
                f"Set apply=true to also write found metadata back to the library."
            ),
        }

    query_fn = _query_openlibrary if source == "openlibrary" else _query_googlebooks
    results = []

    for book in books:
        found = query_fn(book["title"], book["author_sort"])
        entry = {
            "id": book["id"],
            "title": book["title"],
            "found": found,
            "applied": False,
        }

        if apply and found and "error" not in found:
            fields = {}
            if found.get("isbn") and not book["isbn"]:
                fields["identifiers"] = f"isbn:{found['isbn']}"
            if found.get("publisher"):
                fields["publisher"] = found["publisher"]
            if found.get("publish_date") or found.get("publish_year"):
                date = found.get("publish_date") or f"{found['publish_year']}-01-01"
                fields["pubdate"] = date
            if found.get("language"):
                fields["languages"] = found["language"]
            if found.get("subjects") or found.get("categories"):
                tags = found.get("subjects") or found.get("categories", [])
                if tags:
                    fields["tags"] = ",".join(tags[:5])

            if fields:
                try:
                    result = _set_metadata_sqlite(backend, book["id"], fields)
                    entry["applied"] = True
                    entry["applied_fields"] = fields
                    entry["changes"] = result.get("changes", [])
                except Exception as e:
                    entry["apply_error"] = str(e)

        results.append(entry)

    return {
        "status": "completed",
        "source": source,
        "results": results,
        "found": sum(1 for r in results if r["found"] and "error" not in r["found"]),
        "applied": sum(1 for r in results if r["applied"]),
    }


def generate_report(backend) -> str:
    """
    Generate a comprehensive markdown report of the library's current state.
    """
    stats = backend.get_library_stats()

    # Run diagnostics
    exact_dupes = find_duplicate_books(backend, strategy="exact", limit=10)
    isbn_dupes = find_duplicate_books(backend, strategy="isbn", limit=10)
    author_vars = find_author_variants(backend, limit=10)
    series_issues = find_series_issues(backend, limit=10)
    uppercase = find_uppercase_items(backend, limit=5)
    missing = find_missing_metadata(backend, limit=5)
    orphan_links = find_orphan_links(backend)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    report = f"""# Calibre Library Report
Generated: {now}

## Summary

| Metric | Count |
|--------|-------|
| Total books | {stats['total_books']:,} |
| Total authors | {stats['total_authors']:,} |
| Total series | {stats['total_series']:,} |
| Total tags | {stats['total_tags']:,} |
| Total publishers | {stats['total_publishers']:,} |
| Database size | {stats['database_size_mb']} MB |

## Formats

| Format | Count |
|--------|-------|
"""
    for fmt, cnt in stats["formats"].items():
        report += f"| {fmt} | {cnt:,} |\n"

    report += f"""
## Top Languages

| Language | Count |
|----------|-------|
"""
    for lang, cnt in stats["top_languages"].items():
        report += f"| {lang} | {cnt:,} |\n"

    report += f"""
## Issues Detected

### Missing Metadata
- Books without cover: {stats['books_without_cover']:,}
- Books without file records (SQL-only, run find_ghost_books for disk check): {stats['books_without_data_records']:,}
- Books without tags: {stats['books_without_tags']:,}
- Books without language: {stats['books_without_language']:,}
- Books without publisher: {missing['counts'].get('no_publisher', 'N/A')}

### Duplicates
- Exact title+author duplicates: {len(exact_dupes)} groups
- ISBN duplicates: {len(isbn_dupes)} groups

### Author Variants
- Potential variant groups: {len(author_vars)}
"""

    if author_vars:
        report += "\nTop variant groups:\n"
        for v in author_vars[:5]:
            names = ", ".join(a["name"] for a in v["authors"])
            report += f"- {names} ({v['total_books']} books)\n"

    report += f"""
### Series Issues
- Total issues found: {len(series_issues)}
"""

    dup_nums = [i for i in series_issues if i.get("type") == "duplicate_number"]
    unnumbered = [i for i in series_issues if i.get("type") == "unnumbered"]
    if dup_nums:
        report += f"- Series with duplicate numbering: {len(dup_nums)}\n"
    if unnumbered:
        report += f"- Books without series number: {len(unnumbered)}\n"

    report += f"""
### Uppercase Items
- Uppercase titles: {len(uppercase.get('titles', []))}+
- Uppercase authors: {len(uppercase.get('authors', []))}
- Uppercase tags: {len(uppercase.get('tags', []))}
- Uppercase publishers: {len(uppercase.get('publishers', []))}

### Orphan Links
"""
    if orphan_links:
        for table, count in orphan_links.items():
            report += f"- {table}: {count} orphans\n"
    else:
        report += "- No orphan links found\n"

    report += f"""
---
Library path: {stats['library_path']}
"""

    return report


def compare_snapshots(
    backend,
    old_stats: dict[str, Any],
) -> dict[str, Any]:
    """
    Compare current library state with a previous snapshot.

    Args:
        old_stats: A previously saved output from library_stats

    Returns:
        Dict showing differences between old and current state
    """
    current = backend.get_library_stats()

    diff = {}
    compare_keys = [
        "total_books", "total_authors", "total_series", "total_tags",
        "total_publishers", "books_without_cover", "books_without_data_records",
        "books_without_tags", "books_without_language",
    ]

    for key in compare_keys:
        old_val = old_stats.get(key, 0)
        new_val = current.get(key, 0)
        if old_val != new_val:
            diff[key] = {
                "old": old_val,
                "new": new_val,
                "change": new_val - old_val,
            }

    return {
        "snapshot_date": old_stats.get("_snapshot_date", "unknown"),
        "current_date": datetime.now().isoformat(),
        "differences": diff,
        "summary": f"{len(diff)} metric(s) changed." if diff else "No changes detected.",
    }
