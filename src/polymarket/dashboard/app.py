"""The dashboard's HTTP layer: a static page plus four read-only JSON endpoints."""

from __future__ import annotations

import logging
import sqlite3
import threading
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import queries

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"


def build_app(db: str | Path) -> FastAPI:
    app = FastAPI(title="Polymarket tennis dashboard", docs_url=None, redoc_url=None)
    # One connection per thread: sqlite3 objects are not safe to share across
    # them, and a read-only connection is cheap enough to make per worker.
    local = threading.local()

    def conn() -> sqlite3.Connection:
        existing = getattr(local, "conn", None)
        if existing is None:
            existing = local.conn = queries.connect(db)
        return existing

    @app.exception_handler(queries.MissingDatabase)
    def _missing(_request: object, exc: queries.MissingDatabase) -> JSONResponse:
        return JSONResponse(
            {"error": f"no capture database at {exc}", "hint": "start `polymarket run` first"},
            status_code=503,
        )

    @app.get("/api/overview")
    def api_overview() -> dict[str, Any]:
        return queries.overview(conn())

    @app.get("/api/pulse")
    def api_pulse() -> dict[str, Any]:
        """Cheap poll target: the client refetches only when this moves."""
        cursor = conn().execute("SELECT MAX(ts), COUNT(*) FROM books")
        last_ts, rows = cursor.fetchone()
        return {"last_ts": last_ts, "rows": rows}

    @app.get("/api/match/{condition_id}")
    def api_match(condition_id: str) -> dict[str, Any]:
        detail = queries.match_detail(conn(), condition_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="no such match")
        return detail

    @app.get("/api/match/{condition_id}/series")
    def api_series(
        condition_id: str,
        points: int = Query(900, ge=50, le=20000),
        since: float | None = None,
    ) -> dict[str, Any]:
        return queries.match_series(conn(), condition_id, max_points=points, since=since)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _ensure_schema(db: str | Path) -> None:
    """Apply any missing schema to an older capture database, once, at startup.

    Serving is strictly read-only, but a read-only connection cannot create the
    index the per-match lookups seek on -- and without it every refresh degrades
    into a full scan of a season's ticks. Store's migration is the same additive,
    idempotent one `run` performs on every start, so applying it here is safe
    beside a live capture. If the file cannot be written, serving still works.
    """
    from ..store import Store

    try:
        Store(db).close()
    except sqlite3.Error as exc:
        log.warning("could not upgrade %s (%s) -- serving it as it is", db, exc)


def serve(db: str | Path, host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> None:
    import uvicorn

    _ensure_schema(db)
    url = f"http://{host}:{port}"
    if open_browser:
        # Fires once the server is accepting connections; a browser that opens
        # first would land on a connection refused.
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    log.info("dashboard on %s  (reading %s read-only)", url, db)
    uvicorn.run(build_app(db), host=host, port=port, log_level="warning")
