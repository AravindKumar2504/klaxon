"""The panel's server: FastAPI on 127.0.0.1, GET and HEAD only, a strict Content-Security-Policy on every answer.

It only reads evidence/. A request with any Host but this panel's own (a page elsewhere that points a name of its own
at 127.0.0.1: DNS rebinding) is refused, and so is every method but GET and HEAD: there is nothing here to change.
FastAPI's own docs pages are off, as they load files from another site.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from klaxon.panel import pages, runs

HOST = "127.0.0.1"  # the only address it listens on: never a flag
PORT = 8090  # neither Compose nor kind uses it (they take 3000, 3100, 3200, 5432, 6379, 8080 to 8082, 9090, 9094)
METHODS = ("GET", "HEAD")
STATIC = Path(__file__).with_name("static")
CSP = "; ".join(
    (
        "default-src 'none'",
        "style-src 'self'",
        "img-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
WRONG_HOST = "Refused: this panel answers only as 127.0.0.1 or localhost, on its own port."
WRONG_METHOD = "Refused: this panel only shows. It answers GET and HEAD, nothing that could change anything."
BROKEN = "The panel could not make this page. What went wrong is in the terminal that runs make panel."


def hosts(port: int) -> frozenset[str]:
    """The Host headers this panel answers: its own address, by number or by name, with its port."""
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def create_app(root: Path = runs.EVIDENCE, port: int = PORT, also: Iterable[Path] = ()) -> FastAPI:
    """The panel, reading the runs under root, and the folders also names (make panel ALSO=, such as make
    gate-demo's): folders from its own command line, never from a request."""
    allowed = hosts(port)
    extra = tuple(Path(folder) for folder in also)
    css = (STATIC / "panel.css").read_text(encoding="utf-8")
    script = (STATIC / "live.js").read_text(encoding="utf-8")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.headers.get("host", "") not in allowed:
            response: Response = PlainTextResponse(WRONG_HOST, status_code=400)
        elif request.method not in METHODS:
            response = PlainTextResponse(WRONG_METHOD, status_code=405, headers={"Allow": ", ".join(METHODS)})
        else:
            try:
                response = await call_next(request)
            except Exception:  # a page that cannot be made still carries the panel's headers (the review's 6)
                traceback.print_exc(file=sys.stderr)
                response = PlainTextResponse(BROKEN, status_code=500)
        for key, value in HEADERS.items():
            response.headers[key] = value
        return response

    def shown(request: Request) -> bool:
        return request.query_params.get("graders") == pages.SHOWN

    @app.api_route("/", methods=list(METHODS))
    def home() -> Response:
        return RedirectResponse("/runs", status_code=303)

    @app.api_route("/runs", methods=list(METHODS))
    def all_runs(request: Request) -> Response:
        show = shown(request)
        return HTMLResponse(pages.runs_page(runs.rows(root, show, extra), show))

    @app.api_route("/run/{name}", methods=list(METHODS))
    def one_run(name: str, request: Request) -> Response:
        # By name, from evidence/'s own folders and the command line's: never a path from the request.
        folder = runs.index(root, extra).get(name)
        if folder is None:
            return HTMLResponse(pages.not_found(name), status_code=404)
        show = shown(request)
        return HTMLResponse(pages.run_page(runs.run(folder, root, show), show))

    @app.api_route("/run/{name}/state", methods=list(METHODS))
    def run_state(name: str) -> Response:
        """What the live view asks every 2 seconds: the folder's fingerprint, and whether to keep asking."""
        folder = runs.index(root, extra).get(name)
        if folder is None:
            return JSONResponse({"error": "no such run"}, status_code=404)
        view = runs.run(folder, root)
        return JSONResponse({"version": view["version"], "live": bool(view["live"])})

    @app.api_route("/evaluation", methods=list(METHODS))
    def evaluation_report(request: Request) -> Response:
        show = shown(request)
        chosen = request.query_params.getlist("from") or list(runs.REPORT_FOLDERS)
        return HTMLResponse(pages.evaluation_page(runs.report(chosen, root, show), show))

    @app.api_route("/static/panel.css", methods=list(METHODS))
    def stylesheet() -> Response:
        return Response(css, media_type="text/css")

    @app.api_route("/static/live.js", methods=list(METHODS))
    def live_view() -> Response:
        return Response(script, media_type="text/javascript")

    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m klaxon.panel", description="Klaxon's web panel: it only shows.")
    parser.add_argument("--port", type=int, default=PORT, help=f"its port on 127.0.0.1 (default {PORT})")
    parser.add_argument(
        "--also",
        type=Path,
        action="append",
        default=[],
        help="a run folder outside evidence/, such as make gate-demo's",
    )
    args = parser.parse_args(argv)
    import uvicorn  # the services' server, already installed (make venv): only needed to serve

    print(f"Klaxon's panel: http://{HOST}:{args.port}/runs (it only reads evidence/; Ctrl-C stops it)", flush=True)
    uvicorn.run(
        create_app(port=args.port, also=args.also),
        host=HOST,
        port=args.port,
        log_level="warning",
        server_header=False,
        proxy_headers=False,
    )
    return 0
