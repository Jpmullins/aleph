"""aleph-evals CLI.

`python -m aleph_evals --datasets all --gate strict --profile aleph-dev`.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from aleph_evals.ci.gate import write_summary
from aleph_evals.runner import Gate, run


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Aleph eval runner")
    p.add_argument(
        "--datasets",
        default="all",
        help="'all', a comma list, or '@tag'. Default: all.",
    )
    p.add_argument(
        "--gate",
        choices=[g.value for g in Gate],
        default=Gate.STRICT.value,
    )
    p.add_argument(
        "--datasets-root",
        type=Path,
        default=Path(
            os.environ.get(
                "ALEPH_EVAL_DATASETS_ROOT",
                "packages/aleph-evals/datasets",
            )
        ),
    )
    p.add_argument(
        "--profile",
        default=os.environ.get("ALEPH_DEFAULT_MODEL_PROFILE", "aleph-dev"),
    )
    p.add_argument("--report", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run(
        datasets_root=args.datasets_root,
        selected=args.datasets,
        gate=Gate(args.gate),
        profile_name=args.profile,
    )
    return write_summary(report, path=str(args.report) if args.report else None)


if __name__ == "__main__":
    raise SystemExit(main())
