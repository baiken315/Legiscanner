"""FastAPI application for Legiscanner web interface."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request

from .. import db
from .routes import router

logger = logging.getLogger(__name__)


def create_app(db_path: Path | None = None) -> FastAPI:
    """
    Create the FastAPI application.

    Args:
        db_path: Path to the SQLite database file. Defaults to ./legiscanner.db
    """
    app = FastAPI(
        title="Legiscanner",
        description="Virginia Legislative Bill Scanner for Local Government",
        version="0.1.0",
    )

    app.state.db_path = db_path or Path("legiscanner.db")

    # Verify the DB is initialized (has at least a sessions table) at startup.
    conn = db.get_connection(app.state.db_path)
    db.init_db(conn)
    conn.close()

    templates_dir = Path(__file__).parent / "templates"
    from fastapi.templating import Jinja2Templates
    app.state.templates = Jinja2Templates(directory=str(templates_dir))

    app.include_router(router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    return app
