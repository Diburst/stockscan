"""The one Refresh button.

``POST /refresh`` starts the pipeline in the background (or joins the run
already in flight) and returns the strip in its running state; the strip
polls ``GET /refresh/status`` every two seconds until the run finishes,
then renders the result and fires ``refresh-done`` so the Dashboard
reloads its cards. There is no cooldown: the pipeline is idempotent, so a
second click after a finished run costs nothing but a few local queries.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from stockscan.jobs import background
from stockscan.web.deps import render

router = APIRouter(prefix="/refresh", tags=["refresh"])


def strip_context() -> dict[str, object]:
    """Template context for ``_refresh_strip.html``; the Dashboard uses it
    on initial render so a run started elsewhere shows immediately."""
    job = background.current()
    return {"job": job, "running": job is not None and job.running}


@router.post("")
def start(request: Request):
    background.start()
    return render(request, "_refresh_strip.html", **strip_context())


@router.get("/status")
def status(request: Request, was_running: bool = False):
    """Polled by the strip while a run is in flight. ``was_running`` is
    what the polling strip knew; when the run has since finished the
    response also tells the Dashboard to reload its cards."""
    ctx = strip_context()
    response = render(request, "_refresh_strip.html", **ctx)
    if was_running and not ctx["running"]:
        response.headers["HX-Trigger"] = "refresh-done"
    return response
