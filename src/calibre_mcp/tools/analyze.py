"""High-level workflow tools that compose lower-level diagnostics.

These tools exist to compress the typical author-cleanup workflow
(duplicates → compilation coverage → attribution check → deletion proposal)
into single MCP calls so a session doesn't waste tokens on the same 10 SQL
queries per author.

Modules used: epub (TOC extraction), diagnose (duplicates), bulk (orphan checks).
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from . import epub as epub_tools
from ..utils.normalize import normalize_for_grouping

logger = logging.getLogger("calibre_mcp")


# ── #4: progress tracker — read calibre-hecho.md to skip done authors ──────

# Default path to the per-user progress log. The user can override via
# config.calibre_hecho_path if they relocate it.
_DEFAULT_HECHO_PATH = Path.home() / "claude" / "calibre-hecho.md"

# Recognises the leading author field in lines like:
#   "Author Name | 2026-05-10 | 5 | note..."
#   "Author Name | anterior | 27 FB2 | ..."
# Comment/blank/header lines are skipped.
_HECHO_LINE_RE = re.compile(r"^\s*([^|#\-][^|]*?)\s*\|")


def _read_processed_authors(path: Path) -> set[str]:
    """Return the set of normalized author names already logged in calibre-hecho.md.

    Returns an empty set if the file doesn't exist (so a fresh user gets
    the full author list on first call). Non-fatal on any read error.
    """
    if not path.exists():
        return set()
    processed: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.lstrip()
                if not stripped or stripped.startswith(("#", "-", "Formato")):
                    continue
                m = _HECHO_LINE_RE.match(line)
                if m:
                    processed.add(normalize_for_grouping(m.group(1)))
    except OSError as exc:
        logger.warning(f"Could not read {path}: {exc}")
    return processed


def next_author_to_process(
    backend,
    min_books: int = 10,
    limit: int = 5,
    hecho_path: str | None = None,
) -> dict[str, Any]:
    """Return the next N authors that haven't been logged in calibre-hecho.md yet.

    Sorts by descending book count so the highest-value cleanup targets come
    first. Excludes anything already in the progress log (matched by normalized
    name, so 'García Márquez, Gabriel' and 'gabriel garcia marquez' collide).

    Args:
        min_books: Skip authors with fewer than this many books.
        limit: How many candidates to return (default 5 — enough to choose from
            without flooding the response).
        hecho_path: Override path to calibre-hecho.md. Defaults to
            ~/claude/calibre-hecho.md.

    Returns:
        {
          "candidates": [{"author_id": int, "name": str, "book_count": int}, ...],
          "processed_count": int,         # how many authors are already done
          "hecho_file": str,
          "hecho_exists": bool,
        }
    """
    path = Path(hecho_path) if hecho_path else _DEFAULT_HECHO_PATH
    processed = _read_processed_authors(path)

    rows = backend.query_db(
        """
        SELECT a.id, a.name, COUNT(bal.book) AS book_count
        FROM authors a
        JOIN books_authors_link bal ON a.id = bal.author
        GROUP BY a.id, a.name
        HAVING COUNT(bal.book) >= ?
        ORDER BY book_count DESC, a.name
        """,
        (int(min_books),),
    )

    candidates: list[dict[str, Any]] = []
    for r in rows:
        if normalize_for_grouping(r["name"]) in processed:
            continue
        candidates.append({
            "author_id": r["id"],
            "name": r["name"],
            "book_count": r["book_count"],
        })
        if len(candidates) >= limit:
            break

    return {
        "candidates": candidates,
        "processed_count": len(processed),
        "hecho_file": str(path),
        "hecho_exists": path.exists(),
    }


# ── Title normalization shared by #2 and #3 ────────────────────────────────


def _normalize_title_for_match(s: str) -> str:
    """Aggressive normalization for fuzzy title comparison.

    Strips accents, articles, punctuation, common suffixes, and lowercases.
    'El Capitán de Salgari' and 'capitan salgari' produce the same key.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(
        r"^\s*(el|la|los|las|un|una|the|a|an|le|les|une|der|die|das)\s+",
        "",
        s,
    )
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── #2: compilation coverage ───────────────────────────────────────────────

