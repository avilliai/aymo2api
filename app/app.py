"""
FastAPI 应用工厂：负责注册路由、挂载中间件和生命周期管理。
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.services.token_manager import TokenManager
from app.api.v1 import chat, models, admin, anthropic
from app.core.exceptions import register_exception_handlers
from dotenv import load_dotenv
load_dotenv()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 启动阶段 ──────────────────────────────────────────
    token_manager = TokenManager(file_path=os.getenv("TOKEN_FILE", "data/accounts.json"))
    await token_manager.load(refresh=False)
    app.state.token_manager = token_manager

    try:
        keep_alive_minutes = max(1, int(os.getenv("KEEP_ALIVE_MINUTES", "30")))
    except (TypeError, ValueError):
        keep_alive_minutes = 30
    startup_refresh_task = None
    if (
        token_manager.tokens
        and os.getenv("REFRESH_ON_START", "true").lower() in {"1", "true", "yes", "on"}
    ):
        startup_refresh_task = token_manager._track_background(
            token_manager.refresh_all(), name="AymoStartupRefresh"
        )
    keep_alive_task = asyncio.create_task(
        token_manager.keep_alive_loop(interval_minutes=keep_alive_minutes)
    )

    logger.info("aymo2api 启动完成")
    yield

    # ── 关闭阶段 ──────────────────────────────────────────
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
    logger.info("aymo2api 已关闭。")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Aymo2API",
        description="将 aymo.ai 服务包装为 OpenAI 与 Anthropic 兼容 API",
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
