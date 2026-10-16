"""Stateful stand-in for the ShortsMaker Platform API, used only by the e2e tests.

Tokens: ``good-token`` / ``other-token`` are two distinct users (``cache-token`` and ``outage-token`` are extra user-a tokens), ``inactive-token`` gets 403,
``throttled-token`` gets 429, anything else gets 401. Every authenticated call is recorded and can
be read at ``/__calls`` (and cleared with DELETE) so tests can assert what reached the backend.

Magic niches: ``boom500`` -> HTTP 500 with an internal-looking detail, ``badjson`` -> 200 with a
non-JSON body.
"""

from __future__ import annotations

import json
import os
import uuid

import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

USERS = {
    "good-token": "user-a",
    "other-token": "user-b",
    "cache-token": "user-a",  # dedicated tokens keep the MCP validation cache out of other tests
    "outage-token": "user-a",
}
CREDITS = {"user-a": 100, "user-b": 5}
JOB_COST = 10
CALLS: list[dict] = []
JOBS: dict[str, dict] = {}
IDEMPOTENT: dict[tuple[str, str], str] = {}

LIST_PATHS = {"/api/jobs", "/api/connections", "/api/posts", "/api/workflows"}


def _auth(request: Request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme != "Bearer":
        return None, JSONResponse({"detail": "Not authenticated"}, status_code=401)
    try:
        claims = jwt.decode(
            token,
            os.environ["MCP_BRIDGE_SECRET"],
            algorithms=["HS256"],
            issuer="shortsmaker-mcp",
            audience="shortsmaker-api",
        )
        if claims.get("token_use") != "mcp_bridge":
            raise jwt.InvalidTokenError("Wrong token type")
        user = claims["sub"]
    except (jwt.InvalidTokenError, KeyError):
        return None, JSONResponse({"detail": "Invalid token"}, status_code=401)
    return user, None


async def jwks(request: Request):
    return JSONResponse({"keys": [json.loads(os.environ["MCP_TEST_PUBLIC_JWK"])]})


async def calls(request: Request):
    if request.method == "DELETE":
        CALLS.clear()
        JOBS.clear()
        IDEMPOTENT.clear()
        CREDITS.update({"user-a": 100, "user-b": 5})
    return JSONResponse(CALLS)


async def create_job(request: Request, user: str, body: dict):
    niche = body.get("niche", "")
    if niche == "boom500":
        return JSONResponse({"detail": "psycopg2.OperationalError: db host 10.0.0.5 refused"}, status_code=500)
    if niche == "badjson":
        return PlainTextResponse("<html>oops</html>")
    key = (user, body.get("idempotency_key", ""))
    if key in IDEMPOTENT:
        return JSONResponse(JOBS[IDEMPOTENT[key]])
    if CREDITS[user] < JOB_COST:
        return JSONResponse({"detail": f"Insufficient credits: need {JOB_COST}, have {CREDITS[user]}"}, status_code=402)
    CREDITS[user] -= JOB_COST
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        # The real API reports queued jobs as "pending"; a "failed-" niche stands in for a failure.
        "status": "failed" if niche.startswith("failed-") else "ready" if niche.startswith("ready-") else "pending",
        "created_at": f"2026-10-01T00:00:{len(JOBS):02d}+00:00",
        "niche": niche,
        "format": body.get("format"),
        "credits_cost": JOB_COST,
        "owner": user,
        "config": {"huge": "x" * 1000},  # must be stripped by the MCP job summary
    }
    JOBS[job_id] = job
    IDEMPOTENT[key] = job_id
    # The real API answers a new submit with just these three; a replayed key gets the whole row.
    return JSONResponse({"id": job_id, "status": "queued", "credits_cost": JOB_COST})


