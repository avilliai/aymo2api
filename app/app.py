import asyncio
import logging
import os
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.services.token_manager import token_manager
from app.services.outlook_email_service import OutlookEmailService
from app.api.v1 import chat, models, admin, anthropic
from app.core.exceptions import register_exception_handlers
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 清洗并解封历史因 Turnstile Solver 掉线或网络临时故障误标为 failed 的候选邮箱
    try:
        clean_res = OutlookEmailService().clean_and_recover_status()
        if clean_res.get("recovered") or clean_res.get("converted"):
            logger.info("已自动清洗解封历史失败邮箱: %s", clean_res)
    except Exception as exc:
        logger.debug("自动清洗邮箱状态异常: %s", exc)

    # 2. 加载账号池（不进行全量探查）
    await token_manager.load(refresh=False)
    app.state.token_manager = token_manager

    # 3. 批量保活控制：默认彻底关闭，杜绝批量封号
    keep_alive_enabled = os.getenv("KEEP_ALIVE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    keep_alive_task = None
    if keep_alive_enabled:
        try:
            keep_alive_minutes = max(1, int(os.getenv("KEEP_ALIVE_MINUTES", "30")))
        except (TypeError, ValueError):
            keep_alive_minutes = 30
        logger.warning("已启用保活任务（采用单账号大间隔温和探查）")
        keep_alive_task = asyncio.create_task(
            token_manager.keep_alive_loop(interval_minutes=keep_alive_minutes)
        )
    else:
        logger.info("批量保活已禁用 (KEEP_ALIVE_ENABLED=false)，杜绝批量封号风险，账号采用按需探活/OTP自愈机制。")

    # 4. 在 Outlook 模式下，按需确保基础账号数量
    if os.getenv("EMAIL_MODE", "outlook").lower() == "outlook":
        from app.services.register import register_batch_outlook
        default_outlook_count = int(os.getenv("DEFAULT_OUTLOOK_ACCOUNTS", "10"))
        usable = sum(1 for t in token_manager.tokens if not t.get("disabled") and t.get("is_alive"))
        if usable < default_outlook_count:
            need = default_outlook_count - usable
            logger.info("Outlook 可用账号不足 (%d/%d)，正在后台补充注册 %d 个账号...", usable, default_outlook_count, need)
            token_manager._track_background(register_batch_outlook(count=need), name="AymoStartupOutlookRegister")

    logger.info("aymo2api 服务启动成功")
    yield

    # Shutdown
    await token_manager.cancel_background()
    if keep_alive_task is not None and not keep_alive_task.done():
        keep_alive_task.cancel()
        with suppress(asyncio.CancelledError):
            await keep_alive_task
    await token_manager.flush()
    logger.info("aymo2api 服务关闭完成")


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
