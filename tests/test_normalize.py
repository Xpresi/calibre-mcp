"""Tests for normalization utilities — these run without a Calibre library."""

import pytest
from calibre_mcp.utils.normalize import (
    normalize_for_grouping,
    initials_compatible,
    parse_author_tokens,
    strip_accents,
)
from calibre_mcp.utils.titlecase import smart_title_case, is_all_uppercase


class TestNormalizeForGrouping:
    def test_same_name_different_order(self):
        assert normalize_for_grouping("Stephen King") == normalize_for_grouping("King, Stephen")

    def test_case_insensitive(self):
        assert normalize_for_grouping("STEPHEN KING") == normalize_for_grouping("stephen king")

    def test_accent_insensitive(self):
        assert normalize_for_grouping("García Márquez") == normalize_for_grouping("Garcia Marquez")

    def test_pipe_format(self):
        assert normalize_for_grouping("King| Stephen") == normalize_for_grouping("King, Stephen")

    def test_punctuation_ignored(self):
        assert normalize_for_grouping("O'Brien") == normalize_for_grouping("OBrien")


class TestInitialsCompatible:
    def test_full_match(self):
        assert initials_compatible(["herbert", "george"], ["herbert", "george"])

    def test_initial_vs_full(self):
        assert initials_compatible(["h.", "g."], ["herbert", "george"])

    def test_initial_vs_full_reversed(self):
        assert initials_compatible(["herbert", "george"], ["h.", "g."])

    def test_partial_initial(self):
        assert initials_compatible(["j.", "r.", "r."], ["john", "ronald", "reuel"])

    def test_different_names(self):
        assert not initials_compatible(["h.", "g."], ["john", "george"])

    def test_different_lengths(self):
        assert not initials_compatible(["h."], ["herbert", "george"])


class TestStripAccents:
    def test_spanish_accents(self):
        assert strip_accents("García") == "Garcia"
        assert strip_accents("señor") == "senor"

    def test_no_accents(self):
        assert strip_accents("Hello") == "Hello"


class TestSmartTitleCase:
    def test_all_caps(self):
        result = smart_title_case("EL SEÑOR DE LOS ANILLOS")
        assert result == "El Señor de los Anillos"

    def test_preserves_roman_numerals(self):
        result = smart_title_case("CAPITULO III")
        assert "III" in result

    def test_preserves_acronyms(self):
        result = smart_title_case("EL FBI Y LA CIA")
        assert "FBI" in result
        assert "CIA" in result

    def test_empty_string(self):
        assert smart_title_case("") == ""


class TestIsAllUppercase:
    def test_uppercase(self):
        assert is_all_uppercase("HELLO WORLD")

    def test_mixed(self):
        assert not is_all_uppercase("Hello World")

    def test_single_char(self):
        assert not is_all_uppercase("A")  # Too short to be meaningful
