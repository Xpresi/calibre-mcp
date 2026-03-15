"""Query and search tools for the Calibre library (Hito 1)."""

import logging
from typing import Any

logger = logging.getLogger("calibre_mcp")


def search_books(
    backend,
    title: str | None = None,
    author: str | None = None,
    series: str | None = None,
    tag: str | None = None,
    publisher: str | None = None,
    format: str | None = None,
    identifier: str | None = None,
    has_cover: bool | None = None,
    has_formats: bool | None = None,
    query_string: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Search books with multiple optional filters.

    Two modes:
    - Structured: use title, author, series, tag, etc. params (SQL-based, more flexible)
    - calibredb native: pass query_string using Calibre's search syntax
      (e.g., "author:King AND tag:horror AND not format:pdf")
    """
    # ── Mode 2: calibredb native search ──
    if query_string is not None:
        try:
            output = backend.run_calibredb("search", [query_string])
            # calibredb search returns comma-separated IDs
            raw = output.strip()
            if not raw:
                return {"mode": "calibredb_search", "query": query_string, "ids": [], "count": 0}
            ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
            # Optionally fetch details for the found IDs
            if ids and limit > 0:
                subset = ids[offset:offset + limit]
                if subset:
                    ph = ",".join("?" * len(subset))
                    books = backend.query_db(f"""
                        SELECT b.id, b.title, b.author_sort, b.series_index,
                               (SELECT s.name FROM books_series_link bsl
                                JOIN series s ON bsl.series = s.id
                                WHERE bsl.book = b.id) as series_name
                        FROM books b WHERE b.id IN ({ph})
                        ORDER BY b.sort
                    """, tuple(subset))
                else:
                    books = []
                return {
                    "mode": "calibredb_search",
                    "query": query_string,
                    "total_ids": len(ids),
                    "books": books,
                    "showing": len(books),
                }
            return {"mode": "calibredb_search", "query": query_string, "ids": ids, "count": len(ids)}
        except Exception as e:
            return {"mode": "calibredb_search", "error": str(e)}

    # ── Mode 1: Structured SQL search ──
    conditions = []
    params = []

    if title:
        conditions.append("b.title LIKE ?")
        params.append(f"%{title}%")
    if author:
        conditions.append("""b.id IN (
            SELECT bal.book FROM books_authors_link bal
            JOIN authors a ON bal.author = a.id
            WHERE a.name LIKE ?
        )""")
        params.append(f"%{author}%")
    if series:
        conditions.append("""b.id IN (
            SELECT bsl.book FROM books_series_link bsl
            JOIN series s ON bsl.series = s.id
            WHERE s.name LIKE ?
        )""")
        params.append(f"%{series}%")
    if tag:
        conditions.append("""b.id IN (
            SELECT btl.book FROM books_tags_link btl
            JOIN tags t ON btl.tag = t.id
            WHERE t.name LIKE ?
        )""")
        params.append(f"%{tag}%")
    if publisher:
        conditions.append("""b.id IN (
            SELECT bpl.book FROM books_publishers_link bpl
            JOIN publishers p ON bpl.publisher = p.id
            WHERE p.name LIKE ?
        )""")
        params.append(f"%{publisher}%")
    if format:
        conditions.append("""b.id IN (
            SELECT d.book FROM data d WHERE d.format = ?
        )""")
        params.append(format.upper())
    if identifier:
        conditions.append("""b.id IN (
            SELECT i.book FROM identifiers i WHERE i.val LIKE ?
        )""")
        params.append(f"%{identifier}%")
    if has_cover is not None:
        conditions.append("b.has_cover = ?")
        params.append(1 if has_cover else 0)
    if has_formats is not None:
        if has_formats:
            conditions.append("EXISTS (SELECT 1 FROM data d WHERE d.book = b.id)")
        else:
            conditions.append("NOT EXISTS (SELECT 1 FROM data d WHERE d.book = b.id)")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    sql = f"""
        SELECT b.id, b.title, b.author_sort, b.pubdate, b.series_index,
               b.has_cover, b.path,
               (SELECT GROUP_CONCAT(a.name, ' & ')
                FROM books_authors_link bal JOIN authors a ON bal.author = a.id
                WHERE bal.book = b.id) as authors,
               (SELECT s.name FROM books_series_link bsl
                JOIN series s ON bsl.series = s.id
                WHERE bsl.book = b.id) as series_name,
               (SELECT GROUP_CONCAT(d.format, ', ')
                FROM data d WHERE d.book = b.id) as formats
        FROM books b
        {where}
        ORDER BY b.sort
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    return backend.query_db(sql, tuple(params))


def get_book_details(backend, book_id: int) -> dict[str, Any] | None:
    """Get complete details for a single book."""
    books = backend.query_db("""
        SELECT b.id, b.title, b.sort as title_sort, b.author_sort,
               b.pubdate, b.timestamp, b.series_index, b.has_cover, b.path,
               b.last_modified
        FROM books b WHERE b.id = ?
    """, (book_id,))

    if not books:
        return None

    book = books[0]

    # Authors
    book["authors"] = backend.query_db("""
        SELECT a.id, a.name, a.sort
        FROM books_authors_link bal JOIN authors a ON bal.author = a.id
        WHERE bal.book = ?
    """, (book_id,))

    # Series
    series = backend.query_db("""
        SELECT s.id, s.name FROM books_series_link bsl
        JOIN series s ON bsl.series = s.id WHERE bsl.book = ?
    """, (book_id,))
    book["series"] = series[0] if series else None

    # Tags
    book["tags"] = [r["name"] for r in backend.query_db("""
        SELECT t.name FROM books_tags_link btl
        JOIN tags t ON btl.tag = t.id WHERE btl.book = ?
    """, (book_id,))]

    # Publisher
    pubs = backend.query_db("""
        SELECT p.name FROM books_publishers_link bpl
        JOIN publishers p ON bpl.publisher = p.id WHERE bpl.book = ?
    """, (book_id,))
    book["publisher"] = pubs[0]["name"] if pubs else None

    # Formats
    book["formats"] = backend.query_db("""
        SELECT format, uncompressed_size, name FROM data WHERE book = ?
    """, (book_id,))

    # Identifiers (ISBN, etc.)
    book["identifiers"] = {r["type"]: r["val"] for r in backend.query_db("""
        SELECT type, val FROM identifiers WHERE book = ?
    """, (book_id,))}

    # Languages
    book["languages"] = [r["lang_code"] for r in backend.query_db("""
        SELECT l.lang_code FROM books_languages_link bl
        JOIN languages l ON bl.lang_code = l.id WHERE bl.book = ?
    """, (book_id,))]

    # Comments/Description
    comments = backend.query_db("SELECT text FROM comments WHERE book = ?", (book_id,))
    book["description"] = comments[0]["text"] if comments else None

    return book


def list_authors(
    backend,
    search: str | None = None,
    min_books: int = 0,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "name",
) -> list[dict[str, Any]]:
    """List authors with book counts."""
    conditions = []
    params = []

    if search:
        conditions.append("a.name LIKE ?")
        params.append(f"%{search}%")

    having = f"HAVING COUNT(bal.book) >= {min_books}" if min_books > 0 else ""
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    order = "a.sort" if sort_by == "name" else "book_count DESC"

    sql = f"""
        SELECT a.id, a.name, a.sort, COUNT(bal.book) as book_count
        FROM authors a
        LEFT JOIN books_authors_link bal ON a.id = bal.author
        {where}
        GROUP BY a.id
        {having}
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    return backend.query_db(sql, tuple(params))


def list_series(
    backend,
    search: str | None = None,
    min_books: int = 0,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List series with book counts and number range."""
    conditions = []
    params = []

    if search:
        conditions.append("s.name LIKE ?")
        params.append(f"%{search}%")

    having = f"HAVING COUNT(bsl.book) >= {min_books}" if min_books > 0 else ""
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    sql = f"""
        SELECT s.id, s.name,
               COUNT(bsl.book) as book_count,
               MIN(b.series_index) as min_index,
               MAX(b.series_index) as max_index
        FROM series s
        LEFT JOIN books_series_link bsl ON s.id = bsl.series
        LEFT JOIN books b ON bsl.book = b.id
        {where}
        GROUP BY s.id
        {having}
        ORDER BY s.sort
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    return backend.query_db(sql, tuple(params))


def list_tags(
    backend,
    search: str | None = None,
    min_books: int = 0,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List tags with book counts."""
    conditions = []
    params = []

    if search:
        conditions.append("t.name LIKE ?")
        params.append(f"%{search}%")

    having = f"HAVING COUNT(btl.book) >= {min_books}" if min_books > 0 else ""
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    sql = f"""
        SELECT t.id, t.name, COUNT(btl.book) as book_count
        FROM tags t
        LEFT JOIN books_tags_link btl ON t.id = btl.tag
        {where}
        GROUP BY t.id
        {having}
        ORDER BY book_count DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    return backend.query_db(sql, tuple(params))


def list_publishers(
    backend,
    search: str | None = None,
    min_books: int = 0,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List publishers with book counts."""
    conditions = []
    params = []

    if search:
        conditions.append("p.name LIKE ?")
        params.append(f"%{search}%")

    having = f"HAVING COUNT(bpl.book) >= {min_books}" if min_books > 0 else ""
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    sql = f"""
        SELECT p.id, p.name, COUNT(bpl.book) as book_count
        FROM publishers p
        LEFT JOIN books_publishers_link bpl ON p.id = bpl.publisher
        {where}
        GROUP BY p.id
        {having}
        ORDER BY book_count DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    return backend.query_db(sql, tuple(params))


def raw_sql_query(
    backend,
    sql: str,
    params: list | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """
    Execute a read-only SQL SELECT query directly against metadata.db.

    Only SELECT statements are permitted. Any attempt to run INSERT, UPDATE,
    DELETE, DROP, CREATE, ATTACH, or PRAGMA write operations is rejected.

    Args:
        sql:    A SELECT query. May reference any table in metadata.db.
                Useful tables: books, authors, series, tags, publishers,
                books_authors_link, books_series_link, books_tags_link,
                books_publishers_link, books_languages_link, identifiers,
                data (formats), comments.
        params: Optional list of positional parameters (? placeholders).
        limit:  Maximum rows returned (default 200, hard cap 1000).
    """
    import re

    # ── Safety: only allow SELECT ──────────────────────────────────────
    normalized = sql.strip().lstrip(";").strip()
    first_word = re.split(r"\s+", normalized, maxsplit=1)[0].upper()
    if first_word != "SELECT":
        return {
            "status": "error",
            "message": (
                f"Only SELECT statements are allowed. Got: '{first_word}'. "
                "Use the appropriate write tools for modifications."
            ),
        }

    # Block dangerous keywords even inside SELECT (e.g. SELECT ... INTO)
    blocked = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|DETACH|REPLACE|PRAGMA)\b",
        re.IGNORECASE,
    )
    if blocked.search(normalized):
        return {
            "status": "error",
            "message": "Query contains a forbidden keyword. Only pure SELECT queries are allowed.",
        }

    # Enforce row limit (hard cap at 1000)
    effective_limit = min(limit, 1000)

    # Wrap in a subquery to enforce the limit without altering user's query
    wrapped = f"SELECT * FROM ({normalized}) AS _q LIMIT {effective_limit}"

    try:
        rows = backend.query_db(wrapped, tuple(params or []))
        return {
            "status": "success",
            "rows": rows,
            "count": len(rows),
            "truncated": len(rows) == effective_limit,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
