"""Pydantic data models used across collector, filter and dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


class Keyword(BaseModel):
    suchbegriff: str
    markt: List[str] = Field(default_factory=list)
    themenbereich: List[str] = Field(default_factory=list)
    prioritaet: Optional[str] = None
    sprache: Optional[str] = None
    notizen: Optional[str] = None


class FeedSource(BaseModel):
    rss_feed: str
    typ: Optional[str] = None  # "RSS-Feed" | "Google News Query" | "Website-Scrape"
    markt: List[str] = Field(default_factory=list)
    themenbereich: List[str] = Field(default_factory=list)
    prioritaet: Optional[str] = None
    sprache: Optional[str] = None
    notizen: Optional[str] = None


class Outlet(BaseModel):
    outlet: str
    url: Optional[str] = None
    markt: List[str] = Field(default_factory=list)
    themenbereich: List[str] = Field(default_factory=list)
    prioritaet: Optional[str] = None
    sprache: Optional[str] = None
    outlet_typ: Optional[str] = None
    hat_rss: bool = False
    notizen: Optional[str] = None


class NegativeKeyword(BaseModel):
    begriff: str
    markt: List[str] = Field(default_factory=list)
    kategorie: Optional[str] = None
    notizen: Optional[str] = None


class NotionConfig(BaseModel):
    keywords: List[Keyword] = Field(default_factory=list)
    feeds: List[FeedSource] = Field(default_factory=list)
    outlets: List[Outlet] = Field(default_factory=list)
    negative_keywords: List[NegativeKeyword] = Field(default_factory=list)
    loaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AIScore(BaseModel):
    relevance_score: int = Field(ge=0, le=10)
    category: str
    themes: List[str] = Field(default_factory=list)
    reasoning: str = ""
    # v0.2.0 F2: Claude may propose keyword phrases (0-3, lowercase) that
    # would have surfaced this article had they already been in the
    # high-priority keyword list. Empty list when the model didn't propose
    # anything or the SUGGEST_KEYWORDS env flag is off (prompt keeps the
    # field out of the schema in that case, so the model won't emit it).
    suggested_keywords: List[str] = Field(default_factory=list)
