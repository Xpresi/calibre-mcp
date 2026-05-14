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
    _title_matches_toc_entry,
    _title_match_kind,
    suggest_dedup_resolution,
    regenerate_pending_worklist,
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

    def test_parses_markdown_table_rows(self, tmp_path):
        """Lots 5 and 6 of the real hecho.md use markdown table format."""
        from calibre_mcp.utils.normalize import normalize_for_grouping
        f = tmp_path / "hecho.md"
        f.write_text(
            "| Autor | Libros | Borrados | Nota |\n"
            "|---|---|---|---|\n"
            "| Steel, Danielle | 76 | 0 | 4 falsos positivos |\n"
            "| Neels, Betty | 74 | 0 | Limpio |\n"
            "| Gardner, Erle Stanley | 93 | 0 | Perry Mason |\n",
            encoding="utf-8",
        )
        result = _read_processed_authors(f)
        assert normalize_for_grouping("Steel, Danielle") in result
        assert normalize_for_grouping("Neels, Betty") in result
        assert normalize_for_grouping("Gardner, Erle Stanley") in result
        # Header row "Autor" must NOT be treated as an author name
        assert normalize_for_grouping("Autor") not in result
        # Separator row "|---|---|---|---|" must NOT be parsed
        assert len(result) == 3

    def test_mixed_canonical_and_table_formats(self, tmp_path):
        """A real hecho.md mixes both formats — both must be picked up."""
        from calibre_mcp.utils.normalize import normalize_for_grouping
        f = tmp_path / "hecho.md"
        f.write_text(
            "# Calibre — Autores ya procesados\n"
            "Formato: `Autor | Fecha | Borrados | Nota`\n"
            "---\n"
            "Cervantes, Miguel de | anterior | 20 | note\n"
            "Borges, Jorge Luis | anterior | 0 | clean\n"
            "\n"
            "| Autor | Libros | Borrados | Nota |\n"
            "|---|---|---|---|\n"
            "| Steel, Danielle | 76 | 0 | falsos positivos |\n",
            encoding="utf-8",
        )
        result = _read_processed_authors(f)
        assert normalize_for_grouping("Cervantes, Miguel de") in result
        assert normalize_for_grouping("Borges, Jorge Luis") in result
        assert normalize_for_grouping("Steel, Danielle") in result
        assert len(result) == 3


