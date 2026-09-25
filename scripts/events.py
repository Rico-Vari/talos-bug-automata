#!/usr/bin/env python3
"""
Event ledger inspector.

A filter you cannot audit gets switched off within a week: this shows what
the gates swallowed without opening the database by hand.

  python scripts/events.py                  # last 30
  python scripts/events.py --rejected       # rejected only, with the reason
  python scripts/events.py --repo owner/x
  python scripts/events.py --show 42        # one full row
  python scripts/events.py --deliveries     # every delivery and its verdict,
                                            # including rejections with no row
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from talos import store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--rejected", action="store_true", help="rejected events only")
    ap.add_argument("--active", action="store_true", help="live events only")
    ap.add_argument("--repo", help="filter by owner/repo")
    ap.add_argument("--show", type=int, metavar="ID", help="show one full row")
    ap.add_argument("--deliveries", action="store_true",
                    help="deliveries with their verdict (rejections that happen before a row "
                         "exists —anti-loop, foreign PR, unmapped repo— only live here)")
    args = ap.parse_args()

    if not store.DB_PATH.exists():
        print(f"{store.DB_PATH} does not exist yet — no events recorded.")
        return 0

    store.init_db()
    with store.connect() as conn:
        if args.deliveries:
            rows = list(conn.execute(
                "SELECT * FROM deliveries ORDER BY received_at DESC LIMIT ?", (args.limit,)
            ))
            print(f"{'received':<21} {'source':<7} {'resource.action':<40} {'verdict':<26} ev")
            print("-" * 104)
            for r in rows:
                ra = f"{r['resource'] or ''}.{r['action'] or ''}"[:40]
                print(f"{r['received_at']:<21} {r['source']:<7} {ra:<40} "
                      f"{(r['verdict'] or '-'):<26} {r['event_id'] or ''}")
            return 0
        if args.show:
            row = store.get_event(conn, args.show)
            if row is None:
                print(f"No event {args.show}")
                return 1
            width = max(len(k) for k in row.keys())
            for k in row.keys():
                if row[k] not in (None, ""):
                    print(f"{k:<{width}}  {row[k]}")
            return 0

        sql = "SELECT * FROM events"
        where, params = [], []
        if args.rejected:
            where.append("state LIKE 'rejected_%'")
        if args.active:
            where.append("state IN (%s)" % ",".join("?" * len(store.ACTIVE_STATES)))
            params += sorted(store.ACTIVE_STATES)
        if args.repo:
            where.append("gh_repo = ?")
            params.append(args.repo)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(args.limit)

        rows = list(conn.execute(sql, params))
        if not rows:
            print("No matching events.")
            return 0

        print(f"{'id':>4}     {'state':<24} {'rnd':>3} {'vis':>3}  {'dedupe_key':<46}  title")
        print("-" * 121)
        for r in rows:
            title = (r["title"] or "")[:34]
            icon = store.state_icon(r["state"])
            print(
                f"{r['id']:>4}  {icon} {r['state']:<24} {r['rounds']:>3} {r['seen_count']:>3}  "
                f"{r['dedupe_key'][:46]:<46}  {title}"
            )
        print(f"\n{len(rows)} event(s). Details: python scripts/events.py --show <id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
