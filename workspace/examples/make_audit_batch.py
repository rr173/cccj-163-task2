#!/usr/bin/env python3
"""Generate a valid audit evidence batch (seq + source + digest chain) for a
vehicle from its live snapshot chain. Output can be fed to:

  ./route_replay.py audit-import <file>
  curl -X POST localhost:8080/api/audit/imports -d @file

Usage:
  python3 examples/make_audit_batch.py --vehicle VH-01 \
      --import-key OPS-2026-10-03-001 --out /tmp/audit_batch.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from audit import AuditEngine  # noqa: E402
from engine import MapEngine  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicle", default="VH-01")
    ap.add_argument("--import-key", default=None)
    ap.add_argument("--out", default=None, help="write to file instead of stdout")
    ap.add_argument("--data", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "mapstate.json"))
    args = ap.parse_args()

    audit = AuditEngine(MapEngine(args.data))
    batch = audit.build_sample_batch(args.vehicle, args.import_key)
    text = json.dumps(batch, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已生成 {len(batch['events'])} 条事件 -> {args.out}"
              f"（导入键 {batch['import_key']}）", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
