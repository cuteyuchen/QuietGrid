"""Append newly completed windows to the frozen semiconductor v2.9 ledger."""

from __future__ import annotations

import argparse
import json

from scripts.semiconductor_grid_forward_oos_v29 import DEFAULT_DATA, DEFAULT_OUTPUT
from scripts.semiconductor_grid_forward_oos_v29_monitor import monitor_forward_oos


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect and append closed Forward OOS windows using the frozen "
            "v2.9 31111 monitor"
        )
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--run-time-utc", default="")
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = monitor_forward_oos(
        output_dir=args.output_dir,
        data_dir=args.data_dir,
        run_time_utc=args.run_time_utc or None,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
