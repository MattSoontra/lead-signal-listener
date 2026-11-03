#!/usr/bin/env python3
"""Feed the local scanners' daily markdown into Lead Signal Listener.

Reads the GTM Systems agent's combined leads file (job-watch + gig-watch,
deduped by link) and, when present, the raw job-watch leads file, converts
each lead to an import row, and hands the batch to `app.py --import` (which
normalizes, scores, and upserts). Cross-source URL de-duplication in the
store makes this idempotent: re-running a day adds nothing new.

Usage:
  python3 tools/import_scanners.py [--date YYYY-MM-DD] [--combined FILE]
                                   [--jobwatch FILE] [--data DIR]
Defaults: today's files in their usual workspace locations.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.dirname(ROOT)

SECTION = re.compile(r"^##\s+(.*)$")
BULLET = re.compile(r"^-\s+([^:]+):\s*(.*)$")
COMBINED_HEAD = re.compile(r"^\d+\.\s+\[([^\]]+)\]\s+(.*)$")


def _sections(text):
    """Yield (heading, {bullet-key: value}) per '## ' section of a leads file."""
    heading, bullets = None, {}
    for line in text.splitlines():
        m = SECTION.match(line)
        if m:
            if heading is not None:
                yield heading, bullets
            heading, bullets = m.group(1).strip(), {}
            continue
        b = BULLET.match(line)
        if b and heading is not None:
            bullets[b.group(1).strip().lower()] = b.group(2).strip()
    if heading is not None:
        yield heading, bullets


def parse_combined(path):
    """combined-leads-YYYY-MM-DD.md: '## 1. [job-watch] <title>' sections."""
    rows = []
    for heading, b in _sections(open(path, encoding="utf-8").read()):
        m = COMBINED_HEAD.match(heading)
        if not m or not b.get("link"):
            continue
        text = b.get("note", "")
        if b.get("budget/pay"):
            text = (text + f" Budget/pay: {b['budget/pay']}").strip()
        rows.append({"source": m.group(1), "kind": "pain", "title": m.group(2),
                     "text": text, "author": b.get("who", ""), "url": b["link"],
                     "query": "scanner-import"})
    return rows


def parse_jobwatch(path):
    """job-watch leads-YYYY-MM-DD.md: '## <title>' sections, other bullet keys."""
    rows = []
    for heading, b in _sections(open(path, encoding="utf-8").read()):
        if not b.get("link"):
            continue
        text = b.get("why it fits", "")
        if b.get("pay"):
            text = (text + f" Pay: {b['pay']}").strip()
        rows.append({"source": "job-watch", "kind": "pain", "title": heading,
                     "text": text, "author": b.get("company", ""), "url": b["link"],
                     "community": b.get("location", ""), "query": "scanner-import"})
    return rows


def main():
    today = datetime.date.today().isoformat()
    ap = argparse.ArgumentParser(description="Import scanner leads into Lead Signal Listener")
    ap.add_argument("--date", default=today, help="day to import (default: today)")
    ap.add_argument("--combined", help="combined leads file (default: GTM Systems agent's, dated --date)")
    ap.add_argument("--jobwatch", help="job-watch leads file (default: job-watch output, dated --date)")
    ap.add_argument("--data", default=os.path.join(ROOT, "data"), help="app data dir (config.json + signals.db)")
    args = ap.parse_args()

    combined = args.combined or os.path.join(
        WORKSPACE, "agents", "gtm-systems", f"combined-leads-{args.date}.md")
    jobwatch = args.jobwatch or os.path.join(
        WORKSPACE, "job-watch", f"leads-{args.date}.md")

    rows = []
    for path, parser in ((combined, parse_combined), (jobwatch, parse_jobwatch)):
        if os.path.exists(path):
            found = parser(path)
            print(f"{os.path.basename(path)}: {len(found)} lead(s)")
            rows.extend(found)
        else:
            print(f"{os.path.basename(path)}: not found, skipped")
    if not rows:
        print("nothing to import")
        return 0

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
        tmp = f.name
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"), "--import", tmp, "--data", args.data],
            capture_output=True, text=True)
    finally:
        os.unlink(tmp)
    if out.returncode != 0:
        print(out.stdout, end="")
        print(out.stderr, end="", file=sys.stderr)
        return out.returncode
    # app.py --import prints "received N, new M"
    m = re.search(r"received (\d+), new (\d+)", out.stdout)
    if not m:
        print(out.stdout, end="")
        return 1
    received, new = int(m.group(1)), int(m.group(2))
    print(f"received {received}, new {new}, skipped {received - new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
