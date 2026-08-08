"""Append newly completed windows to the frozen semiconductor v2.9 ledger."""

from __future__ import annotations

import argparse
import json

from scripts.semiconductor_grid_forward_oos_v29 import (
    DEFAULT_DATA,
    DEFAULT_OUTPUT,
    append_frozen_forward_oos,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append closed Forward OOS windows using frozen v2.9 artifacts"
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--run-time-utc", default="")
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = append_frozen_forward_oos(
        output_dir=args.output_dir,
        data_dir=args.data_dir,
        run_time_utc=args.run_time_utc or None,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