# Title fragments that strongly suggest a book is a compilation/anthology.
_COMPILATION_KEYWORDS = re.compile(
    r"\b(obras?\s+completas?|narrativa\s+completa|cuentos?\s+completos?|"
    r"poes[íi]a\s+completa|teatro\s+completo|antolog[íi]a|recopilaci[óo]n|"
    r"compilaci[óo]n|relatos?\s+completos?|colecci[óo]n|"
    r"all\s+stories|complete\s+(works?|stories|novels?|poems?)|anthology)\b",
    re.IGNORECASE,
)

_DEFAULT_COMPILATION_MIN_KB = 200


def _is_compilation_candidate(title: str, size_kb: float) -> bool:
    """Heuristic: large file or title with anthology keywords → candidate."""
    return size_kb >= _DEFAULT_COMPILATION_MIN_KB or bool(
        _COMPILATION_KEYWORDS.search(title or "")
    )


def find_compilation_coverage(
    backend,
    author_id: int,
    min_compilation_kb: int = _DEFAULT_COMPILATION_MIN_KB,
    max_compilations: int = 50,
) -> dict[str, Any]:
    """Detect which of an author's individual works are covered by their compilations.

    Workflow:
      1. List every book by the author with EPUB format + size.
      2. For each EPUB whose size >= min_compilation_kb OR whose title matches
         compilation keywords, extract the TOC.
      3. Skip any compilation flagged as a likely scan-EPUB (size>50MB +
         generic TOC entries — they don't actually have navigable contents).
      4. Match each individual book's title against each compilation's TOC
         using normalized (accent-stripped, article-stripped) substring match.
      5. Report per-compilation coverage and per-book "is covered by" lists.

    Returns:
        {
          "author_id": int,
          "compilations": [
             {"book_id": int, "title": str, "size_mb": float,
              "toc_count": int, "skipped": str | None,
              "covered_book_ids": [int], "covered_titles": [str]},
            ...
          ],
          "individual_books": [
             {"book_id": int, "title": str, "format": str, "size_kb": float,
              "covered_by": [compilation_book_ids]},
            ...
          ],
          "summary": {
            "total_books": int,
            "compilation_count": int,
            "compilation_skipped_scans": int,
            "covered_count": int,        # individual books covered by ≥1 compilation
            "uncovered_count": int,
          },
        }
    """
    # All books by author with their primary format + size
    books = backend.query_db(
        """
        SELECT b.id, b.title, d.format, d.uncompressed_size
        FROM books b
        JOIN books_authors_link bal ON b.id = bal.book
        LEFT JOIN data d ON d.book = b.id
        WHERE bal.author = ?
        ORDER BY b.id, d.format
        """,
        (int(author_id),),
    )

    # Group by book_id; pick best format (EPUB if available, else any)
    by_book: dict[int, dict[str, Any]] = {}
    for r in books:
        bid = r["id"]
        if bid not in by_book:
            by_book[bid] = {
                "id": bid,
                "title": r["title"],
                "formats": [],
            }
        if r["format"]:
            by_book[bid]["formats"].append({
                "format": r["format"],
                "size_kb": round((r["uncompressed_size"] or 0) / 1024, 1),
            })

    # Identify compilation candidates (EPUB + size/title heuristic)
    candidates: list[tuple[int, str, float]] = []  # (book_id, title, size_kb)
    for bid, info in by_book.items():
        epub_fmt = next((f for f in info["formats"] if f["format"] == "EPUB"), None)
        if not epub_fmt:
            continue
        if _is_compilation_candidate(info["title"], epub_fmt["size_kb"]):
            candidates.append((bid, info["title"], epub_fmt["size_kb"]))

    # Extract TOC from each candidate (cap to avoid timeout on huge authors).
    # Sort: keyword matches first (high signal — "obras completas", "antología"),
    # then by size descending. This prevents a real but small anthology from
    # being dropped while large novels eat the cap (the "Varios" case).
    keyword_match_count = sum(
        1 for _, t, _ in candidates if _COMPILATION_KEYWORDS.search(t or "")
    )
    candidates_total = len(candidates)
    candidates = sorted(
        candidates,
        key=lambda x: (
            bool(_COMPILATION_KEYWORDS.search(x[1] or "")),  # True > False
            x[2],                                             # size_kb desc
        ),
        reverse=True,
    )
    compilations_capped = candidates_total > max_compilations
    candidates_dropped = max(0, candidates_total - max_compilations)
    if compilations_capped:
        candidates = candidates[:max_compilations]

    compilations: list[dict[str, Any]] = []
    skipped_scans = 0
    for bid, title, size_kb in candidates:
        toc_result = epub_tools.get_epub_toc(backend, bid)
        if toc_result.get("status") != "success":
            compilations.append({
                "book_id": bid,
                "title": title,
                "size_mb": round(size_kb / 1024, 2),
                "toc_count": 0,
                "skipped": toc_result.get("message", "TOC extraction failed"),
                "covered_book_ids": [],
                "covered_titles": [],
            })
            continue

        if toc_result.get("is_likely_scan"):
            skipped_scans += 1
            compilations.append({
                "book_id": bid,
                "title": title,
                "size_mb": toc_result.get("size_mb", round(size_kb / 1024, 2)),
                "toc_count": toc_result.get("toc_count", 0),
                "skipped": "likely_scan: " + ",".join(toc_result.get("scan_signals", [])),
                "covered_book_ids": [],
                "covered_titles": [],
            })
            continue

        # Pre-normalize TOC entries once for matching
        toc_norm = [_normalize_title_for_match(t) for t in toc_result.get("toc", [])]
        toc_norm_str = " | ".join(toc_norm)  # joined with separators to use substring match

        covered_ids: list[int] = []
        covered_titles: list[str] = []
        for other_bid, other_info in by_book.items():
            if other_bid == bid:
                continue
            tnorm = _normalize_title_for_match(other_info["title"])
            if not tnorm or len(tnorm) < 4:  # skip too-short titles (false positives)
                continue
            # Match if any TOC entry contains the normalized title
            # (covers "El cuento" matching TOC entry "El cuento de los caballos")
            if any(tnorm in entry or entry in tnorm for entry in toc_norm if entry):
                covered_ids.append(other_bid)
                covered_titles.append(other_info["title"])

        compilations.append({
            "book_id": bid,
            "title": title,
            "size_mb": toc_result.get("size_mb", round(size_kb / 1024, 2)),
            "toc_count": toc_result.get("toc_count", 0),
            "skipped": None,
            "covered_book_ids": covered_ids,
            "covered_titles": covered_titles,
        })

    # Build per-book "covered_by" reverse index
    covered_by: dict[int, list[int]] = {}
    for c in compilations:
        for cid in c["covered_book_ids"]:
            covered_by.setdefault(cid, []).append(c["book_id"])

    individual_books: list[dict[str, Any]] = []
    compilation_ids = {c["book_id"] for c in compilations}
    for bid, info in by_book.items():
        if bid in compilation_ids:
            continue
        primary_format = info["formats"][0] if info["formats"] else {"format": None, "size_kb": 0}
        individual_books.append({
            "book_id": bid,
            "title": info["title"],
            "format": primary_format["format"],
            "size_kb": primary_format["size_kb"],
            "covered_by": covered_by.get(bid, []),
        })

    covered_count = sum(1 for b in individual_books if b["covered_by"])

    return {
        "author_id": int(author_id),
        "compilations": compilations,
        "individual_books": individual_books,
        "summary": {
            "total_books": len(by_book),
            "compilation_count": len(compilations),
            "compilation_skipped_scans": skipped_scans,
            "compilations_capped": compilations_capped,         # True if >max_compilations were found
            "candidates_total": candidates_total,                # all candidates before cap
            "candidates_keyword_matched": keyword_match_count,   # title-keyword hits (high signal)
            "candidates_dropped_by_cap": candidates_dropped,     # how many we didn't extract
            "covered_count": covered_count,
            "uncovered_count": len(individual_books) - covered_count,
        },
    }


