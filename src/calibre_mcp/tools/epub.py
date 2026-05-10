"""EPUB-related read-only tools (TOC extraction, scan-EPUB detection).

Useful for verifying recopilatorio coverage before deleting individual works
that may already be included in a collection.
"""

import html
import os
import re
import zipfile
from typing import Any

from . import _toc_cache


# Patterns that suggest a TOC entry is a generic page/scan marker
# rather than a real chapter/work title.
_GENERIC_TOC_PATTERNS = [
    re.compile(r"^(WS|Page|Pag|Pg|Scan|IMG|Img|Image)[-_\s]*\d+$", re.IGNORECASE),
    re.compile(r"^\d{3,4}[-_]\d{3,4}$"),                     # 0001-0002, 130514-0002
    re.compile(r"^[A-Z]{1,3}[-_\s]*\d{2,}$"),                 # WS-0001, A-12
    re.compile(r"^(scan|escaneo|escaneado)[-_\s]", re.IGNORECASE),
    re.compile(r"^p[áa]g(ina)?[-_\s.]*\d+$", re.IGNORECASE),  # pag-001, página 12
]

# Markers in book titles that suggest the EPUB is a scan rip
_TITLE_SCAN_MARKERS = [
    re.compile(r"\[\s*por\s+\w+\s*\]", re.IGNORECASE),       # [por Ganz1912]
    re.compile(r"\bscan(ned)?\b", re.IGNORECASE),
    re.compile(r"\bescaneado\b", re.IGNORECASE),
    re.compile(r"\bOCR\b"),
]

_SCAN_SIZE_THRESHOLD_MB = 50.0
_TOC_GENERIC_RATIO_THRESHOLD = 0.7
_MAX_TOC_ENTRIES = 500


def _extract_toc_entries(epub_path: str) -> tuple[list[str], str]:
    """Extract TOC entries from an EPUB. Returns (entries, source).

    Source is one of: "ncx", "nav", "none".
    """
    try:
        with zipfile.ZipFile(epub_path) as z:
            names = z.namelist()

            # Try toc.ncx first (EPUB 2 standard, also used in EPUB 3)
            ncx_files = [n for n in names if n.lower().endswith("toc.ncx")]
            if ncx_files:
                content = z.read(ncx_files[0]).decode("utf-8", errors="ignore")
                # navLabel/text pairs — the order matters in toc.ncx, just grab <text>
                titles = re.findall(r"<text[^>]*>(.*?)</text>", content, re.DOTALL | re.IGNORECASE)
                return _clean_titles(titles), "ncx"

            # Fall back to EPUB 3 nav document
            nav_files = [n for n in names if n.lower().endswith(("nav.xhtml", "nav.html"))]
            if nav_files:
                content = z.read(nav_files[0]).decode("utf-8", errors="ignore")
                # Strip nested tags inside <a>...</a>
                titles = re.findall(r"<a[^>]*>(.*?)</a>", content, re.DOTALL | re.IGNORECASE)
                # Remove inner HTML tags
                titles = [re.sub(r"<[^>]+>", "", t) for t in titles]
                return _clean_titles(titles), "nav"

            return [], "none"

    except (zipfile.BadZipFile, KeyError, OSError):
        return [], "none"


def _clean_titles(raw: list[str]) -> list[str]:
    """Decode HTML entities, strip whitespace, drop empties."""
    out = []
    for t in raw:
        t = html.unescape(t).strip()
        # Collapse whitespace
        t = re.sub(r"\s+", " ", t)
        if t:
            out.append(t)
    return out


def _is_generic_toc_entry(entry: str) -> bool:
    """True if the entry looks like an auto-generated page/scan marker."""
    for pat in _GENERIC_TOC_PATTERNS:
        if pat.match(entry):
            return True
    return False


