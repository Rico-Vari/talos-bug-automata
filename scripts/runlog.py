#!/usr/bin/env python3
"""Read a stream-json run log and summarize it to one line per action.

The raw log is NDJSON with the full content of every message, so it runs to
tens of MB per run. This is for watching a live run without drowning.

    python scripts/runlog.py <log>            # latest stretch
    python scripts/runlog.py <log> --all
"""
import argparse
import json
import pathlib
import sys

SKIP_KEYS = ("file_path", "path", "command", "pattern", "prompt", "description")


def summarize(obj: dict) -> str | None:
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        return f"· init model={obj.get('model')} cwd={obj.get('cwd')}"
    if t == "result":
        return (
            f"· RESULT {obj.get('subtype')} "
            f"turns={obj.get('num_turns')} "
            f"cost=${obj.get('total_cost_usd', 0):.2f} "
            f"err={obj.get('is_error')}"
        )
    msg = obj.get("message") or {}
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            txt = " ".join((block.get("text") or "").split())
            if txt:
                return f"  {txt[:220]}"
        elif block.get("type") == "tool_use":
            name = block.get("name")
            inp = block.get("input") or {}
            arg = ""
            for k in SKIP_KEYS:
                if k in inp:
                    arg = " ".join(str(inp[k]).split())[:160]
                    break
            if not arg and inp:
                arg = " ".join(str(next(iter(inp.values()))).split())[:160]
            return f"→ {name}: {arg}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-n", type=int, default=40)
    args = ap.parse_args()

    lines = []
    raw = pathlib.Path(args.log).read_text(errors="replace")
    for line in raw.splitlines():
        line = line.strip().lstrip("\r")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        s = summarize(obj)
        if s:
            lines.append(s)
    out = lines if args.all else lines[-args.n:]
    print("\n".join(out))
    print(f"\n[{len(lines)} actions in total]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
