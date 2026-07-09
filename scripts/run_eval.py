"""CLI: run the golden-set evaluation and dump a JSON artefact.

Feature 1 (v0.2.0). Reads the ``golden_labels`` table, re-scores each
labeled article via ``app.eval.run_eval``, writes the result to
``DATA_DIR/eval_runs/<ISO-timestamp>_<tag>.json``.

Usage:
    python scripts/run_eval.py --tag baseline_v0.1.0
    python scripts/run_eval.py --tag no_feedback --no-feedback
    python scripts/run_eval.py --limit 50 --min-score 5

Cost note:
    Re-scoring 100 labeled articles ≈ $0.30 on Haiku 4.5. --limit caps
    the run; over 200 articles requires --yes as a confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import database, eval as evalmod  # noqa: E402
from app.config import DATA_DIR  # noqa: E402


_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _valid_tag(raw: str) -> str:
    """argparse ``type=`` hook: ensure the tag is filename-safe (avoids
    path-traversal via ``../`` and Windows-illegal characters)."""
    if not _TAG_RE.match(raw):
        raise argparse.ArgumentTypeError(
            "tag must match [A-Za-z0-9._-]{1,64} (no slashes / spaces)"
        )
    return raw


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-score the golden-set labeled articles and dump metrics."
    )
    parser.add_argument(
        "--tag",
        type=_valid_tag,
        default="run",
        help="filename tag (default: 'run'); appears in the output artefact name",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap on articles to score (None = all labeled articles)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=evalmod.DEFAULT_MIN_SCORE,
        help=f"positive-prediction threshold (default: {evalmod.DEFAULT_MIN_SCORE})",
    )
    parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="empty the few-shot list — baseline behaviour without user signal",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm running against > 200 articles (cost guardrail)",
    )
    return parser.parse_args(argv)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _artefact_path(tag: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    out_dir = DATA_DIR / "eval_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{ts}_{tag}.json"


def main() -> int:
    _setup_logging()
    log = logging.getLogger("run_eval")
    args = _parse_args()

    total = database.count_golden_labels()
    if total == 0:
        log.warning(
            "No golden labels in the DB — label articles as 'relevant' or "
            "'noise' via the dashboard first."
        )
        return 1

    effective = min(args.limit, total) if args.limit is not None else total
    if effective > 200 and not args.yes:
        log.error(
            "Refusing to run against %s articles without --yes (cost guardrail). "
            "Pass --yes to confirm.", effective,
        )
        return 2

    log.info(
        "Running eval: %s labels · min_score=%s · no_feedback=%s",
        effective, args.min_score, args.no_feedback,
    )
    try:
        result = asyncio.run(
            evalmod.run_eval(
                limit=args.limit,
                no_feedback=args.no_feedback,
                min_score=args.min_score,
            )
        )
    except KeyboardInterrupt:
        return 130

    result["tag"] = args.tag
    result["run_at"] = datetime.now(timezone.utc).isoformat()

    out_path = _artefact_path(args.tag)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    m = result["metrics"]
    log.info(
        "Eval done · precision=%s recall=%s f1=%s · TP=%s FP=%s FN=%s TN=%s · errors=%s",
        m["precision"], m["recall"], m["f1"],
        m["confusion"]["tp"], m["confusion"]["fp"],
        m["confusion"]["fn"], m["confusion"]["tn"],
        m["errors"],
    )
    log.info("Artefact written: %s", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
