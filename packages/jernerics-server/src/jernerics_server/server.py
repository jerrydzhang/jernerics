from pathlib import Path

import uvicorn

from .http import create_app
from .store import Store


def serve(
    db_path: str | Path,
    *,
    host: str = "[::]",
    http_port: int = 8000,
    api_key: str | None = None,
    artifacts_root: str | Path | None = None,
) -> None:
    store = Store(db_path)
    root = (
        Path(artifacts_root) if artifacts_root else Path(db_path).parent / "artifacts"
    )
    app = create_app(store, api_key=api_key, artifacts_root=root)
    config = uvicorn.Config(app, host=host, port=http_port, log_level="error")
    uvicorn.Server(config).run()
