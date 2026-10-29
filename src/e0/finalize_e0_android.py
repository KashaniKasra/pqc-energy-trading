#!/usr/bin/env python3
"""Create the standalone Android E0 result; never edits canonical server data."""

import argparse
from pathlib import Path

from android_e0_reporting import finalize_android_v3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--comparison-output", required=True, type=Path)
    args = parser.parse_args()
    finalize_android_v3(
        args.evidence_root.resolve(),
        args.output.resolve(),
        args.comparison_output.resolve(),
    )


if __name__ == "__main__":
    main()