def _detect_scan_signals(
    title: str,
    size_mb: float,
    toc_entries: list[str],
) -> list[str]:
    """Return a list of human-readable signals that this EPUB is a scan rip."""
    signals = []

    if size_mb > _SCAN_SIZE_THRESHOLD_MB:
        signals.append(f"size>{int(_SCAN_SIZE_THRESHOLD_MB)}MB ({size_mb:.1f} MB)")

    if toc_entries:
        generic_count = sum(1 for e in toc_entries if _is_generic_toc_entry(e))
        ratio = generic_count / len(toc_entries)
        if ratio >= _TOC_GENERIC_RATIO_THRESHOLD:
            pct = int(ratio * 100)
            signals.append(f"toc_pattern:generic_{pct}%")

    for pat in _TITLE_SCAN_MARKERS:
        if pat.search(title or ""):
            signals.append(f"title:scan_marker ({pat.pattern})")
            break  # one is enough

    return signals


def get_epub_toc(backend, book_id: int, use_cache: bool = True) -> dict[str, Any]:
    """Extract TOC from an EPUB book and detect scan-EPUB signals.

    Returns metadata, the TOC entries (truncated at 500), and heuristic flags
    for scan detection. Useful before deleting individual works that may be
    covered by a recopilatorio.

    Caching: results are stored in a sidecar SQLite DB inside the library
    folder (`_calibre_mcp_toc_cache.db`), keyed by (book_id, mtime, file_size).
    Set use_cache=False to force re-extraction (e.g. after editing the EPUB).
    Cache hits skip the zip/regex work entirely — useful when running
    find_compilation_coverage repeatedly across sessions.
    """
    rows = backend.query_db(
        """
        SELECT b.id, b.title, b.path, d.name, d.format, d.uncompressed_size
        FROM books b
        JOIN data d ON b.id = d.book
        WHERE b.id = ? AND d.format = 'EPUB'
        LIMIT 1
        """,
        (int(book_id),),
    )

    if not rows:
        # Distinguish "no EPUB format" from "book doesn't exist"
        exists = backend.query_db_scalar(
            "SELECT 1 FROM books WHERE id = ?", (int(book_id),)
        )
        if not exists:
            return {
                "status": "error",
                "book_id": int(book_id),
                "message": f"Book {book_id} does not exist in the library.",
            }
        return {
            "status": "error",
            "book_id": int(book_id),
            "message": f"Book {book_id} has no EPUB format.",
        }

    row = rows[0]
    title = row["title"]
    rel_path = row["path"]
    file_name = row["name"]
    size_bytes = row["uncompressed_size"] or 0
    size_mb = round(size_bytes / 1024 / 1024, 2)

    epub_path = os.path.join(backend.library, rel_path, f"{file_name}.epub")

    if not os.path.exists(epub_path):
        return {
            "status": "error",
            "book_id": int(book_id),
            "title": title,
            "expected_path": epub_path,
            "message": "EPUB file not found on disk.",
        }

    cache_hit = False
    if use_cache:
        cached = _toc_cache.get_cached(backend.library, int(book_id), epub_path)
        if cached is not None:
            cache_hit = True
            toc_entries = cached["toc"]
            source = cached["toc_source"]
            signals = cached["scan_signals"]
            is_likely_scan = cached["is_likely_scan"]

    if not cache_hit:
        toc_entries, source = _extract_toc_entries(epub_path)
        signals = _detect_scan_signals(title, size_mb, toc_entries)
        is_likely_scan = len(signals) >= 2 or (
            len(signals) == 1 and any("toc_pattern:generic" in s for s in signals)
        )
        # Persist before truncation so a future invocation can serve the full set.
        if use_cache:
            _toc_cache.store(
                backend.library, int(book_id), epub_path,
                toc_entries, source, is_likely_scan, signals,
            )

    truncated = False
    if len(toc_entries) > _MAX_TOC_ENTRIES:
        toc_entries = toc_entries[:_MAX_TOC_ENTRIES]
        truncated = True

    return {
        "status": "success",
        "book_id": int(book_id),
        "title": title,
        "format": "EPUB",
        "size_mb": size_mb,
        "path": epub_path,
        "toc_source": source,
        "toc": toc_entries,
        "toc_count": len(toc_entries),
        "toc_truncated": truncated,
        "is_likely_scan": is_likely_scan,
        "scan_signals": signals,
        "cache_hit": cache_hit,
    }
