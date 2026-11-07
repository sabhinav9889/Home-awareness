"""The dashboard: static files mounted at /app/, plus the / -> /app/ redirect."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",  # revalidate (ETag) so a deploy reaches open dashboards
}


class _DashboardFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(SECURITY_HEADERS)
        return response


def mount_dashboard(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=307)

    app.mount("/app", _DashboardFiles(directory=STATIC_DIR, html=True), name="dashboard")
