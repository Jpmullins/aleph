"""CLI entry point: `python -m aleph_evals.runner --datasets all --gate strict`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from aleph_evals.runner import Gate, run


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Aleph eval runner")
    p.add_argument(
        "--datasets",
        default="all",
        help="'all', a comma list, or '@tag' (Inc 8). Default: all.",
    )
    p.add_argument(
        "--gate",
        choices=[g.value for g in Gate],
        default=Gate.STRICT.value,
        help="Gate mode. 'strict' exits 1 on any failure.",
    )
    p.add_argument(
        "--datasets-root",
        type=Path,
        default=Path(os.environ.get("ALEPH_EVAL_DATASETS_ROOT", "evals/datasets")),
        help="Where to discover eval datasets.",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write report JSON to this path.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run(
        datasets_root=args.datasets_root,
        selected=args.datasets,
        gate=Gate(args.gate),
    )

    serialized = {
        "selected_datasets": report.selected_datasets,
        "gate": report.gate.value,
        "results": [r.model_dump() for r in report.results],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(serialized, indent=2))
    else:
        sys.stdout.write(json.dumps(serialized, indent=2) + "\n")
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
