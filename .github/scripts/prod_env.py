"""Print the env-file for deploy/endpoint.sh: the P2P_PROD_ENV settings plus the job images.

P2P_PROD_ENV (a `prod` environment variable) holds the non-secret settings of
deploy/prod.env.example except the three job images, which come from a digests.json written by
the deploy workflow (this deploy, or the previous one for a rollback). endpoint.sh checks every
key against deploy/prod.env.example and refuses secret-looking names.

    python3 .github/scripts/prod_env.py deploy/digests.json > "$RUNNER_TEMP/prod.env"
"""

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

DIGEST_REF = re.compile(r"^[a-z0-9.\-/:]+@sha256:[0-9a-f]{64}$")
JOB_IMAGES = {
    "P2P_PROVISIONER_IMAGE": "p2p-provisioner",
    "P2P_RUNNER_IMAGE": "p2p-runner",
    "P2P_RUNNER_CPU_IMAGE": "p2p-runner-cpu",
}


def render(settings: str, digests: Mapping[str, str]) -> str:
    lines = settings.replace("\r\n", "\n").rstrip("\n").split("\n")
    if not any(line.strip() and not line.lstrip().startswith("#") for line in lines):
        raise ValueError("P2P_PROD_ENV has no settings")
    for line in lines:
        key = line.strip().split("=", 1)[0]
        if key in JOB_IMAGES:
            raise ValueError(f"P2P_PROD_ENV must not set {key}: CI writes the job images")
    for key, name in JOB_IMAGES.items():
        ref = digests.get(name, "")
        if not DIGEST_REF.fullmatch(ref):
            raise ValueError(f"{name} is not a digest reference in the digests file")
        lines.append(f"{key}={ref}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    env = os.environ if env is None else env
    if len(args) != 1:
        print("usage: prod_env.py <digests.json>", file=sys.stderr)
        return 2
    settings = env.get("P2P_PROD_ENV", "")
    if not settings:
        print("prod_env: set the P2P_PROD_ENV variable (see the A-05 checklist)", file=sys.stderr)
        return 2
    try:
        digests = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        sys.stdout.write(render(settings, digests))
    except (OSError, ValueError) as err:
        print(f"prod_env: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