async def api(request: Request):
    user, error = _auth(request)
    if error:
        return error
    path = request.url.path
    body = None
    if request.method in {"POST", "PATCH", "PUT"}:
        raw = await request.body()
        body = json.loads(raw) if raw else None
    CALLS.append(
        {
            "user": user,
            "method": request.method,
            "path": path,
            "query": dict(request.query_params),
            "body": body,
        }
    )

    if path == "/api/features":
        return JSONResponse({"promo_end_card": False})
    if path == "/api/payments/credit-status":
        return JSONResponse({"balance": CREDITS[user]})
    if path == "/api/publishing/health":
        return JSONResponse({"worker_alive": True, "worker_platforms": ["youtube"], "scheduler_alive": True})
    if path == "/api/config" and request.method == "GET":
        return JSONResponse({
            "formats": [{"id": value, "label": value} for value in ("auto", "transformation", "asmr")],
            "modes": [{"id": "lite", "label": "Standard"}],
            "resolutions": ["720p", "1080p"],
            "resolutions_by_format": {
                "transformation": {"available": ["720p", "1080p"], "default": "720p"},
                "asmr": {"available": ["1080p"], "default": "1080p"},
            },
            "styles": [],
            "styles_by_format": {"transformation": {"available": []}, "asmr": {"available": []}},
            "audio": {"modes": [], "by_format": {}, "voices": [], "music_tracks": []},
        })
    if path == "/api/connections" and request.method == "GET":
        return JSONResponse([{"id": "00000000-0000-0000-0000-000000000001", "platform": "youtube",
                              "account_name": "Test channel", "status": "active"}])
    if path == "/api/posts" and request.method == "GET":
        return JSONResponse([{"id": "00000000-0000-0000-0000-000000000001", "status": "scheduled",
                              "platform": "youtube", "account_name": "Test channel"}])
    if path == "/api/workflows" and request.method == "GET":
        return JSONResponse([{"id": "00000000-0000-0000-0000-000000000001", "name": "Test series",
                              "theme": "Test theme", "status": "active", "timezone": "UTC"}])
    if path == "/api/workflows/00000000-0000-0000-0000-000000000001/slots" and request.method == "GET":
        return JSONResponse([{"id": "00000000-0000-0000-0000-000000000001",
                              "workflow_id": "00000000-0000-0000-0000-000000000001",
                              "title": "Test idea", "status": "planned"}])
    if path.startswith("/api/posts/") and path.count("/") == 3 and request.method == "GET":
        return JSONResponse({"id": path.rsplit("/", 1)[1], "status": "scheduled",
                             "platform": "youtube", "account_name": "Test channel"})
    if path == "/api/resolve":
        # Like the engine: lengths over 60s snap to 60, keeping what was asked in requested_length.
        asked = float(request.query_params.get("total_length", 12))
        niche = request.query_params.get("niche", "")
        requested_format = request.query_params.get("format", "transformation")
        resolution = request.query_params.get("resolution", "720p")
        resolved_format = "asmr" if "marble rolling through a neon obstacle course" in niche.lower() else requested_format
        if resolved_format == "asmr" and resolution != "1080p":
            return JSONResponse({"detail": "This format doesn't offer resolution '720p'; it offers 1080p."}, status_code=422)
        return JSONResponse({"format": resolved_format, "resolution": resolution,
                             "estimated_cost": 0.10, "estimated_credits": JOB_COST, "exact": True, "mode": "lite",
                             "total_length": min(asked, 60.0), "requested_length": asked})
    if path == "/api/jobs" and request.method == "POST":
        return await create_job(request, user, body or {})
    if path.startswith("/api/jobs/") and path.endswith("/retry") and request.method == "POST":
        # Like the real API: a failed or cancelled video is charged again, a paused one is free.
        job = JOBS.get(path.split("/")[3])
        if not job or job["owner"] != user:
            return JSONResponse({"detail": "Job not found"}, status_code=404)
        if job["status"] not in ("failed", "cancelled", "paused"):
            return JSONResponse({"detail": f"Cannot resume a job that is {job['status']}"}, status_code=409)
        if job["status"] != "paused":
            CREDITS[user] -= job["credits_cost"]
        job["status"] = "pending"
        return JSONResponse({"status": "queued"})
    if path == "/api/jobs" and request.method == "GET":
        # newest first, like the real backend
        return JSONResponse([j for j in reversed(JOBS.values()) if j["owner"] == user])
    if path.startswith("/api/jobs/") and path.count("/") == 3 and request.method == "GET":
        job = JOBS.get(path.rsplit("/", 1)[1])
        if not job or job["owner"] != user:
            return JSONResponse({"detail": "Job not found"}, status_code=404)
        return JSONResponse(job)
    if request.method == "GET" and path in LIST_PATHS or path.endswith("/slots"):
        return JSONResponse([])
    return JSONResponse({"ok": True, "path": path, "method": request.method})


app = Starlette(
    routes=[
        Route("/__calls", calls, methods=["GET", "DELETE"]),
        Route("/oauth/jwks", jwks, methods=["GET"]),
        Route("/api/{rest:path}", api, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
    ]
)
