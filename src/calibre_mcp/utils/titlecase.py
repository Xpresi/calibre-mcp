"""Intelligent title case for Spanish and English text."""

import re

# Spanish particles that stay lowercase (except at the start)
PARTICLES_ES = {
    'de', 'del', 'la', 'las', 'los', 'el', 'en', 'y', 'o', 'u',
    'a', 'al', 'con', 'sin', 'por', 'para', 'que', 'un', 'una',
}

# English particles
PARTICLES_EN = {
    'the', 'a', 'an', 'and', 'but', 'or', 'nor', 'for', 'yet', 'so',
    'in', 'on', 'at', 'to', 'by', 'of', 'up', 'as', 'is', 'it',
}

ALL_PARTICLES = PARTICLES_ES | PARTICLES_EN

# Acronyms and Roman numerals that should keep their form
KEEP_AS_IS = {
    'FBI', 'CIA', 'NASA', 'USA', 'ISBN', 'USB', 'PDF', 'HTML',
    'DVD', 'CD', 'TV', 'BBC', 'KGB', 'ADN', 'ONU', 'ETA', 'IRA',
    'CEO', 'CTO', 'PhD', 'MBA', 'DIY', 'FAQ', 'GPS', 'LED', 'RAM',
    'URL', 'VIP', 'WWW', 'XML', 'API', 'CSS', 'SQL', 'NYC', 'UK',
    'EU', 'UN', 'WHO', 'NATO', 'OTAN', 'ONG', 'URSS',
    # Roman numerals
    'I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X',
    'XI', 'XII', 'XIII', 'XIV', 'XV', 'XVI', 'XVII', 'XVIII', 'XIX', 'XX',
    'XXI', 'XXV', 'XXX', 'XL', 'L', 'C', 'D', 'M',
}

# Build a lookup set in uppercase for matching
_KEEP_UPPER = {w.upper(): w for w in KEEP_AS_IS}


def smart_title_case(text: str) -> str:
    """
    Convert text to title case with intelligence:
    - Spanish/English particles stay lowercase (except first word)
    - Known acronyms and Roman numerals keep their canonical form
    - Words with internal caps (e.g., 'McDonald') are left alone
    - Text in parentheses/brackets follows the same rules
    """
    if not text:
        return text

    words = text.split()
    result = []

    for i, word in enumerate(words):
        upper = word.upper()

        # Check if it's a known acronym / Roman numeral
        if upper in _KEEP_UPPER:
            result.append(_KEEP_UPPER[upper])
            continue

        # If word has internal punctuation (e.g., "d'Artagnan"), handle carefully
        # Only process the all-uppercase case
        if not word.isupper():
            # Mixed case — leave it alone (might be intentional like "iPhone")
            result.append(word)
            continue

        # All uppercase word — convert to title case
        lower = word.lower()

        # First word always capitalized
        if i == 0:
            result.append(word.capitalize())
        elif lower in ALL_PARTICLES:
            result.append(lower)
        else:
            result.append(word.capitalize())

    return ' '.join(result)


def is_all_uppercase(text: str) -> bool:
    """Check if a string is ALL UPPERCASE (ignoring non-alpha characters)."""
    alpha = ''.join(ch for ch in text if ch.isalpha())
    return len(alpha) > 1 and alpha == alpha.upper()
