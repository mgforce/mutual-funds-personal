"""
Admin utility: list, unblock, or manually block IPs in the auth gateway's
permanent ip_blocklist.

Blocks created by the gateway's automatic per-IP throttling are permanent
(by design — see analytics/auth.py FAILED_LOGIN_* constants). This CLI
is the only path that removes them.

Usage:
    python scripts/unblock_ip.py list
    python scripts/unblock_ip.py unblock <ip>
    python scripts/unblock_ip.py block <ip> --reason "manual ban"

The auth gateway picks up changes instantly — no restart required, since
the blocklist is read from sqlite on every /login request.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import auth, db


def cmd_list(_args: argparse.Namespace) -> int:
    blocked = auth.list_blocked_ips()
    if not blocked:
        print("(no IPs currently blocked)")
        return 0
    print(f"{'ip':18s}  {'blocked_at':27s}  reason")
    print("-" * 80)
    for r in blocked:
        print(f"{r['ip']:18s}  {r['blocked_at']:27s}  {r.get('reason') or '-'}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    n = auth.unblock_ip(args.ip)
    if n:
        print(f"OK · unblocked {args.ip} (and cleared its failed-attempt history).")
        return 0
    print(f"(no-op — {args.ip} was not in the blocklist)")
    return 1


def cmd_block(args: argparse.Namespace) -> int:
    auth.block_ip(args.ip, reason=args.reason or "manual")
    print(f"OK · blocked {args.ip}  reason={args.reason or 'manual'!r}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Manage the auth gateway's IP blocklist.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="show the currently-blocked IPs")
    pu = sub.add_parser("unblock", help="remove an IP from the blocklist")
    pu.add_argument("ip")
    pb = sub.add_parser("block", help="permanently block an IP manually")
    pb.add_argument("ip")
    pb.add_argument("--reason", default="")
    args = p.parse_args()

    db.init_schema()
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "unblock":
        return cmd_unblock(args)
    if args.cmd == "block":
        return cmd_block(args)
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
