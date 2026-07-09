"""Centralised enum normalisation for priority / themes / outlet types / categories.

Background: the legacy DACH config carries German enum values ("Hoch", "Mittel",
"Niedrig", "Behörde", "Ministerium", …). The Market-Edition UI is English, so
new entries created via the admin pages will use English values ("High",
"Medium", "Low", …).

To keep both worlds working without a forced migration, every consumer of these
fields normalises through the helpers below.

Display labels (what the UI shows) live in MAP_*_DISPLAY. Storage is canonical
English unless a record was imported from the legacy seed.
"""

from __future__ import annotations

from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

PRIORITY_HIGH = "High"
PRIORITY_MEDIUM = "Medium"
PRIORITY_LOW = "Low"

PRIORITY_OPTIONS = [PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW]

_PRIORITY_ALIASES: Dict[str, str] = {
    "high": PRIORITY_HIGH, "hoch": PRIORITY_HIGH,
    "medium": PRIORITY_MEDIUM, "mittel": PRIORITY_MEDIUM, "med": PRIORITY_MEDIUM,
    "low": PRIORITY_LOW, "niedrig": PRIORITY_LOW,
}


def normalise_priority(value: Optional[str]) -> Optional[str]:
    """Map any legacy German or new English priority to the canonical English form."""
    if not value:
        return None
    return _PRIORITY_ALIASES.get(value.strip().lower(), value)


def is_high_priority(value: Optional[str]) -> bool:
    return normalise_priority(value) == PRIORITY_HIGH


def is_low_priority(value: Optional[str]) -> bool:
    return normalise_priority(value) == PRIORITY_LOW


# ---------------------------------------------------------------------------
# Outlet types
# ---------------------------------------------------------------------------

OUTLET_TYPE_OPTIONS = [
    "Trade press",
    "Daily press",
    "Authority",
    "Ministry",
    "EU institution",
    "Funding bank",
    "Association",
    "Other",
]

_OUTLET_TYPE_ALIASES: Dict[str, str] = {
    "fachpresse": "Trade press",
    "tagespresse": "Daily press",
    "behörde": "Authority", "behoerde": "Authority",
    "ministerium": "Ministry",
    "eu-institution": "EU institution", "eu institution": "EU institution",
    "förderbank": "Funding bank", "foerderbank": "Funding bank",
    "verband": "Association",
}


def normalise_outlet_type(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _OUTLET_TYPE_ALIASES.get(value.strip().lower(), value)


# ---------------------------------------------------------------------------
# Negative keyword categories
# ---------------------------------------------------------------------------

NEGATIVE_CATEGORY_OPTIONS = [
    "Off-topic",
    "Job posting",
    "Advertisement",
    "Other",
]

_NEGATIVE_CATEGORY_ALIASES: Dict[str, str] = {
    "off-topic": "Off-topic",
    "stellenanzeige": "Job posting",
    "job posting": "Job posting",
    "werbung": "Advertisement",
    "advertisement": "Advertisement",
}


def normalise_negative_category(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _NEGATIVE_CATEGORY_ALIASES.get(value.strip().lower(), value)


# ---------------------------------------------------------------------------
# Feed types
# ---------------------------------------------------------------------------

FEED_TYPE_OPTIONS = ["RSS-Feed", "Google News Query", "Website-Scrape"]


# ---------------------------------------------------------------------------
# Languages (ISO 639-1, uppercased)
# ---------------------------------------------------------------------------

LANGUAGE_OPTIONS = ["DE", "EN", "FR", "IT", "NL", "PT", "SV", "NO", "DA", "FI"]