class TestRegeneratePendingWorklist:
    def test_writes_pending_authors_filtered_by_done(self, tmp_path):
        hecho = tmp_path / "hecho.md"
        hecho.write_text(
            "Steel, Danielle | 2026-05-14 | 0 | done\n",
            encoding="utf-8",
        )
        out = tmp_path / "pendientes.md"

        # Fake backend with deterministic rows
        class FakeBackend:
            def query_db(self, sql, params):
                # Return three authors above threshold; only Steel is in hecho.md
                return [
                    {"id": 10, "name": "Steel, Danielle", "book_count": 76},
                    {"id": 20, "name": "Neels, Betty", "book_count": 74},
                    {"id": 30, "name": "Gardner, Erle Stanley", "book_count": 93},
                ]

        result = regenerate_pending_worklist(
            FakeBackend(),
            min_books=15,
            output_path=str(out),
            hecho_path=str(hecho),
        )
        assert result["pending_count"] == 2
        assert result["processed_count"] == 1
        assert result["total_authors_above_threshold"] == 3

        content = out.read_text(encoding="utf-8")
        assert "- [ ] Neels, Betty (74) [id=20]" in content
        assert "- [ ] Gardner, Erle Stanley (93) [id=30]" in content
        # Steel is in hecho, must be excluded
        assert "Steel, Danielle" not in content

    def test_creates_parent_directory(self, tmp_path):
        hecho = tmp_path / "hecho.md"
        hecho.write_text("", encoding="utf-8")
        out = tmp_path / "subdir" / "pendientes.md"

        class FakeBackend:
            def query_db(self, sql, params):
                return [{"id": 1, "name": "X, Y", "book_count": 20}]

        regenerate_pending_worklist(
            FakeBackend(), output_path=str(out), hecho_path=str(hecho)
        )
        assert out.exists()


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

    def test_short_toc_entry_does_not_match_long_title(self):
        """The 'Steel' regression: short TOC entry as substring of long title.

        Pre-fix: a 5-char TOC entry like 'paris' would match the 18-char title
        'una mujer en parís' because the asymmetric ratio check only kicked in
        when the TITLE was the substring, not when the TOC entry was.

        Post-fix: ratio applies in both directions, so 5/18 = 0.28 < 0.75 → no match.
        """
        from calibre_mcp.tools import epub as epub_tools
        from calibre_mcp.tools.analyze import find_compilation_coverage

        books = [
            {"id": 1, "title": "Antología", "format": "EPUB", "uncompressed_size": 5_000_000},
            {"id": 2, "title": "Una mujer en París", "format": "EPUB", "uncompressed_size": 80_000},
        ]
        # A TOC of an unrelated anthology that happens to mention "paris"
        toc_for = {1: ["paris", "casa", "hotel"]}
        backend, original = self._make_backend(books, toc_for)
        try:
            result = find_compilation_coverage(backend, author_id=1)
        finally:
            epub_tools.get_epub_toc = original

        compilation = next(c for c in result["compilations"] if c["book_id"] == 1)
        assert 2 not in compilation["covered_book_ids"], (
            f"short TOC entry wrongly matched long title "
            f"(coverage={compilation['covered_book_ids']})"
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


# ── Direct tests for the symmetric ratio helper (commit 73d9a36) ─────────

class TestTitleMatchesTocEntry:
    """Direct unit tests for _title_matches_toc_entry — the core matcher.

    The function is hoisted from a closure, so we can exercise it without
    spinning up a fake backend. Both directions of substring containment
    require ratio ≥ _MIN_COVERAGE_RATIO (0.75).
    """

    def test_exact_match(self):
        assert _title_matches_toc_entry("el chacal", "el chacal")

    def test_short_title_in_long_entry_rejected(self):
        # 9 / 18 = 0.50 < 0.75
        assert not _title_matches_toc_entry("el chacal", "el reto del chacal")

    def test_short_entry_in_long_title_rejected(self):
        # 5 / 18 = 0.28 < 0.75 — the "Steel/parís" regression case
        assert not _title_matches_toc_entry("una mujer en paris", "paris")

    def test_near_exact_match_accepted(self):
        # 17 / 18 ≈ 0.94 ≥ 0.75
        assert _title_matches_toc_entry(
            "crimen y castigo a", "crimen y castigo ab"
        )

    def test_disjoint_returns_false(self):
        assert not _title_matches_toc_entry("uno", "dos")


# ── TOC cache integration tests ─────────────────────────────────────────

class TestTocCache:
    """Cache invalidation must hold under the realistic failure modes."""

    def test_cache_hit_when_file_unchanged(self, tmp_path):
        from calibre_mcp.tools import _toc_cache

        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK\x03\x04" + b"x" * 5000)  # any non-empty bytes
        _toc_cache.store(
            str(tmp_path), book_id=1, epub_path=str(epub),
            toc=["Cap 1", "Cap 2"], toc_source="ncx",
            is_likely_scan=False, scan_signals=[],
        )
        result = _toc_cache.get_cached(str(tmp_path), 1, str(epub))
        assert result is not None
        assert result["toc"] == ["Cap 1", "Cap 2"]
        assert result["_from_cache"] is True

    def test_cache_invalidated_on_size_change(self, tmp_path):
        from calibre_mcp.tools import _toc_cache

        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK\x03\x04" + b"x" * 5000)
        _toc_cache.store(
            str(tmp_path), book_id=1, epub_path=str(epub),
            toc=["a"], toc_source="ncx", is_likely_scan=False, scan_signals=[],
        )
        # Change size — must invalidate
        epub.write_bytes(b"PK\x03\x04" + b"x" * 9999)
        assert _toc_cache.get_cached(str(tmp_path), 1, str(epub)) is None

    def test_cache_invalidated_on_content_change_same_size(self, tmp_path):
        """The byte-identical-resave case: same mtime, same size, different bytes.

        Only catchable via content fingerprint (head + tail hash).
        """
        from calibre_mcp.tools import _toc_cache
        import os

        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK\x03\x04" + b"a" * 9000)
        _toc_cache.store(
            str(tmp_path), book_id=1, epub_path=str(epub),
            toc=["original"], toc_source="ncx",
            is_likely_scan=False, scan_signals=[],
        )

        # Re-write with different content but exact same size and a forced
        # equal mtime (mimics FAT 2-second resolution).
        st = os.stat(str(epub))
        epub.write_bytes(b"PK\x03\x04" + b"b" * 9000)
        os.utime(str(epub), (st.st_atime, st.st_mtime))

        assert _toc_cache.get_cached(str(tmp_path), 1, str(epub)) is None, \
            "different content must invalidate even when mtime+size are equal"

    def test_cache_lives_in_hidden_subdir(self, tmp_path):
        """Cache file must NOT pollute the library root (visible to check_library)."""
        from calibre_mcp.tools import _toc_cache

        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK\x03\x04xxxxxx")
        _toc_cache.store(
            str(tmp_path), book_id=1, epub_path=str(epub),
            toc=["x"], toc_source="ncx", is_likely_scan=False, scan_signals=[],
        )
        # No cache file at root
        assert not (tmp_path / "_calibre_mcp_toc_cache.db").exists()
        # Subdir is created (hidden via leading dot)
        assert (tmp_path / ".calibre_mcp_cache" / "toc.db").exists()

    def test_legacy_cache_file_migrated(self, tmp_path):
        """Old cache file at root must be silently moved to the new location."""
        from calibre_mcp.tools import _toc_cache
        import sqlite3

        # Pre-create a cache file in the legacy location with the legacy schema
        legacy = tmp_path / "_calibre_mcp_toc_cache.db"
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            "CREATE TABLE toc_cache (book_id INTEGER PRIMARY KEY, x TEXT);"
        )
        conn.execute("INSERT INTO toc_cache VALUES (1, 'legacy')")
        conn.commit()
        conn.close()

        # Trigger a connect — should rename the file
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK\x03\x04xxxxxx")
        _toc_cache.store(
            str(tmp_path), book_id=2, epub_path=str(epub),
            toc=["a"], toc_source="ncx", is_likely_scan=False, scan_signals=[],
        )

        assert not legacy.exists(), "legacy cache file should have been moved"
        assert (tmp_path / ".calibre_mcp_cache" / "toc.db").exists()


