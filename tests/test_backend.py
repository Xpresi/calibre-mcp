"""Integration tests for the Calibre backend — these run against the real library."""

import pytest

from calibre_mcp.config import Config
from calibre_mcp.backend import CalibreBackend
from calibre_mcp.tools.report import generate_report, compare_snapshots


@pytest.fixture
def backend():
    cfg = Config.load()
    b = CalibreBackend(cfg)
    # Skip integration tests if the library isn't accessible
    if not b.db_path.exists():
        pytest.skip("Calibre library not accessible — skipping integration tests")
    return b


def test_library_stats_keys(backend):
    """get_library_stats must expose the keys generate_report/compare_snapshots rely on."""
    stats = backend.get_library_stats()
    for key in (
        "total_books", "total_authors", "total_series", "total_tags",
        "total_publishers", "books_without_cover", "books_without_data_records",
        "books_without_tags", "books_without_language", "database_size_mb",
        "formats", "top_languages", "library_path",
    ):
        assert key in stats, f"missing stats key: {key}"


def test_generate_report_does_not_crash(backend):
    """Regression: generate_report referenced a non-existent 'books_without_files'
    key and raised KeyError on every call. Must return a non-empty markdown string."""
    report = generate_report(backend)
    assert isinstance(report, str)
    assert "# Calibre Library Report" in report
    assert "Books without cover" in report


def test_compare_snapshots_self_is_noop(backend):
    """Comparing a snapshot to itself must report no changes (and not KeyError)."""
    snap = backend.get_library_stats()
    diff = compare_snapshots(backend, snap)
    assert diff["differences"] == {}
    assert "No changes" in diff["summary"]
