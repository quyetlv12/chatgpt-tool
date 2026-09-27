"""CLI: export + convert."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gpt_tool.convert import FORMATS
from gpt_tool.export import convert_bulk, export_bulk


def main(argv: list[str] | None = None) -> int:
    from gpt_tool.ensure_deps import ensure_deps

    ensure_deps()
    parser = argparse.ArgumentParser(prog="gpt-tool", description="Codex export + JSON converter")
    sub = parser.add_subparsers(dest="cmd", required=True)

    exp = sub.add_parser("export", help="login email|pass|2fa and export one JSON per account")
    exp.add_argument("--format", required=True, choices=FORMATS)
    exp.add_argument("--lines", help="file of email|password|totp lines")
    exp.add_argument("--out", default="out")
    exp.add_argument("--proxy")
    exp.add_argument("--workers", type=int, default=2)

    conv = sub.add_parser("convert", help="convert existing session/OAuth JSON")
    conv.add_argument("--format", required=True, choices=FORMATS)
    conv.add_argument("--in", dest="infile", required=True)
    conv.add_argument("--out", default="out")

    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.cmd == "export":
        if args.lines:
            text = Path(args.lines).read_text(encoding="utf-8")
        elif not sys.stdin.isatty():
            text = sys.stdin.read()
        else:
            print("pass --lines or pipe accounts on stdin", file=sys.stderr)
            return 2
        results = export_bulk(text.splitlines(), args.format, out, args.proxy, args.workers)
        ok = sum(1 for r in results if r.ok)
        print(f"done {ok}/{len(results)} → {out}")
        for r in results:
            mark = "OK" if r.ok else "FAIL"
            print(f"  {mark} {r.email} {r.path or r.error}")
        return 0 if ok == len(results) else 1

    text = Path(args.infile).read_text(encoding="utf-8")
    results = convert_bulk(text, args.format, out)
    print(f"converted {len(results)} → {out}")
    for r in results:
        print(f"  OK {r.email} {r.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
