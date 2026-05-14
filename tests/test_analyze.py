"""Unit tests for the workflow tools in tools/analyze.py.

These exercise the internal helpers (title normalization, processed-author
parsing, dedup scoring) without requiring a real Calibre library.
"""

from pathlib import Path
from types import SimpleNamespace

from calibre_mcp.tools.analyze import (
    _normalize_title_for_match,
    _read_processed_authors,
    _book_quality_score,
    _is_compilation_candidate,
    _is_meaningful_toc_entry,
    suggest_dedup_resolution,
)


class TestNormalizeTitleForMatch:
    def test_strips_accents(self):
        assert _normalize_title_for_match("Crónicas") == "cronicas"

    def test_strips_leading_articles(self):
        assert _normalize_title_for_match("El Capitán") == _normalize_title_for_match("Capitán")
        assert _normalize_title_for_match("The Lord") == _normalize_title_for_match("Lord")

    def test_lowercase(self):
        assert _normalize_title_for_match("HELLO") == "hello"

    def test_collapses_whitespace_and_punctuation(self):
        assert _normalize_title_for_match("Hello,  World!") == "hello world"

    def test_empty_returns_empty(self):
        assert _normalize_title_for_match("") == ""
        assert _normalize_title_for_match(None) == ""


class TestReadProcessedAuthors:
    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_processed_authors(tmp_path / "nope.md") == set()

    def test_parses_canonical_format(self, tmp_path):
        f = tmp_path / "hecho.md"
        f.write_text(
            "# Header\n"
            "Formato: line\n"
            "---\n"
            "Cervantes, Miguel de | 2026-01-01 | 5 | note\n"
            "Borges, Jorge Luis | anterior | 0 | clean\n",
            encoding="utf-8",
        )
        result = _read_processed_authors(f)
        # normalize_for_grouping sorts tokens, so 'Cervantes Miguel de' → 'cervantes de miguel'
        from calibre_mcp.utils.normalize import normalize_for_grouping
        assert normalize_for_grouping("Cervantes, Miguel de") in result
        assert normalize_for_grouping("Borges, Jorge Luis") in result

    def test_skips_comments_and_blanks(self, tmp_path):
        f = tmp_path / "hecho.md"
        f.write_text("# heading\n\n---\nReal Author | 2026 | 1 | x\n", encoding="utf-8")
        assert len(_read_processed_authors(f)) == 1


class TestBookQualityScore:
    def test_epub_beats_fb2(self):
        epub = {"format": "EPUB", "has_cover": False, "has_comments": False,
                "size_bytes": 0, "last_modified": "2020-01-01"}
        fb2  = {"format": "FB2",  "has_cover": True,  "has_comments": True,
                "size_bytes": 9999, "last_modified": "2026-01-01"}
        assert _book_quality_score(epub) > _book_quality_score(fb2)

    def test_cover_is_tiebreaker_within_format(self):
        a = {"format": "EPUB", "has_cover": True,  "has_comments": False,
             "size_bytes": 0, "last_modified": ""}
        b = {"format": "EPUB", "has_cover": False, "has_comments": False,
             "size_bytes": 0, "last_modified": ""}
        assert _book_quality_score(a) > _book_quality_score(b)


class TestIsCompilationCandidate:
    def test_large_size_qualifies(self):
        assert _is_compilation_candidate("Some short story", 500)

    def test_small_size_with_keyword_qualifies(self):
        assert _is_compilation_candidate("Obras Completas", 50)

    def test_small_size_no_keyword_does_not(self):
        assert not _is_compilation_candidate("Some Book", 50)

    def test_anthology_keyword(self):
        assert _is_compilation_candidate("Antología poética", 100)

    def test_complete_works_english(self):
        assert _is_compilation_candidate("Complete Works", 100)


class TestIsMeaningfulTocEntry:
    """Filters out structural TOC entries that caused false-positive coverage matches."""

    def test_chapter_heading_rejected(self):
        assert not _is_meaningful_toc_entry("capitulo i")
        assert not _is_meaningful_toc_entry("chapter 5")
        assert not _is_meaningful_toc_entry("parte primera")

    def test_decorative_separator_rejected(self):
        assert not _is_meaningful_toc_entry("***")
        assert not _is_meaningful_toc_entry("---")

    def test_short_keywords_rejected(self):
        assert not _is_meaningful_toc_entry("fin")
        assert not _is_meaningful_toc_entry("prologo")
        assert not _is_meaningful_toc_entry("introduccion")

    def test_pure_number_rejected(self):
        assert not _is_meaningful_toc_entry("123")
        assert not _is_meaningful_toc_entry("3.")

    def test_real_titles_accepted(self):
        assert _is_meaningful_toc_entry("el chacal")
        assert _is_meaningful_toc_entry("crimen y castigo")

    def test_too_short_rejected(self):
        assert not _is_meaningful_toc_entry("")
        assert not _is_meaningful_toc_entry("ab")