# ── suggest_dedup_resolution: tie-break stability (#6) ───────────────────

class TestDedupTieBreak:
    """When all quality axes are equal, must pick the lowest book id."""

    def _backend(self, books):
        def query_db(sql, params):
            return [books.get(int(params[0]))] if int(params[0]) in books else []
        return SimpleNamespace(query_db=query_db)

    def test_ties_pick_lowest_id_regardless_of_input_order(self):
        # Two identically-scored books — input order [42, 5, 17] must NOT
        # decide; we must pick id=5.
        common = {"format": "EPUB", "uncompressed_size": 100,
                  "has_cover": 0, "has_comments": 0, "last_modified": ""}
        books = {
            42: {"id": 42, "title": "X", **common},
            5:  {"id": 5,  "title": "X", **common},
            17: {"id": 17, "title": "X", **common},
        }
        backend = self._backend(books)
        result = suggest_dedup_resolution(backend, [42, 5, 17])
        assert result["keep"]["id"] == 5


# ── Confidence levels (#3 dedup divergence resolution) ──────────────────

class TestTitleMatchKind:
    """The matcher distinguishes exact (safe to delete) from partial (review)."""

    def test_exact_after_normalization(self):
        # "El Chacal" and "el chacal" both normalize to "chacal" → exact
        assert _title_match_kind("chacal", "chacal") == "exact"

    def test_substring_partial(self):
        # 18-char title contains 5-char entry but ratio works → partial
        # entry='crimen y castigo' (16) in tnorm='crimen y castigo a' (18) = 16/18 = 0.89
        assert _title_match_kind("crimen y castigo a", "crimen y castigo") == "partial"

    def test_no_match(self):
        assert _title_match_kind("uno", "dos") is None

    def test_short_title_in_long_entry_below_ratio(self):
        # The Steel-paris regression — short entry, long title, ratio fails
        assert _title_match_kind("una mujer en paris", "paris") is None


