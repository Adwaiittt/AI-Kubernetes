"""The dashboard's typed client is generated from frontend/openapi.json; it must match the API."""

import json
from pathlib import Path

from app.core.config import Settings
from app.main import create_app

SNAPSHOT = Path(__file__).resolve().parents[3] / "frontend" / "openapi.json"


def test_frontend_openapi_snapshot_is_current() -> None:
    current = create_app(settings=Settings(_env_file=None)).openapi()
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert committed == current, (
        "frontend/openapi.json is stale. Regenerate it and the client:\n"
        "  cd backend && uv run python -m app.export_openapi ../frontend/openapi.json\n"
        "  cd frontend && npm run gen:api"
    )