class TestCoverageMatcher:
    """Regression tests for the coverage matcher: title vs. TOC entry.

    These exercise find_compilation_coverage end-to-end via a fake backend so
    we can guarantee the 75% coverage ratio + structural filter behaviour.
    """

    def _make_backend(self, books_rows, toc_for):
        """books_rows: list of dict for the SELECT b.id, b.title, d.format... query.
        toc_for: dict of book_id -> list of TOC entries (used by faked get_epub_toc).
        """
        from calibre_mcp.tools import epub as epub_tools

        def query_db(sql, params=None):
            return list(books_rows)

        # Patch get_epub_toc on the module — find_compilation_coverage calls it directly.
        original = epub_tools.get_epub_toc

        def fake_get_toc(backend, bid, **kw):
            entries = toc_for.get(int(bid), [])
            return {
                "status": "success",
                "is_likely_scan": False,
                "toc": entries,
                "toc_count": len(entries),
                "size_mb": 1.0,
            }

        epub_tools.get_epub_toc = fake_get_toc

        backend = SimpleNamespace(query_db=query_db, library="/lib")
        return backend, original  # caller restores get_epub_toc

    def test_short_title_does_not_match_longer_toc_entry(self):
        """The 'el chacal' regression: must not match 'el reto del chacal'."""
        from calibre_mcp.tools import epub as epub_tools
        from calibre_mcp.tools.analyze import find_compilation_coverage

        books = [
            # The compilation (book 1) — large EPUB
            {"id": 1, "title": "Obras Completas", "format": "EPUB", "uncompressed_size": 5_000_000},
            # The individual book (book 2) with the short title
            {"id": 2, "title": "El Chacal", "format": "EPUB", "uncompressed_size": 50_000},
        ]
        toc_for = {1: ["El reto del chacal", "Otro cuento largo"]}
        backend, original = self._make_backend(books, toc_for)
        try:
            result = find_compilation_coverage(backend, author_id=1)
        finally:
            epub_tools.get_epub_toc = original

        # Book 2 must NOT be marked as covered by book 1
        compilation = next(c for c in result["compilations"] if c["book_id"] == 1)
        assert 2 not in compilation["covered_book_ids"], (
            f"'El Chacal' wrongly matched 'El reto del chacal' "
            f"(coverage={compilation['covered_book_ids']})"
        )

    def test_chapter_heading_does_not_match_book_title(self):
        """Structural TOC entries (CAPÍTULO I) must not match book titles."""
        from calibre_mcp.tools import epub as epub_tools
        from calibre_mcp.tools.analyze import find_compilation_coverage

        books = [
            {"id": 1, "title": "Compilación", "format": "EPUB", "uncompressed_size": 5_000_000},
            # A book whose normalized title starts with 'capitulo' or similar
            {"id": 2, "title": "Capítulos perdidos", "format": "EPUB", "uncompressed_size": 50_000},
        ]
        toc_for = {1: ["Capítulo I", "Capítulo II", "Fin"]}
        backend, original = self._make_backend(books, toc_for)
        try:
            result = find_compilation_coverage(backend, author_id=1)
        finally:
            epub_tools.get_epub_toc = original

        compilation = next(c for c in result["compilations"] if c["book_id"] == 1)
        assert 2 not in compilation["covered_book_ids"]

    def test_exact_short_match_still_works(self):
        """Don't break the legitimate case: 'el chacal' = TOC entry 'el chacal'."""
        from calibre_mcp.tools import epub as epub_tools
        from calibre_mcp.tools.analyze import find_compilation_coverage

        books = [
            {"id": 1, "title": "Obras Completas", "format": "EPUB", "uncompressed_size": 5_000_000},
            {"id": 2, "title": "El Chacal", "format": "EPUB", "uncompressed_size": 50_000},
        ]
        toc_for = {1: ["El chacal"]}
        backend, original = self._make_backend(books, toc_for)
        try:
            result = find_compilation_coverage(backend, author_id=1)
        finally:
            epub_tools.get_epub_toc = original

        compilation = next(c for c in result["compilations"] if c["book_id"] == 1)
        assert 2 in compilation["covered_book_ids"], (
            "exact-title match must still work"
        )


class TestSuggestDedupResolution:
    def _backend_with_books(self, books_by_id):
        """Build a fake backend that returns canned book metadata."""
        def query_db(sql, params):
            bid = int(params[0])
            book = books_by_id.get(bid)
            if not book:
                return []
            return [book]
        return SimpleNamespace(query_db=query_db)

    def test_too_few_book_ids(self):
        backend = self._backend_with_books({})
        result = suggest_dedup_resolution(backend, [1])
        assert result["status"] == "no_action"

    def test_picks_epub_over_fb2(self):
        books = {
            1: {"id": 1, "title": "X", "format": "FB2", "uncompressed_size": 9999,
                "has_cover": 1, "has_comments": 1, "last_modified": "2026-01-01"},
            2: {"id": 2, "title": "X", "format": "EPUB", "uncompressed_size": 100,
                "has_cover": 0, "has_comments": 0, "last_modified": "2020-01-01"},
        }
        backend = self._backend_with_books(books)
        result = suggest_dedup_resolution(backend, [1, 2])
        assert result["status"] == "suggested"
        assert result["keep"]["id"] == 2
        assert "format" in result["reasons"][0]

    def test_missing_book_reported(self):
        books = {
            1: {"id": 1, "title": "X", "format": "EPUB", "uncompressed_size": 1,
                "has_cover": 0, "has_comments": 0, "last_modified": ""},
        }
        backend = self._backend_with_books(books)
        result = suggest_dedup_resolution(backend, [1, 99])
        assert result["status"] == "error"
        assert 99 in result["missing"]
