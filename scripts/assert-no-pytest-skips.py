#!/usr/bin/env python3
"""Fail a release gate when a JUnit result unexpectedly contains skipped tests."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("junit_xml", type=Path)
    args = parser.parse_args()
    root = ET.parse(args.junit_xml).getroot()
    skipped = sum(int(suite.get("skipped", "0")) for suite in root.iter("testsuite"))
    if skipped:
        raise SystemExit(f"unexpected PostgreSQL test skips: {skipped}")
    print("PostgreSQL release tests: 0 skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