# ── #3: dedup resolution suggestions ───────────────────────────────────────

# Format preference (highest = best). EPUB beats FB2 because EPUB is the
# library standard and has best Calibre integration.
_FORMAT_RANK = {
    "EPUB": 100,
    "AZW3": 90,
    "MOBI": 80,
    "FB2":  70,
    "PDF":  60,
    "PRC":  50,
    "LIT":  40,
    "RTF":  30,
    "TXT":  20,
    "DOC":  20,
    "ZIP":  10,
    "RAR":  10,
    "CHM":   5,
    "LRF":   5,
    "CBZ":   5,
}


def _book_quality_score(book: dict[str, Any]) -> tuple[int, int, int, int, str]:
    """Tuple comparable as a quality score. Higher tuples = better book to keep.

    Components (in priority order):
      1. Format rank (EPUB > FB2 > etc.)
      2. Has cover (1 vs 0)
      3. Has comments/description (1 vs 0)
      4. File size (larger usually = more content; tiebreaker)
      5. Last-modified ISO string (most recent wins among equals)
    """
    fmt = (book.get("format") or "").upper()
    return (
        _FORMAT_RANK.get(fmt, 0),
        1 if book.get("has_cover") else 0,
        1 if book.get("has_comments") else 0,
        int(book.get("size_bytes") or 0),
        book.get("last_modified") or "",
    )


