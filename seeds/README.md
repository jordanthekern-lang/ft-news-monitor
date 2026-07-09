# Market Seed Packs

Shipping-time JSON files that seed the four config tables (keywords, feeds,
outlets, negative_keywords) when a user first runs the tool. The setup wizard
imports one seed per market the user selects.

## Layout

- One file per market, lowercase filename: `dach.json`, `eu.json`, `italy.json`,
  `france.json`, `benelux.json`, `portugal.json`, `nordics.json`.
- Only `dach.json` and `eu.json` ship today (extracted from the legacy Notion
  DACH+EU export). The other five come from research work later.
- Files whose name starts with `_` are holding-pens — not imported at runtime,
  skipped by `scripts/validate_seeds.py`'s default `seeds/*.json` glob. Today
  the only one is `_pending_research.json`: legacy Notion export rows tagged
  for non-DACH/non-EU markets. Frozen for T8/T9 (Italy/France/Benelux/
  Portugal/Nordics seed research) to consume as prior art. Not imported at
  runtime. Enum values still German — normalise when adopting into a market
  seed file.

## File schema

```jsonc
{
  "market": "DACH",         // display name; informational only
  "seed_version": 1,        // reserved for future incompatible bumps
  "keywords":          [ ... ],
  "feeds":             [ ... ],
  "outlets":           [ ... ],
  "negative_keywords": [ ... ]
}
```

The importer ignores unknown top-level keys, so custom pipelines / annotations
are safe to add.

### Rows (all four arrays)

The row-level German KEY names below are intentional — the underlying SQLite
column names are still German (`suchbegriff`, `markt`, …) from the original
Notion schema; the migration to English keys is out of scope. Enum VALUES,
however, are canonical English (see `app/config.py` and `app/enums.py`).

#### `keywords`

| Field | Type | Notes |
|-------|------|-------|
| `suchbegriff` | string, required | Search term. Supports `+`-combos (e.g. `E-Bus + Förderung`) — every `+`-separated segment must contain at least one significant token (>= 4 chars). |
| `markt` | string[] | Subset of `VALID_MARKETS` (`DACH`, `Benelux`, `France`, `Italy`, `Nordics`, `Portugal`, `EU`) or the sentinel `"All"`. Empty list also means "applies to all markets". |
| `themenbereich` | string[] | Subset of `VALID_THEMES` from `app/config.py`. |
| `prioritaet` | string | One of `High` / `Medium` / `Low`. |
| `sprache` | string | ISO-639-1 language code, uppercase (`DE`, `EN`, `FR`, `IT`, `NL`, `PT`, `SV`, `NO`, `DA`, `FI`). |
| `notizen` | string | Free text, optional. |

#### `feeds`

| Field | Type | Notes |
|-------|------|-------|
| `rss_feed` | string, required | RSS URL, Google News query, or website URL — depending on `typ`. |
| `typ` | string | One of `RSS-Feed`, `Google News Query`, `Website-Scrape` (`FEED_TYPE_OPTIONS`). |
| `markt`, `themenbereich`, `prioritaet`, `sprache`, `notizen` | see above | Same rules as keywords. |

#### `outlets`

| Field | Type | Notes |
|-------|------|-------|
| `outlet` | string, required | Display name of the outlet. |
| `url` | string | Homepage URL. |
| `markt`, `themenbereich`, `prioritaet`, `sprache` | see above | |
| `outlet_typ` | string | One of `OUTLET_TYPE_OPTIONS` (`Trade press`, `Daily press`, `Authority`, `Ministry`, `EU institution`, `Funding bank`, `Association`, `Other`). |
| `hat_rss` | bool | Whether the outlet publishes an RSS feed. |
| `notizen` | string | Free text. |

#### `negative_keywords`

| Field | Type | Notes |
|-------|------|-------|
| `begriff` | string, required | Excluded term. |
| `markt` | string[] | See above. |
| `kategorie` | string | One of `NEGATIVE_CATEGORY_OPTIONS` (`Off-topic`, `Job posting`, `Advertisement`, `Other`). |
| `notizen` | string | Free text. |

## Import semantics (natural keys, merge behaviour)

Merge import (`import_seed_file(path, mode="merge")`, the wizard default) is
idempotent — running it twice with the same seed produces zero writes on the
second run, and user-edited rows are never touched. Natural keys per table:

| Table | Natural key |
|-------|-------------|
| `keywords` | `(lower(suchbegriff), sprache)` |
| `feeds` | `rss_feed` |
| `outlets` | `url` (falls back to `outlet` name if `url` is empty) |
| `negative_keywords` | `lower(begriff)` |

`reset` mode wipes each table before inserting (dev-only convenience — do not
use in the shipped wizard).

## Validating a seed pack

```
python scripts/validate_seeds.py                # all seeds/*.json, offline
python scripts/validate_seeds.py --network       # additionally fetch RSS URLs
```

The offline validator is exercised by the test-suite and must exit 0 for every
file in this directory.
