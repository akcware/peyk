"""FastAPI gateway: /health and POST /webhook/composio. Verifies signature, writes one observation, done."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from adapters.composio.webhook import (
    WebhookSignatureError,
    parse_envelope,
    to_observation,
    verify_signature,
)
from core import db
from core.config import get_settings
from core.log import configure_logging, get_logger
from core.repo import observation_repo

log = get_logger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    await db.open_pool(app.state.database_url or settings.DATABASE_URL)
    yield
    await db.close_pool()


def create_app(database_url: str | None = None) -> FastAPI:
    app = FastAPI(title="proactive-agent gateway", lifespan=lifespan)
    app.state.database_url = database_url

    @app.get("/health")
    async def health() -> dict:
        async with db.connection() as conn:
            await conn.execute("select 1")
        return {"ok": True}

    @app.post("/webhook/composio")
    async def composio_webhook(request: Request) -> Response:
        settings = get_settings()
        raw = (await request.body()).decode()
        try:
            verify_signature(
                webhook_id=request.headers.get("webhook-id", ""),
                timestamp=request.headers.get("webhook-timestamp", ""),
                body=raw,
                signature=request.headers.get("webhook-signature", ""),
                secret=settings.COMPOSIO_WEBHOOK_SECRET,
            )
        except WebhookSignatureError as e:
            log.warning("webhook.rejected", reason=str(e))
            return Response(status_code=401)
        event = parse_envelope(raw)
        obs = to_observation(event, settings.USER_ID)
        if obs is None:
            return Response(status_code=200, content='{"stored":false,"reason":"unknown_trigger"}', media_type="application/json")
        async with db.connection() as conn:
            stored = await observation_repo.insert(conn, obs)
        log.info("webhook.stored", source=obs.source, source_key=obs.source_key, duplicate=stored is None)
        return Response(
            status_code=200,
            content=f'{{"stored":{"true" if stored else "false"}}}',
            media_type="application/json",
        )

    return app


app = create_app()
