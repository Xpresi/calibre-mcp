"""Tests for get_epub_toc — uses synthetic in-memory EPUBs and a stub backend."""

import os
import zipfile
from pathlib import Path

import pytest

from calibre_mcp.tools.epub import (
    _is_generic_toc_entry,
    _detect_scan_signals,
    _extract_toc_entries,
    get_epub_toc,
)


# ── Helpers ────────────────────────────────────────────────────────────


class StubBackend:
    """Minimal backend mock for offline tests."""

    def __init__(self, library, rows_by_book, exists_set=None):
        self.library = str(library)
        self._rows = rows_by_book        # {book_id: row dict or None}
        self._exists = exists_set or set(rows_by_book.keys())

    def query_db(self, sql, params=None):
        bid = int(params[0])
        row = self._rows.get(bid)
        return [row] if row else []

    def query_db_scalar(self, sql, params=None):
        bid = int(params[0])
        return 1 if bid in self._exists else None


def _build_epub(path: Path, ncx_titles=None, nav_titles=None, padding_bytes=0):
    """Write a minimal EPUB-like zip with an optional toc.ncx or nav.xhtml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        if ncx_titles is not None:
            entries = "\n".join(
                f'<navPoint><navLabel><text>{t}</text></navLabel><content src="x.xhtml"/></navPoint>'
                for t in ncx_titles
            )
            z.writestr(
                "OEBPS/toc.ncx",
                f'<?xml version="1.0"?><ncx><navMap>{entries}</navMap></ncx>',
            )
        if nav_titles is not None:
            entries = "\n".join(f'<li><a href="x.xhtml">{t}</a></li>' for t in nav_titles)
            z.writestr(
                "OEBPS/nav.xhtml",
                f'<html><body><nav><ol>{entries}</ol></nav></body></html>',
            )
        if padding_bytes > 0:
            z.writestr("OEBPS/padding.bin", b"\x00" * padding_bytes)


# ── Unit tests for helpers ─────────────────────────────────────────────


class TestGenericTocDetection:
    @pytest.mark.parametrize("entry", [
        "WS-0001", "WS - 0042", "Page-001", "page 12",
        "IMG-0099", "Scan-130514-0002", "0001-0002",
        "PáginA-12", "pag 003",
    ])
    def test_generic_entries_detected(self, entry):
        assert _is_generic_toc_entry(entry)

    @pytest.mark.parametrize("entry", [
        "Romeo y Julieta", "Capítulo 1: El comienzo",
        "Hamlet", "El curioso impertinente",
        "Prólogo", "Sonetos",
    ])
    def test_real_titles_not_flagged(self, entry):
        assert not _is_generic_toc_entry(entry)


class TestScanSignals:
    def test_size_signal(self):
        signals = _detect_scan_signals("X", 196.0, ["Real chapter"])
        assert any("size>50MB" in s for s in signals)

    def test_toc_pattern_signal(self):
        toc = [f"WS-{i:04d}" for i in range(20)]
        signals = _detect_scan_signals("X", 1.0, toc)
        assert any("toc_pattern:generic" in s for s in signals)

    def test_title_marker_signal(self):
        signals = _detect_scan_signals(
            "Obras Completas {Ed. Aguilar} [por Ganz1912]", 1.0, ["Real"]
        )
        assert any("title:scan_marker" in s for s in signals)

    def test_clean_book_no_signals(self):
        signals = _detect_scan_signals(
            "Cuentos completos", 1.5,
            ["Prólogo", "El Aleph", "El Sur", "Funes el memorioso"],
        )
        assert signals == []


# ── Integration tests with synthetic EPUBs ─────────────────────────────


class TestExtractTocEntries:
    def test_extracts_from_ncx(self, tmp_path):
        epub = tmp_path / "book.epub"
        _build_epub(epub, ncx_titles=["Capítulo 1", "Capítulo 2", "Epílogo"])
        entries, source = _extract_toc_entries(str(epub))
        assert source == "ncx"
        assert entries == ["Capítulo 1", "Capítulo 2", "Epílogo"]

    def test_falls_back_to_nav(self, tmp_path):
        epub = tmp_path / "book.epub"
        _build_epub(epub, nav_titles=["Intro", "Acto I", "Acto II"])
        entries, source = _extract_toc_entries(str(epub))
        assert source == "nav"
        assert entries == ["Intro", "Acto I", "Acto II"]

    def test_decodes_html_entities(self, tmp_path):
        epub = tmp_path / "book.epub"
        _build_epub(epub, ncx_titles=["Romeo &amp; Julieta", "Marlowe&#39;s play"])
        entries, _ = _extract_toc_entries(str(epub))
        assert entries == ["Romeo & Julieta", "Marlowe's play"]

    def test_no_toc_returns_empty(self, tmp_path):
        epub = tmp_path / "book.epub"
        _build_epub(epub)  # no ncx, no nav
        entries, source = _extract_toc_entries(str(epub))
        assert source == "none"
        assert entries == []

    def test_corrupt_epub(self, tmp_path):
        bad = tmp_path / "bad.epub"
        bad.write_bytes(b"not a zip")
        entries, source = _extract_toc_entries(str(bad))
        assert source == "none"
        assert entries == []


# ── End-to-end with stub backend ───────────────────────────────────────


class TestGetEpubToc:
    def test_real_text_epub(self, tmp_path):
        rel = "Borges, Jorge Luis/Cuentos completos (1)"
        epub_path = tmp_path / rel / "Cuentos completos.epub"
        _build_epub(
            epub_path,
            ncx_titles=["Prólogo", "El Aleph", "El Sur", "Funes el memorioso"],
        )
        backend = StubBackend(tmp_path, {
            1: {
                "id": 1, "title": "Cuentos completos", "path": rel,
                "name": "Cuentos completos", "format": "EPUB",
                "uncompressed_size": 1_500_000,
            }
        })
        result = get_epub_toc(backend, 1)
        assert result["status"] == "success"
        assert result["toc_count"] == 4
        assert result["toc_source"] == "ncx"
        assert result["is_likely_scan"] is False
        assert result["scan_signals"] == []
        assert result["size_mb"] < 2

    def test_scan_epub_detected(self, tmp_path):
        rel = "ganz1912/Obras Compl (2)"
        epub_path = tmp_path / rel / "Obras.epub"
        scan_titles = [f"WS-{i:04d}" for i in range(50)]
        # 60 MB padding to trigger size signal
        _build_epub(epub_path, ncx_titles=scan_titles, padding_bytes=60 * 1024 * 1024)
        backend = StubBackend(tmp_path, {
            2: {
                "id": 2,
                "title": "SHAKESPEARE - Obras Completas [por Ganz1912]",
                "path": rel, "name": "Obras", "format": "EPUB",
                "uncompressed_size": 60 * 1024 * 1024 + 1000,
            }
        })
        result = get_epub_toc(backend, 2)
        assert result["status"] == "success"
        assert result["is_likely_scan"] is True
        # All three signals should fire
        joined = " ".join(result["scan_signals"])
        assert "size>50MB" in joined
        assert "toc_pattern:generic" in joined
        assert "title:scan_marker" in joined

    def test_book_does_not_exist(self, tmp_path):
        backend = StubBackend(tmp_path, {}, exists_set=set())
        result = get_epub_toc(backend, 999)
        assert result["status"] == "error"
        assert "does not exist" in result["message"]

    def test_book_without_epub(self, tmp_path):
        backend = StubBackend(tmp_path, {}, exists_set={3})  # exists, no EPUB row
        result = get_epub_toc(backend, 3)
        assert result["status"] == "error"
        assert "no EPUB" in result["message"]

    def test_epub_missing_on_disk(self, tmp_path):
        backend = StubBackend(tmp_path, {
            4: {
                "id": 4, "title": "Lost", "path": "missing/dir",
                "name": "lost", "format": "EPUB", "uncompressed_size": 1000,
            }
        })
        result = get_epub_toc(backend, 4)
        assert result["status"] == "error"
        assert "not found on disk" in result["message"]

    def test_nav_fallback(self, tmp_path):
        rel = "Author/Book (5)"
        epub_path = tmp_path / rel / "book.epub"
        _build_epub(epub_path, nav_titles=["Chapter 1", "Chapter 2"])
        backend = StubBackend(tmp_path, {
            5: {
                "id": 5, "title": "Book", "path": rel,
                "name": "book", "format": "EPUB", "uncompressed_size": 500_000,
            }
        })
        result = get_epub_toc(backend, 5)
        assert result["status"] == "success"
        assert result["toc_source"] == "nav"
        assert result["toc"] == ["Chapter 1", "Chapter 2"]

    def test_truncates_huge_toc(self, tmp_path):
        rel = "Author/Big (6)"
        epub_path = tmp_path / rel / "big.epub"
        titles = [f"Capítulo {i}" for i in range(800)]
        _build_epub(epub_path, ncx_titles=titles)
        backend = StubBackend(tmp_path, {
            6: {
                "id": 6, "title": "Big", "path": rel,
                "name": "big", "format": "EPUB", "uncompressed_size": 5_000_000,
            }
        })
        result = get_epub_toc(backend, 6)
        assert result["toc_truncated"] is True
        assert len(result["toc"]) == 500
        assert result["toc_count"] == 500