def _enrich_book_for_dedup(backend, book_id: int) -> dict[str, Any]:
    """Pull the metadata needed to score a book against duplicates."""
    rows = backend.query_db(
        """
        SELECT
            b.id, b.title, b.has_cover, b.last_modified,
            (SELECT COUNT(*) FROM comments c WHERE c.book = b.id AND length(c.text) > 0) AS has_comments,
            d.format, d.uncompressed_size
        FROM books b
        LEFT JOIN data d ON d.book = b.id
        WHERE b.id = ?
        ORDER BY
          CASE d.format
            WHEN 'EPUB' THEN 1 WHEN 'AZW3' THEN 2 WHEN 'MOBI' THEN 3
            WHEN 'FB2'  THEN 4 ELSE 5
          END
        LIMIT 1
        """,
        (int(book_id),),
    )
    if not rows:
        return {"id": int(book_id), "missing": True}
    r = rows[0]
    return {
        "id": r["id"],
        "title": r["title"],
        "format": r["format"],
        "size_bytes": r["uncompressed_size"] or 0,
        "has_cover": bool(r["has_cover"]),
        "has_comments": bool(r["has_comments"]),
        "last_modified": r["last_modified"],
    }


def suggest_dedup_resolution(
    backend,
    book_ids: list[int],
) -> dict[str, Any]:
    """Pick which book to keep among a duplicate group, and explain why.

    Apply, in order:
      1. Best format (EPUB > FB2 > PRC etc.)
      2. Has cover
      3. Has comments/description
      4. Largest file size
      5. Most recent last_modified

    Returns:
      {
        "keep": {book details with reason},
        "delete": [book details, ...],
        "ranked": [...all books ordered best-to-worst],
        "reasons": [list of human-readable bullet points explaining the choice]
      }

    Caller must still confirm with the user before deleting (Regla 7 del skill).
    """
    if len(book_ids) < 2:
        return {
            "status": "no_action",
            "message": "Need at least 2 book IDs to suggest a resolution.",
        }

    enriched = [_enrich_book_for_dedup(backend, bid) for bid in book_ids]
    valid = [b for b in enriched if not b.get("missing")]
    missing = [b["id"] for b in enriched if b.get("missing")]

    if len(valid) < 2:
        return {
            "status": "error",
            "message": f"Only {len(valid)} of {len(book_ids)} book IDs exist.",
            "missing": missing,
        }

    # Sort by score desc — best book first
    ranked = sorted(valid, key=_book_quality_score, reverse=True)
    keep = ranked[0]
    delete = ranked[1:]

    # Build human-readable reasons by finding the first axis where keep > best loser
    reasons: list[str] = []
    keep_score = _book_quality_score(keep)
    second_score = _book_quality_score(ranked[1])
    axis_names = ["format", "cover", "description", "file_size", "recency"]
    for i, axis in enumerate(axis_names):
        if keep_score[i] > second_score[i]:
            if axis == "format":
                reasons.append(f"keep format {keep['format']} > {ranked[1]['format']}")
            elif axis == "cover":
                reasons.append("keep has cover; loser does not")
            elif axis == "description":
                reasons.append("keep has comments/description; loser does not")
            elif axis == "file_size":
                reasons.append(
                    f"keep larger ({keep['size_bytes']} B vs {ranked[1]['size_bytes']} B)"
                )
            elif axis == "recency":
                reasons.append(
                    f"keep more recent ({keep['last_modified']} vs {ranked[1]['last_modified']})"
                )
            break  # one decisive reason is enough
    if not reasons:
        reasons.append("all books are tied on every quality axis — kept lowest book_id by default")

    return {
        "status": "suggested",
        "keep": keep,
        "delete": delete,
        "ranked": ranked,
        "reasons": reasons,
        "missing": missing,
    }


