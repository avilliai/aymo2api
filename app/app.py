import asyncio
import logging
import os
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.services.token_manager import token_manager
from app.api.v1 import chat, models, admin, anthropic
from app.core.exceptions import register_exception_handlers
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await token_manager.load(refresh=False)
    app.state.token_manager = token_manager

    try:
        keep_alive_minutes = max(1, int(os.getenv("KEEP_ALIVE_MINUTES", "30")))
    except (TypeError, ValueError):
        keep_alive_minutes = 30

    startup_refresh_task = None
    if (
        token_manager.tokens
        and os.getenv("REFRESH_ON_START", "false").lower() in {"1", "true", "yes", "on"}
    ):
        startup_refresh_task = token_manager._track_background(
            token_manager.refresh_all(), name="AymoStartupRefresh"
        )
    keep_alive_task = asyncio.create_task(
        token_manager.keep_alive_loop(interval_minutes=keep_alive_minutes)
    )

    # In Outlook mode, automatically ensure default accounts are available
    if os.getenv("EMAIL_MODE", "outlook").lower() == "outlook":
        from app.services.register import register_batch_outlook
        default_outlook_count = int(os.getenv("DEFAULT_OUTLOOK_ACCOUNTS", "10"))
        usable = sum(1 for t in token_manager.tokens if not t.get("disabled") and t.get("is_alive"))
        if usable < default_outlook_count:
            need = default_outlook_count - usable
            logger.info("Outlook ????????? (%d/%d)??????????? %d ???...", usable, default_outlook_count, need)
            token_manager._track_background(register_batch_outlook(count=need), name="AymoStartupOutlookRegister")

    logger.info("aymo2api ??????")
    yield

    # Shutdown
    await token_manager.cancel_background()
    if startup_refresh_task is not None:
        if not startup_refresh_task.done():
            startup_refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await startup_refresh_task
    keep_alive_task.cancel()
    with suppress(asyncio.CancelledError):
        await keep_alive_task
    await token_manager.flush()
    # In Outlook mode, automatically ensure default accounts are available
    if os.getenv("EMAIL_MODE", "outlook").lower() == "outlook":
        from app.services.register import register_batch_outlook
        default_outlook_count = int(os.getenv("DEFAULT_OUTLOOK_ACCOUNTS", "10"))
        usable = sum(1 for t in token_manager.tokens if not t.get("disabled") and t.get("is_alive"))
        if usable < default_outlook_count:
            need = default_outlook_count - usable
            logger.info("Outlook ????????? (%d/%d)??????????? %d ???...", usable, default_outlook_count, need)
            token_manager._track_background(register_batch_outlook(count=need), name="AymoStartupOutlookRegister")

    logger.info("aymo2api ??????")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Aymo2API",
        description="Aymo.ai to OpenAI and Anthropic compatible API",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(chat.router,      prefix="/v1")
    app.include_router(models.router,    prefix="/v1")
    app.include_router(admin.router,     prefix="/admin")
    app.include_router(anthropic.router, prefix="/v1")

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok"}

    register_exception_handlers(app)

    return app
