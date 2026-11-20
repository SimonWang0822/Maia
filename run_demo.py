#!/usr/bin/env python3
"""Run MAIA's API-backed fund matching demo."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from maia_matcher.data import load_records, validate_records
from maia_matcher.engine import RANKING_BASIS, evaluate_fund, review_fund
from maia_matcher.extractor import DEFAULT_MODEL, ExtractionError, MetaExtractor


ROOT = Path(__file__).resolve().parent


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(funds, needs, extractor, as_of):
    validate_records(funds, "fund_id", "Funds")
    validate_records(needs, "need_id", "Needs")

    def process(fund):
        try:
            extraction = extractor.extract(fund)
        except ExtractionError as exc:
            return review_fund(fund, needs, str(exc))
        return evaluate_fund(fund, needs, extraction, as_of)

    # Extract each fund once, then compare it with every need.
    with ThreadPoolExecutor(max_workers=min(3, len(funds))) as executor:
        groups = list(executor.map(process, funds))
    counts = Counter(result["status"] for group in groups for result in group["results"])
    errors = sum(group["extraction_status"] == "failed" for group in groups)
    return {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "model": extractor.model,
        "run_status": "partial_failure" if errors else "completed",
        "currency": "USD",
        "ranking_basis": RANKING_BASIS,
        "summary": {
            "funds": len(funds), "needs": len(needs), "pairs": len(funds) * len(needs),
            "match": counts["match"], "no_match": counts["no_match"],
            "needs human review": counts["needs human review"],
            "extraction_failures": errors,
        },
        "fund_results": groups,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--funds", type=Path, required=True, help="Funds JSON array or CSV")
    parser.add_argument("--needs", type=Path, required=True, help="Needs JSON array or CSV")
    parser.add_argument("--output", type=Path, default=ROOT / "output/results.json")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)
    try:
        if not os.environ.get("MODEL_API_KEY", "").strip():
            raise ValueError("Export MODEL_API_KEY before running the demo.")
        funds = load_records(args.funds, "fund_id")
        needs = load_records(args.needs, "need_id")
        if args.output.resolve() in {args.funds.resolve(), args.needs.resolve()}:
            raise ValueError("The output path must differ from the input files.")
        extractor = MetaExtractor(model=args.model)
        print(f"Extracting restrictions for {len(funds)} funds; evaluating {len(funds) * len(needs)} pairs.", flush=True)
        report = run(funds, needs, extractor, args.as_of)
        write_report(args.output, report)
    except (OSError, ValueError, ExtractionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    summary = report["summary"]
    print(f"Saved {args.output}: {summary['match']} matches, {summary['no_match']} non-matches, "
          f"{summary['needs human review']} need human review.")
    if summary["extraction_failures"]:
        print("Some extractions failed. Their pairs are marked 'needs human review'; see results for details.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
