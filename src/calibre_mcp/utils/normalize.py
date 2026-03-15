"""Normalization utilities for author names, titles, and other metadata."""

import re
import unicodedata


def normalize_for_grouping(name: str) -> str:
    """
    Normalize a name for duplicate detection.
    Strips accents, punctuation, lowercases, and sorts tokens alphabetically.
    'Stephen King' and 'King, Stephen' produce the same key.
    """
    s = name.lower().strip()
    # Remove quotes, brackets, parens
    s = re.sub(r'[«»"\'\(\)\[\]]+', ' ', s)
    # Decompose unicode and strip combining marks (accents)
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    # Remove remaining punctuation
    s = re.sub(r'[^\w\s]', ' ', s)
    # Sort tokens
    tokens = sorted(s.split())
    return ' '.join(tokens)


def initials_compatible(tokens1: list[str], tokens2: list[str]) -> bool:
    """
    Check if two token lists represent the same name with initials.
    'H. G.' is compatible with 'Herbert George'.
    'J. R. R.' is compatible with 'John Ronald Reuel'.
    """
    if len(tokens1) != len(tokens2):
        return False
    for a, b in zip(tokens1, tokens2):
        ac = a.rstrip('.')
        bc = b.rstrip('.')
        if ac.lower() == bc.lower():
            continue
        if len(ac) == 1 and bc.lower().startswith(ac.lower()):
            continue
        if len(bc) == 1 and ac.lower().startswith(bc.lower()):
            continue
        return False
    return True


def strip_accents(s: str) -> str:
    """Remove accents/diacritics from a string."""
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(ch for ch in nfkd if not unicodedata.combining(ch))


def parse_author_tokens(name: str) -> list[str]:
    """
    Parse an author name into tokens, handling 'Last, First' and 'Last| First' formats.
    Returns lowercase tokens without punctuation.
    """
    # Handle Calibre's old pipe format
    name = name.replace('|', ',')
    # Split on comma to separate "Last, First Middle"
    parts = [p.strip() for p in name.split(',')]
    # Flatten and tokenize
    tokens = []
    for part in parts:
        tokens.extend(part.lower().split())
    return [re.sub(r'[^\w]', '', t) for t in tokens if t]