# ── #5: mega-tool — analyze a whole author in one call ─────────────────────


def analyze_author(
    backend,
    author_id: int,
    fuzzy_dup_threshold: float = 0.85,
) -> dict[str, Any]:
    """Single-call author cleanup analysis. Composes #2 + #3 + extra checks.

    Returns everything you need to decide what to do with an author's library:
      - All books with formats and sizes
      - Compilation coverage (#2)
      - Internal exact + fuzzy duplicates with resolution suggestions (#3)
      - Suspect attribution: other authors in the DB with normalized names
        similar to this one (potential merge candidates)
      - Series gaps (integer-indexed only)
      - Tag/cover/language gaps
      - Prioritized action list

    Args:
        author_id: The author to analyze.
        fuzzy_dup_threshold: Reserved for future tuning (currently unused
            because exact + fuzzy detection already runs on normalized titles).
    """
    # 1. Author exists?
    author = backend.query_db(
        "SELECT id, name, sort FROM authors WHERE id = ?", (int(author_id),)
    )
    if not author:
        return {"status": "error", "message": f"Author {author_id} not found."}
    a = author[0]

    # 2. Compilation coverage (also returns the per-book list)
    coverage = find_compilation_coverage(backend, author_id)

    # 3. Internal duplicates: scan this author's books for same normalized title
    title_groups: dict[str, list[int]] = {}
    title_lookup: dict[int, str] = {}
    for b in coverage["individual_books"] + [
        {"book_id": c["book_id"], "title": c["title"]} for c in coverage["compilations"]
    ]:
        t = _normalize_title_for_match(b["title"])
        title_lookup[b["book_id"]] = b["title"]
        if t:
            title_groups.setdefault(t, []).append(b["book_id"])

    duplicate_groups: list[dict[str, Any]] = []
    for norm_title, ids in title_groups.items():
        if len(ids) > 1:
            suggestion = suggest_dedup_resolution(backend, ids)
            duplicate_groups.append({
                "normalized_title": norm_title,
                "book_ids": ids,
                "titles": [title_lookup[i] for i in ids],
                "suggestion": suggestion,
            })

    # 4. Suspect attributions: similar-named authors elsewhere in the DB
    norm_self = normalize_for_grouping(a["name"])
    other_authors = backend.query_db(
        """
        SELECT a.id, a.name, COUNT(bal.book) AS book_count
        FROM authors a
        JOIN books_authors_link bal ON a.id = bal.author
        WHERE a.id != ?
        GROUP BY a.id, a.name
        """,
        (int(author_id),),
    )
    similar_authors = [
        {"id": r["id"], "name": r["name"], "book_count": r["book_count"]}
        for r in other_authors
        if normalize_for_grouping(r["name"]) == norm_self
    ]

    # 5. Series gaps for this author's series (integer indices only)
    series_rows = backend.query_db(
        """
        SELECT
          s.id, s.name,
          COUNT(CASE WHEN b.series_index = CAST(b.series_index AS INTEGER) THEN 1 END) AS whole_count,
          MIN(CASE WHEN b.series_index = CAST(b.series_index AS INTEGER) THEN b.series_index END) AS lo,
          MAX(CASE WHEN b.series_index = CAST(b.series_index AS INTEGER) THEN b.series_index END) AS hi,
          GROUP_CONCAT(
            CASE WHEN b.series_index = CAST(b.series_index AS INTEGER)
                 THEN CAST(b.series_index AS INTEGER) END
            ORDER BY b.series_index
          ) AS indices
        FROM series s
        JOIN books_series_link bsl ON s.id = bsl.series
        JOIN books b ON b.id = bsl.book
        JOIN books_authors_link bal ON b.id = bal.book
        WHERE bal.author = ?
        GROUP BY s.id, s.name
        HAVING whole_count >= 3
        """,
        (int(author_id),),
    )
    series_gaps = []
    for s in series_rows:
        gap = (int(s["hi"]) + 1) - s["whole_count"] if s["hi"] else 0
        if 1 <= gap <= 20:
            series_gaps.append({
                "series_id": s["id"],
                "name": s["name"],
                "have": s["whole_count"],
                "from": int(s["lo"]) if s["lo"] else None,
                "to":   int(s["hi"]) if s["hi"] else None,
                "missing": gap,
                "indices": s["indices"],
            })

    # 6. Metadata gaps (cover/tags/language/comments)
    gap_counts = backend.query_db(
        """
        SELECT
          SUM(CASE WHEN b.has_cover = 0 THEN 1 ELSE 0 END) AS no_cover,
          SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM books_tags_link bt WHERE bt.book = b.id) THEN 1 ELSE 0 END) AS no_tags,
          SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM books_languages_link bl WHERE bl.book = b.id) THEN 1 ELSE 0 END) AS no_language,
          SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM comments c WHERE c.book = b.id AND length(c.text) > 0) THEN 1 ELSE 0 END) AS no_description
        FROM books b
        JOIN books_authors_link bal ON b.id = bal.book
        WHERE bal.author = ?
        """,
        (int(author_id),),
    )
    gaps = gap_counts[0] if gap_counts else {}

    # 7. Build a prioritized action list
    actions: list[str] = []
    if similar_authors:
        names = ", ".join(f"{x['name']} (id={x['id']}, {x['book_count']} books)"
                          for x in similar_authors[:5])
        actions.append(
            f"REVIEW MERGE: {len(similar_authors)} similar-named author(s) found: {names}"
        )
    if duplicate_groups:
        actions.append(
            f"DEDUP: {len(duplicate_groups)} duplicate title group(s) — review suggestions below"
        )
    if coverage["summary"]["covered_count"]:
        actions.append(
            f"COMPILATION COVERAGE: {coverage['summary']['covered_count']} individual book(s) "
            f"covered by {coverage['summary']['compilation_count']} compilation(s) — "
            f"consider deleting covered after verification"
        )
    if series_gaps:
        actions.append(
            f"SERIES GAPS: {len(series_gaps)} series with 1-20 missing book(s)"
        )
    if gaps.get("no_cover"):
        actions.append(f"METADATA: {gaps['no_cover']} book(s) without cover")
    if gaps.get("no_tags"):
        actions.append(f"METADATA: {gaps['no_tags']} book(s) without tags")
    if not actions:
        actions.append("CLEAN: no actionable issues detected for this author")

    # Only return individual books that need attention: covered by a compilation
    # or part of a duplicate group. Clean uncovered books are summarised by count
    # to avoid token-overflow on large authors (400+ books).
    dup_ids: set[int] = {bid for g in duplicate_groups for bid in g["book_ids"]}
    notable_books = [
        b for b in coverage["individual_books"]
        if b["covered_by"] or b["book_id"] in dup_ids
    ]
    skipped_clean = len(coverage["individual_books"]) - len(notable_books)

    return {
        "status": "success",
        "author": {"id": a["id"], "name": a["name"], "sort": a["sort"]},
        "summary": coverage["summary"],
        "compilations": coverage["compilations"],
        "individual_books": notable_books,
        "individual_books_skipped_clean": skipped_clean,  # uncovered + no dups — nothing to do
        "duplicate_groups": duplicate_groups,
        "similar_named_authors": similar_authors,
        "series_gaps": series_gaps,
        "metadata_gaps": dict(gaps),
        "actions": actions,
    }
