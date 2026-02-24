"""Filename sanitization utilities."""

import unicodedata


def sanitize_title(title: str, max_len: int = 80) -> str:
    """Clean up a title string for safe filesystem use.

    Removes characters that are illegal in file names on Windows/macOS/Linux,
    normalizes Unicode to NFC form, and truncates to max_len.
    """
    title = unicodedata.normalize("NFC", title)
    safe = "".join(c for c in title if c not in '/\\:*?"<>|').strip()
    return safe[:max_len]
