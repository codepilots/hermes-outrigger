"""`hermes outrigger …`: pair, list and revoke client tokens (ctx.register_cli_command)."""
from __future__ import annotations

import time

from . import tokens


def setup(subparser) -> None:
    subs = subparser.add_subparsers(dest="outrigger_command")
    p = subs.add_parser("pair", help="Mint a token for one client (shown once)")
    p.add_argument("--user", required=True, help="User id this client acts as on the Outrigger platform")
    p.add_argument("--name", default="", help="Display name")
    p.add_argument("--label", default="", help="What the token is for, e.g. 'phone' or 'work laptop'")
    p.add_argument("--admin", action="store_true",
                   help="May run every slash command (otherwise only user_allowed_commands)")
    subs.add_parser("tokens", help="List tokens (never shows the secrets)")
    r = subs.add_parser("revoke", help="Revoke a token; its live connections close")
    r.add_argument("token_id")
    subparser.set_defaults(func=handle)


def handle(args) -> int:
    cmd = getattr(args, "outrigger_command", None)
    if cmd == "pair":
        try:
            tid, token = tokens.mint(args.user, user_name=args.name, label=args.label, admin=args.admin)
        except ValueError as e:
            print(f"error: {e}")
            return 2
        print(f"Token {tid} for user '{args.user}'{' (admin)' if args.admin else ''}:\n")
        print(f"  {token}\n")
        print("Shown once. Paste it into the client's key/token setting. It works on the")
        print("Outrigger platform only (chat, slash commands, tools on its own chats), not as")
        print("an API_SERVER_KEY. Revoke with: hermes outrigger revoke " + tid)
        return 0
    if cmd == "tokens":
        rows = tokens.listing()
        if not rows:
            print("No tokens. Create one with: hermes outrigger pair --user <id>")
            return 0
        for r in rows:
            state = "revoked " + time.strftime("%Y-%m-%d", time.localtime(r["revoked"])) if r.get("revoked") else "active"
            print(f"{r['id']}  {r['user_id']:<16} {'admin' if r.get('admin') else 'user ':<5}  {state:<18} "
                  f"{time.strftime('%Y-%m-%d', time.localtime(r.get('created', 0)))}  {r.get('label', '')}")
        return 0
    if cmd == "revoke":
        if tokens.revoke(args.token_id):
            print(f"Revoked {args.token_id}. Its connections close within ~20 s.")
            return 0
        print(f"No active token {args.token_id}.")
        return 1
    print("usage: hermes outrigger {pair,tokens,revoke}")
    return 2
