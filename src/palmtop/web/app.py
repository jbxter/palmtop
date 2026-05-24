"""Starlette ASGI app — public web presence.

SECURITY ARCHITECTURE:
  Public internet → Cloudflare Tunnel → this app → WebAgent (sandboxed)

  The WebAgent is completely isolated from the internal AgentLoop.
  It uses the cloud LLM for conversation but has NO access to tools,
  memory stores, or any internal systems.  See web/agent.py for details.

Endpoints:
  GET  /              → landing page (static HTML)
  GET  /api/health    → health check for Cloudflare Tunnel
  POST /api/chat      → chat with the agent (SSE response)
  POST /api/intake    → intake form submission
  GET  /static/...    → static assets (CSS, JS, images)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Awaitable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from palmtop.persona import PersonaConfig
from palmtop.web.agent import WebAgent
from palmtop.web.blog import list_posts, load_post, render_blog_index, render_post_page
from palmtop.web.outreach import LeadInfo, LeadOutreach
from palmtop.web.ratelimit import RateLimiter
from palmtop.web.sanitize import is_suspicious

log = logging.getLogger(__name__)

# Session ID format: UUID v4 only
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

STATIC_DIR = Path(__file__).parent / "static"


def _client_ip(request: Request) -> str:
    """Extract client IP, respecting Cloudflare headers."""
    # CF-Connecting-IP is set by Cloudflare Tunnel
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(
    agent: WebAgent,
    rate_limiter: RateLimiter,
    notify_fn: Callable[[str, str], Awaitable[None]] | None = None,
    notify_user_id: str = "",
    allowed_origin: str = "",
    outreach: LeadOutreach | None = None,
    persona: PersonaConfig | None = None,
) -> Starlette:
    """Create the ASGI app with all routes and middleware."""
    p = persona or PersonaConfig()

    # ── Landing pages ──────────────────────────────────────────────
    async def homepage(request: Request) -> Response:
        index = STATIC_DIR / "index.html"
        headers = {"Cache-Control": "public, max-age=60"}
        if index.exists():
            return HTMLResponse(index.read_text(), headers=headers)
        return HTMLResponse(f"<h1>{p.name}</h1><p>Coming soon.</p>")

    # ── Health check ──────────────────────────────────────────────
    async def health(request: Request) -> Response:
        return JSONResponse({
            "ok": True,
            "sessions": agent.active_sessions,
        })

    # ── Chat endpoint (SSE) ───────────────────────────────────────
    async def chat(request: Request) -> Response:
        # Parse body
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        message = (body.get("message") or "").strip()
        session_id = (body.get("session_id") or "").strip()

        if not message:
            return JSONResponse({"error": "Empty message"}, status_code=400)

        # Validate session ID format (must be UUID v4)
        if not session_id or not _UUID_RE.match(session_id):
            return JSONResponse({"error": "Invalid session_id"}, status_code=400)

        ip = _client_ip(request)

        # Rate limiting
        if not rate_limiter.check_chat(ip):
            return JSONResponse(
                {"error": "Rate limit exceeded.  Please wait a moment."},
                status_code=429,
            )

        if not rate_limiter.acquire_stream():
            return JSONResponse(
                {"error": "Server is busy.  Please try again shortly."},
                status_code=503,
            )

        # Log suspicious input (but still process it — the WebAgent's
        # system prompt handles adversarial inputs gracefully)
        if is_suspicious(message):
            log.warning("Suspicious input from %s: %s", ip, message[:100])

        async def event_stream():
            try:
                async for event, data in agent.handle_stream(message, session_id):
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
            except Exception:
                log.exception("Chat stream error for session %s", session_id[:8])
                yield f"event: error\ndata: {json.dumps('Something went wrong.')}\n\n"
            finally:
                rate_limiter.release_stream()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx/proxy buffering
            },
        )

    # ── Intake form ───────────────────────────────────────────────
    async def intake(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        ip = _client_ip(request)
        if not rate_limiter.check_form(ip):
            return JSONResponse(
                {"error": "Rate limit exceeded."},
                status_code=429,
            )

        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        project = (body.get("project") or "").strip()

        if not name or not email or not project:
            return JSONResponse(
                {"error": "Name, email, and project description are required."},
                status_code=400,
            )

        # Basic email format check
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return JSONResponse({"error": "Invalid email format."}, status_code=400)

        budget = (body.get("budget") or "").strip()
        timeline = (body.get("timeline") or "").strip()
        referral = (body.get("referral") or "").strip()

        source = p.domain or "website"
        log.info("Intake form: %s <%s> — %s", name, email, project[:80])

        if notify_fn and notify_user_id:
            summary = (
                f"\U0001f4e5 **New lead from {source}**\n\n"
                f"**Name:** {name}\n"
                f"**Email:** {email}\n"
                f"**Project:** {project[:500]}\n"
            )
            if budget:
                summary += f"**Budget:** {budget}\n"
            if timeline:
                summary += f"**Timeline:** {timeline}\n"
            if referral:
                summary += f"**Referral:** {referral}\n"

            try:
                await notify_fn(notify_user_id, summary)
            except Exception:
                log.warning("Failed to send lead notification", exc_info=True)

        # Fire-and-forget: qualify lead and send outreach email
        if outreach:
            lead = LeadInfo(
                name=name, email=email, project=project,
                budget=budget, timeline=timeline, referral=referral,
            )
            asyncio.create_task(outreach.process_lead(lead))

        owner = p.owner_name or "We"
        return JSONResponse({
            "ok": True,
            "message": f"Thanks!  {owner} will be in touch within 24 hours.",
        })

    # ── CORS middleware ───────────────────────────────────────────
    async def cors_middleware(request: Request, call_next):
        origin = allowed_origin or "*"
        # Handle preflight
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "86400",
                },
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = origin
        return response

    # ── HTTPS redirect middleware ────────────────────────────────
    async def https_redirect(request: Request, call_next):
        """Redirect HTTP → HTTPS using Cloudflare's X-Forwarded-Proto header."""
        proto = request.headers.get("x-forwarded-proto", "https")
        if proto == "http":
            url = request.url.replace(scheme="https")
            from starlette.responses import RedirectResponse
            return RedirectResponse(str(url), status_code=301)
        return await call_next(request)

    # ── Security headers middleware ───────────────────────────────
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    # ── Background cleanup task ───────────────────────────────────
    async def _cleanup_loop():
        """Periodic cleanup of expired sessions and rate limit buckets."""
        while True:
            await asyncio.sleep(3600)  # every hour
            try:
                agent.cleanup_expired()
                rate_limiter.cleanup()
            except Exception:
                log.debug("Cleanup error", exc_info=True)

    # ── Lifespan (startup/shutdown) ───────────────────────────────
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        cleanup_task = asyncio.create_task(_cleanup_loop())
        log.info("Web app started — serving %s", p.domain or "localhost")
        yield
        cleanup_task.cancel()
        log.info("Web app shutting down")

    # ── Blog ─────────────────────────────────────────────────────────
    async def blog_index(request: Request) -> Response:
        posts = list_posts()
        headers = {"Cache-Control": "public, max-age=60"}
        return HTMLResponse(render_blog_index(posts, persona=p), headers=headers)

    async def blog_post(request: Request) -> Response:
        slug = request.path_params["slug"]
        post = load_post(slug, persona=p)
        if not post:
            return HTMLResponse("<h1>Post not found</h1>", status_code=404)
        headers = {"Cache-Control": "public, max-age=60"}
        return HTMLResponse(render_post_page(post, persona=p), headers=headers)

    # ── SEO / GEO files (served at root) ────────────────────────────
    async def robots_txt(request: Request) -> Response:
        path = STATIC_DIR / "robots.txt"
        if path.exists():
            return Response(path.read_text(), media_type="text/plain")
        return Response("User-agent: *\nAllow: /\n", media_type="text/plain")

    async def sitemap_xml(request: Request) -> Response:
        path = STATIC_DIR / "sitemap.xml"
        if path.exists():
            return Response(path.read_text(), media_type="application/xml")
        return Response("", status_code=404)

    async def llms_txt(request: Request) -> Response:
        path = STATIC_DIR / "llms.txt"
        if path.exists():
            return Response(path.read_text(), media_type="text/plain")
        return Response("", status_code=404)

    # ── Assemble the app ──────────────────────────────────────────
    routes = [
        Route("/", homepage),
        Route("/blog", blog_index),
        Route("/blog/{slug:path}", blog_post),
        Route("/robots.txt", robots_txt),
        Route("/sitemap.xml", sitemap_xml),
        Route("/llms.txt", llms_txt),
        Route("/api/health", health),
        Route("/api/chat", chat, methods=["POST", "OPTIONS"]),
        Route("/api/intake", intake, methods=["POST", "OPTIONS"]),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    from starlette.middleware.base import BaseHTTPMiddleware

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[
            Middleware(BaseHTTPMiddleware, dispatch=https_redirect),
            Middleware(BaseHTTPMiddleware, dispatch=security_headers),
            Middleware(BaseHTTPMiddleware, dispatch=cors_middleware),
        ],
    )

    return app
