#!/usr/bin/env python3
"""Fetch historical iteration reports and print trend summary.

Usage:
  python3 scripts/fetch_historical.py ITERATION1 [ITERATION2 ...]

Examples of ITERATION strings depend on your ADO team's iteration paths.
"""
import sys
from utilities.historical import fetch_past_iterations, compute_trends


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    iterations = argv[1:]
    data = fetch_past_iterations(iterations)
    trends = compute_trends(data)
    import json
    print(json.dumps(trends, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
