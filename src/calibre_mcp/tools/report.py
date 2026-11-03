"""Reporting and metadata fetch tools (Hito 5)."""

import json
import logging
import re
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
from ..utils.normalize import normalize_for_grouping

logger = logging.getLogger("calibre_mcp")

# Google Books returns ISO-639-1 codes ("es"); Calibre libraries key languages
# by ISO-639-2/B ("spa"). Map common codes so apply=true doesn't create mixed
# language rows alongside the library's canonical 3-letter ones.
_LANG_ISO1_TO_ISO3 = {
    "es": "spa", "en": "eng", "fr": "fra", "de": "deu", "it": "ita",
    "pt": "por", "ca": "cat", "eu": "eus", "gl": "glg", "ru": "rus",
    "ja": "jpn", "zh": "zho", "nl": "nld", "pl": "pol", "sv": "swe",
}


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


# Calibre's "no date" sentinel for books.pubdate.
_UNDEFINED_PUBDATE_PREFIX = "0101-01-01"


def _title_key(title: str) -> set[str]:
    """Significant title tokens: accent/case/punctuation-insensitive, no subtitle."""
    main = re.split(r"[:(\[]", title or "", maxsplit=1)[0]
    return {t for t in normalize_for_grouping(main).split() if len(t) > 2} or \
           set(normalize_for_grouping(main).split())


def _match_problem(book: dict, found: dict) -> str | None:
    """
    Return why `found` is NOT the same book, or None if it matches.

    Both APIs return their top hit even when it is a different work, and the
    old code applied it blindly. Require: the found title shares at least half
    of the book's significant title tokens, and at least one author surname.
    """
    want = _title_key(book["title"])
    got = _title_key(found.get("title") or "")
    if not got or len(want & got) * 2 < len(want):
        return f"title mismatch: {found.get('title')!r} vs {book['title']!r}"

    book_tokens = set()
    for name in book["authors"]:
        book_tokens |= {t for t in normalize_for_grouping(name).split() if len(t) > 2}
    found_tokens = set()
    for name in found.get("authors") or []:
        found_tokens |= {t for t in normalize_for_grouping(name).split() if len(t) > 2}
    if not (book_tokens & found_tokens):
        return f"author mismatch: {found.get('authors')} vs {book['authors']}"
    return None


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

    With apply=True it only FILLS EMPTY fields (ISBN, publisher, pubdate,
    language, tags) and only when the hit is verified to be the same book
    (title + author check). It never replaces existing values: the previous
    version wiped every tag and overwrote publisher/language with whatever
    the API's top hit said, even when that hit was a different book.

    Args:
        book_ids: List of book IDs to look up
        source: 'openlibrary' or 'googlebooks' (default: openlibrary)
        apply: If True and dry_run=False, fill empty fields with found metadata
        dry_run: If True, only show what would be fetched/changed
    """
    if not book_ids:
        return {"status": "no_action", "message": "No book IDs provided."}

    placeholders = ",".join("?" * len(book_ids))
    books = backend.query_db(f"""
        SELECT b.id, b.title, b.author_sort, b.pubdate,
               (SELECT i.val FROM identifiers i WHERE i.book = b.id AND i.type = 'isbn' LIMIT 1) AS isbn,
               (SELECT GROUP_CONCAT(a.name, char(31)) FROM books_authors_link ba
                  JOIN authors a ON a.id = ba.author WHERE ba.book = b.id) AS author_names,
               EXISTS (SELECT 1 FROM books_publishers_link WHERE book = b.id) AS has_publisher,
               EXISTS (SELECT 1 FROM books_languages_link  WHERE book = b.id) AS has_language,
               EXISTS (SELECT 1 FROM books_tags_link       WHERE book = b.id) AS has_tags
        FROM books b WHERE b.id IN ({placeholders})
    """, tuple(book_ids))

    if not books:
        return {"status": "error", "message": "No books found for given IDs."}

    for b in books:
        # Calibre stores commas inside author names as '|'.
        b["authors"] = [n.replace("|", ",") for n in (b.get("author_names") or "").split("\x1f") if n]

    if dry_run:
        return {
            "status": "dry_run",
            "books": [{"id": b["id"], "title": b["title"],
                       "authors": b["authors"], "isbn": b["isbn"]}
                      for b in books],
            "source": source,
            "message": (
                f"Would query {source} for {len(books)} book(s). "
                f"Set dry_run=false to execute. "
                f"Set apply=true to also fill EMPTY fields from verified matches."
            ),
        }

    query_fn = _query_openlibrary if source == "openlibrary" else _query_googlebooks
    results = []

    for book in books:
        author_query = book["authors"][0] if book["authors"] else (book["author_sort"] or "")
        found = query_fn(book["title"], author_query)
        entry = {
            "id": book["id"],
            "title": book["title"],
            "found": found,
            "applied": False,
        }

        mismatch = _match_problem(book, found) if found and "error" not in found else None
        if mismatch:
            entry["match_rejected"] = mismatch

        if apply and found and "error" not in found and not mismatch:
            fields = {}
            if found.get("isbn") and not book["isbn"]:
                fields["identifiers"] = f"isbn:{found['isbn']}"
            if found.get("publisher") and not book["has_publisher"]:
                fields["publisher"] = found["publisher"]
            pubdate_empty = not book["pubdate"] or str(book["pubdate"]).startswith(_UNDEFINED_PUBDATE_PREFIX)
            if pubdate_empty and (found.get("publish_date") or found.get("publish_year")):
                fields["pubdate"] = found.get("publish_date") or f"{found['publish_year']}-01-01"
            if found.get("language") and not book["has_language"]:
                lang = str(found["language"]).lower()
                fields["languages"] = _LANG_ISO1_TO_ISO3.get(lang, lang)
            tags = found.get("subjects") or found.get("categories") or []
            if tags and not book["has_tags"]:
                fields["tags"] = ",".join(t.replace(",", " ") for t in tags[:5])

            if fields:
                try:
                    result = _set_metadata_sqlite(backend, book["id"], fields)
                    entry["applied"] = True
                    entry["applied_fields"] = fields
                    entry["changes"] = result.get("changes", [])
                except Exception as e:
                    entry["apply_error"] = str(e)
            else:
                entry["skipped"] = "no empty fields to fill"

        results.append(entry)

    return {
        "status": "completed",
        "source": source,
        "results": results,
        "found": sum(1 for r in results if r["found"] and "error" not in r["found"]),
        "rejected_mismatches": sum(1 for r in results if r.get("match_rejected")),
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
