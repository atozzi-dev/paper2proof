"""Get a short-lived Nebius IAM token for the deploy job and write it to a private file.

This is the only place where CI turns an identity into Nebius credentials. Two modes, chosen by
the `prod` environment variable NEBIUS_AUTH_MODE:

  oidc  GitHub OIDC token (audience NEBIUS_OIDC_AUDIENCE) exchanged for a token of the service
        account NEBIUS_CI_SA_ID through its federated credential (infra project_iam module).
  key   fallback if spike S-7 fails: the service account credentials file held in the `prod`
        environment secret NEBIUS_SA_CREDENTIALS (required reviewers guard the environment).

ASSUMPTION (S-7 pending): the exchange follows the Nebius SDK's FederatedCredentialsBearer
(subject = service account id, actor = the GitHub JWT), which is also what a CLI profile with
`federated-subject-credentials-file-path` does. If S-7 shows another request shape, change only
`exchange()` below.

Every credential is masked (`::add-mask::`) before anything else is printed, nothing secret is
ever printed, and only the access token file (mode 0600) is left behind. Later steps read it into
NEBIUS_IAM_TOKEN for the command that needs it; the workflow deletes it at the end of the job.

    uv run --no-sync python .github/scripts/nebius_token.py --out "$RUNNER_TEMP/nebius.token"
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

EXCHANGE_TIMEOUT_S = 60.0
GITHUB_TIMEOUT_S = 30.0

ExchangeKind = Literal["federated", "credentials-file"]
Exchange = Callable[[ExchangeKind, Path, str | None], str]


class TokenError(Exception):
    """A failure whose message is safe to print (it never contains a credential)."""


def write_private(path: Path, text: str) -> None:
    """Create (or truncate) a file readable by the runner user only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)  # O_CREAT's mode does not apply to an existing file


def mask(value: str) -> None:
    # `::add-mask::` masks one line only: a multi-line credential would leak its other lines.
    if "\n" in value or "\r" in value:
        raise TokenError("refusing to handle a multi-line credential")
    print(f"::add-mask::{value}", flush=True)


def request_github_jwt(
    env: Mapping[str, str],
    audience: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not request_token:
        raise TokenError("no GitHub OIDC request URL: the job needs `permissions: id-token: write`")
    request = urllib.request.Request(  # noqa: S310 - the runner-provided https URL
        f"{url}&audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"Bearer {request_token}"},
    )
    try:
        with opener(request, timeout=GITHUB_TIMEOUT_S) as response:
            value = json.load(response).get("value", "")
    except Exception as err:
        # Print the type only: HTTP errors can carry the request (and its bearer header).
        raise TokenError(f"GitHub OIDC token request failed: {type(err).__name__}") from None
    if not isinstance(value, str) or not value:
        raise TokenError("GitHub OIDC token response has no value")
    return value


async def _sdk_token(kind: ExchangeKind, path: Path, sa_id: str | None) -> str:
    from nebius.sdk import SDK

    if kind == "federated":
        from nebius.aio.token.federated_credentials import FederatedCredentialsBearer

        channel: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        bearer = FederatedCredentialsBearer(str(path), service_account_id=sa_id, channel=channel)
        sdk = SDK(credentials=bearer)
        channel.set_result(sdk)
    else:
        sdk = SDK(credentials_file_name=str(path))
    try:
        token = await sdk.get_token(timeout=EXCHANGE_TIMEOUT_S)
        return str(token.token)
    finally:
        await sdk.close()


def exchange(kind: ExchangeKind, path: Path, sa_id: str | None) -> str:
    """Nebius access token for the federated JWT or the credentials file at `path`."""
    try:
        return asyncio.run(_sdk_token(kind, path, sa_id))
    except Exception as err:
        # SDK errors can echo request fields; print the type only.
        raise TokenError(f"Nebius token exchange ({kind}) failed: {type(err).__name__}") from None


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ValueError(f"{name} is not set (see the A-05 GitHub setup checklist)")
    return value


def _fail(out: Path) -> int:
    with contextlib.suppress(FileNotFoundError):
        out.unlink()
    return 1


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    fetch_jwt: Callable[..., str] = request_github_jwt,
    exchange: Exchange = exchange,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path, help="where to write the token")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env

    try:
        mode = env.get("NEBIUS_AUTH_MODE", "")
        if mode not in {"oidc", "key"}:
            raise ValueError("NEBIUS_AUTH_MODE must be `oidc` or `key`")
        if mode == "oidc":
            sa_id: str | None = _require(env, "NEBIUS_CI_SA_ID")
            audience = _require(env, "NEBIUS_OIDC_AUDIENCE")
            # The workflow passes the secret itself only in key mode, plus a presence flag.
            if env.get("NEBIUS_SA_CREDENTIALS") or env.get("NEBIUS_SA_CREDENTIALS_SET") == "true":
                # The long-lived key bypasses the repo/environment/ref pin of the federated
                # credential: once OIDC works it must be deleted, not kept as a spare.
                raise ValueError("NEBIUS_SA_CREDENTIALS must not be set in oidc mode: delete it")
        else:
            sa_id, audience = None, ""
            credentials = _require(env, "NEBIUS_SA_CREDENTIALS")
        scratch = Path(_require(env, "RUNNER_TEMP"))
    except ValueError as err:
        print(f"::error::{err}")
        return 2

    kind: ExchangeKind = "federated" if mode == "oidc" else "credentials-file"
    input_file = scratch / f"nebius-{kind}.input"
    try:
        if mode == "oidc":
            jwt = fetch_jwt(env, audience)
            mask(jwt)
            write_private(input_file, jwt)
        else:
            write_private(input_file, credentials)
        token = exchange(kind, input_file, sa_id)
        if not token:
            raise TokenError("Nebius returned an empty token")
        mask(token)
        write_private(args.out, token)
    except TokenError as err:
        print(f"::error::{err}")
        return _fail(args.out)
    except OSError as err:
        print(f"::error::cannot write a credential file: {type(err).__name__}")
        return _fail(args.out)
    finally:
        with contextlib.suppress(FileNotFoundError):
            input_file.unlink()
    print(f"Nebius token ({mode}) written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
