"""Write the OpenAPI schema to a file (the frontend generates its typed client from it).

uv run python -m app.export_openapi ../frontend/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.core.config import Settings
from app.main import create_app


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    schema = create_app(settings=Settings(_env_file=None)).openapi()
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