class TestCoverageConfidenceSplit:
    """find_compilation_coverage must split exact and partial matches into separate buckets."""

    def _make_backend(self, books_rows, toc_for):
        from calibre_mcp.tools import epub as epub_tools

        def query_db(sql, params=None):
            return list(books_rows)

        def fake_get_toc(backend, bid, **kw):
            return {
                "status": "success",
                "is_likely_scan": False,
                "toc": toc_for.get(int(bid), []),
                "toc_count": len(toc_for.get(int(bid), [])),
                "size_mb": 1.0,
            }

        original = epub_tools.get_epub_toc
        epub_tools.get_epub_toc = fake_get_toc
        backend = SimpleNamespace(query_db=query_db, library="/lib")
        return backend, original

    def test_exact_match_lands_in_exact_bucket(self):
        from calibre_mcp.tools import epub as epub_tools
        from calibre_mcp.tools.analyze import find_compilation_coverage

        books = [
            {"id": 1, "title": "Antología", "format": "EPUB", "uncompressed_size": 5_000_000},
            {"id": 2, "title": "El Chacal", "format": "EPUB", "uncompressed_size": 50_000},
        ]
        toc_for = {1: ["El Chacal"]}
        backend, original = self._make_backend(books, toc_for)
        try:
            result = find_compilation_coverage(backend, author_id=1)
        finally:
            epub_tools.get_epub_toc = original

        comp = next(c for c in result["compilations"] if c["book_id"] == 1)
        assert 2 in comp["covered_exact_ids"]
        assert 2 not in comp["covered_partial_ids"]
        assert result["summary"]["covered_exact_count"] == 1
        assert result["summary"]["covered_partial_only_count"] == 0

    def test_partial_match_lands_in_partial_bucket(self):
        from calibre_mcp.tools import epub as epub_tools
        from calibre_mcp.tools.analyze import find_compilation_coverage

        # Title has subtitle/extra text vs the bare TOC entry — ratio passes
        # but it's not exact, so it must go to partial.
        books = [
            {"id": 1, "title": "Antología", "format": "EPUB", "uncompressed_size": 5_000_000},
            {"id": 2, "title": "Don Quijote", "format": "EPUB", "uncompressed_size": 50_000},
        ]
        # entry "Don Quijote, primera parte" — partial substring of title? Actually
        # tnorm='don quijote' (11), entry='don quijote primera parte' (25). 11/25 = 0.44 → no.
        # Reverse direction also no. Need to construct a real partial match:
        # entry "Don Quijote anotado" (19), tnorm='don quijote' (11). 11/19 = 0.58 → no.
        # entry "Don Quijoter" (12), tnorm='don quijote' (11): 11/12 = 0.92 → partial.
        toc_for = {1: ["Don Quijoter"]}
        backend, original = self._make_backend(books, toc_for)
        try:
            result = find_compilation_coverage(backend, author_id=1)
        finally:
            epub_tools.get_epub_toc = original

        comp = next(c for c in result["compilations"] if c["book_id"] == 1)
        assert 2 in comp["covered_partial_ids"]
        assert 2 not in comp["covered_exact_ids"]
        assert result["summary"]["covered_partial_only_count"] == 1

    def test_book_with_both_exact_and_partial_counts_as_exact_only(self):
        """If the same book matches one TOC entry exactly and another partially,
        it must count as 'exact' (safe), not as partial-only-needs-review."""
        from calibre_mcp.tools import epub as epub_tools
        from calibre_mcp.tools.analyze import find_compilation_coverage

        books = [
            {"id": 1, "title": "Antología", "format": "EPUB", "uncompressed_size": 5_000_000},
            {"id": 2, "title": "El Chacal", "format": "EPUB", "uncompressed_size": 50_000},
        ]
        toc_for = {1: ["El Chacal", "el chacalero"]}  # exact + partial
        backend, original = self._make_backend(books, toc_for)
        try:
            result = find_compilation_coverage(backend, author_id=1)
        finally:
            epub_tools.get_epub_toc = original

        comp = next(c for c in result["compilations"] if c["book_id"] == 1)
        assert 2 in comp["covered_exact_ids"]
        assert result["summary"]["covered_partial_only_count"] == 0
