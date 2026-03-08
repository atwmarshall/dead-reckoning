"""Shared utility functions."""

import hashlib
import re
from typing import Any


def slugify(text: str) -> str:
    """Convert a string to a URL-friendly slug.

    Lowercases the text, replaces spaces with hyphens, and strips
    any characters that are not alphanumeric or hyphens.
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text


def hash_password(password: str, salt: str = "") -> str:
    """Return the SHA-256 hex digest of password+salt."""
    payload = (password + salt).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def paginate(items: list[Any], page: int, page_size: int = 20) -> list[Any]:
    """Return a single page of items from a list."""
    start = (page - 1) * page_size
    return items[start : start + page_size]


def flatten(nested: list[list[Any]]) -> list[Any]:
    """Flatten one level of nesting."""
    return [item for sublist in nested for item in sublist]
