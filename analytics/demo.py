"""
Shared constants and a tiny helper for the bundled "Demo" account.

Seeded by ``scripts/seed_demo_account.py``; identified everywhere downstream
by its slug so the UI can render a "not real" banner and the dashboard can
fall back to safe behaviour when a visitor pokes at it.

Public deployments use this to give first-time visitors a working preview
without exposing any real portfolio data.
"""
from __future__ import annotations

from analytics.auth import slugify

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo1234"
DEMO_SLUG = slugify(DEMO_EMAIL)


def is_demo_slug(slug: str | None) -> bool:
    return slug == DEMO_SLUG


__all__ = ["DEMO_EMAIL", "DEMO_PASSWORD", "DEMO_SLUG", "is_demo_slug"]
