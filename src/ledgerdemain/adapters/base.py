"""Shared adapter normalization helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

_WHITESPACE_RE = re.compile(r"\s+")
_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T\s].*)?$")
_COMPACT_DATE_RE = re.compile(r"^(\d{8})(?:\d{6}.*)?$")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %Y",
    "%d %B %Y",
)


def normalize_description(text: str) -> str:
    """Normalize transaction descriptions for stable deduplication and matching."""
    return _WHITESPACE_RE.sub(" ", text).strip().upper()


def parse_flexible_date(val: str) -> str:
    """Parse common date formats into ISO YYYY-MM-DD."""
    cleaned = _WHITESPACE_RE.sub(" ", val).strip()
    if not cleaned:
        raise ValueError("Empty date string")

    iso_match = _ISO_DATE_PREFIX_RE.match(cleaned)
    if iso_match:
        datetime.strptime(iso_match.group(1), "%Y-%m-%d")
        return iso_match.group(1)

    compact_match = _COMPACT_DATE_RE.match(cleaned)
    if compact_match:
        dt = datetime.strptime(compact_match.group(1), "%Y%m%d")
        return dt.strftime("%Y-%m-%d")

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(f"Unrecognized date format: {val!r}")


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
