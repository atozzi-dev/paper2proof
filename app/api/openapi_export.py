"""Write the OpenAPI schema the web client generates its TypeScript types from.

Usage: uv run python -m app.api.openapi_export [OUT]   (default: print to stdout)
"""

import json
import sys
from pathlib import Path
from typing import cast

from app.api.main import AppState, create_app


def build() -> str:
    app = create_app(cast(AppState, None))  # schema generation never touches the state
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> None:
    text = build()
    if argv:
        Path(argv[0]).write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main(sys.argv[1:])
