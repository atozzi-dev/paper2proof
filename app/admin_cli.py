"""Admin helper for the running endpoint: invites, gallery curation, drain and kill switch.

Usage (docs/OPERATIONS.md):
    uv run python -m app.admin_cli [--url URL] invite create [--quota N] [--expires ISO]
    uv run python -m app.admin_cli invite list
    uv run python -m app.admin_cli invite revoke (--id HEX | --code-stdin)
    uv run python -m app.admin_cli runs [--listed | --unlisted] [--limit N]
    uv run python -m app.admin_cli gallery add RUN_ID [--reviewed]
    uv run python -m app.admin_cli gallery remove RUN_ID
    uv run python -m app.admin_cli drain on|off
    uv run python -m app.admin_cli kill-switch on|off

The URL comes from --url or P2P_API_URL. The admin token comes from ADMIN_TOKEN or a prompt,
never from the command line (shell history, process listings). Invite codes to revoke are read
from stdin and hashed here: only the hash is sent.
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO
from urllib.parse import urlsplit

import httpx

DEFAULT_QUOTA = 3  # app.api.admin.DEFAULT_QUOTA (not imported: the CLI stays light)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
TIMEOUT_S = 30.0
RUN_ID = re.compile(r"^r_[0-9a-z]{26}$")  # app.api.deps.RUN_ID: never a path or a query


class UsageError(Exception):
    """Bad input: exit status 2."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.admin_cli", description=__doc__)
    parser.add_argument("--url", help="endpoint URL (default: $P2P_API_URL)")
    commands = parser.add_subparsers(dest="command", required=True)

    invite = commands.add_parser("invite").add_subparsers(dest="action", required=True)
    create = invite.add_parser("create", help="new invite code (printed once)")
    create.add_argument("--quota", type=int, default=DEFAULT_QUOTA, help="runs per day")
    create.add_argument("--expires", help="ISO 8601 expiry, e.g. 2026-12-15T00:00:00Z")
    invite.add_parser("list", help="invites by hash prefix (never the codes)")
    revoke = invite.add_parser("revoke", help="expire an invite now")
    which = revoke.add_mutually_exclusive_group(required=True)
    which.add_argument("--id", help="hash prefix from `invite list` (12+ hex characters)")
    which.add_argument("--code-stdin", action="store_true", help="read the code from stdin")

    runs = commands.add_parser("runs", help="recent runs with their verdicts (to curate)")
    listed = runs.add_mutually_exclusive_group()
    listed.add_argument("--listed", dest="listed", action="store_const", const=True)
    listed.add_argument("--unlisted", dest="listed", action="store_const", const=False)
    runs.add_argument("--limit", type=int, default=50)

    gallery = commands.add_parser("gallery").add_subparsers(dest="action", required=True)
    add = gallery.add_parser("add", help="list a finished run in the public gallery")
    add.add_argument("run_id")
    add.add_argument(
        "--reviewed",
        action="store_true",
        help="you reviewed the run by hand (required for a NOT_REPRODUCED verdict)",
    )
    remove = gallery.add_parser("remove", help="take a run down from the gallery")
    remove.add_argument("run_id")

    for name in ("drain", "kill-switch"):
        toggle = commands.add_parser(name)
        toggle.add_argument("state", choices=("on", "off"))
    return parser


def _base_url(raw: str | None) -> str:
    if not raw:
        raise UsageError("give --url or set P2P_API_URL")
    url = urlsplit(raw)
    local = url.scheme == "http" and (url.hostname or "") in LOCAL_HOSTS
    if (url.scheme != "https" and not local) or not url.hostname or url.username or url.password:
        raise UsageError("the URL must be https://host (plain http only for localhost)")
    return f"{url.scheme}://{url.netloc}"


def _request(
    args: argparse.Namespace, stdin: TextIO, prompt: Callable[[str], str]
) -> tuple[str, str, object, dict[str, str]]:
    """(method, path, json body, query) for the parsed command."""
    if args.command == "invite" and args.action == "create":
        return (
            "POST",
            "/admin/invites",
            {"quota_per_day": args.quota, "expires_at": args.expires},
            {},
        )
    if args.command == "invite" and args.action == "list":
        return "GET", "/admin/invites", None, {}
    if args.command == "invite":
        if args.code_stdin:
            # typed at a terminal: not echoed
            code = (prompt("Invite code: ") if stdin.isatty() else stdin.readline()).strip()
            if not code:
                raise UsageError("no invite code on stdin")
            ident = hashlib.sha256(code.encode("utf-8")).hexdigest()
        else:
            ident = str(args.id).lower()
        return "POST", "/admin/invites/revoke", {"id": ident}, {}
    if args.command == "runs":
        query = {"limit": str(args.limit)}
        if args.listed is not None:
            query = {"listed": "true" if args.listed else "false", **query}
        return "GET", "/admin/runs", None, query
    if args.command == "gallery":
        if not RUN_ID.fullmatch(args.run_id):
            raise UsageError("not a run id (r_ and 26 lowercase letters or digits)")
        body = {"listed": args.action == "add", "reviewed": bool(getattr(args, "reviewed", False))}
        return "POST", f"/admin/gallery/{args.run_id}", body, {}
    return "POST", f"/admin/{args.command}", {"on": args.state == "on"}, {}


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    env = environ if environ is not None else os.environ
    out, err = stdout or sys.stdout, stderr or sys.stderr
    args = _parser().parse_args(argv)
    try:
        base = _base_url(args.url or env.get("P2P_API_URL"))
        method, path, body, query = _request(args, stdin or sys.stdin, prompt)
    except UsageError as error:
        print(f"admin_cli: {error}", file=err)
        return 2
    token = env.get("ADMIN_TOKEN") or prompt("Admin token: ")
    if not token:
        print("admin_cli: no admin token", file=err)
        return 2
    # trust_env=False: no HTTP(S)_PROXY or netrc, which would get the token (clear for http)
    with httpx.Client(
        base_url=base, transport=transport, trust_env=False, timeout=TIMEOUT_S
    ) as client:
        try:
            response = client.request(
                method,
                path,
                json=body,
                params=query or None,
                headers={"X-Admin-Token": token},
            )
        except httpx.HTTPError as error:
            print(f"admin_cli: {type(error).__name__}: the endpoint did not answer", file=err)
            return 1
    if response.status_code >= 400:
        try:
            detail = str(response.json().get("error", ""))
        except (ValueError, AttributeError):
            detail = ""
        print(f"admin_cli: HTTP {response.status_code} {detail}".rstrip(), file=err)
        return 1
    if response.status_code == 204:
        print("ok", file=out)
    elif path == "/admin/invites" and method == "POST":
        print(response.json()["code"], file=out)  # the only time the code is shown
    else:
        print(json.dumps(response.json(), indent=2), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
