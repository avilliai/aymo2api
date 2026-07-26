"""
管理接口：
  GET    /admin/tokens              查看 Token 池状态摘要
  GET    /admin/tokens/detail       每个 Token 的脱敏详情（供前端用）
  POST   /admin/tokens/reload       热重载 Token 文件
  POST   /admin/tokens/refresh      立即触发一次全量检测

  POST   /admin/tokens/add          批量添加 Token
  DELETE /admin/tokens/{id}         永久删除单个 Token
  POST   /admin/tokens/{id}/refresh 刷新单个 Token 状态
  POST   /admin/tokens/bulk/delete  批量永久删除
  POST   /admin/tokens/bulk/disable 批量禁用（不删除，不参与调度）
  POST   /admin/tokens/bulk/enable  批量启用
  POST   /admin/tokens/bulk/refresh 批量刷新（仅刷新选中的 Token）
  POST   /admin/register/stop      请求停止注册任务
"""

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core.auth import verify_api_key
from app.core.dependencies import get_token_manager
from app.services.register import RegistrationError, registration_service
from app.services.token_manager import TokenManager

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 请求体模型 ──────────────────────────────────────────────────────────────

class TokenItem(BaseModel):
    email: str = ""
    password: str = ""
    token: str = ""
    session_token: str = ""
    user_id: str = ""
    org_id: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    inbox_token: str = ""
    proxy: str = ""

class AddTokensBody(BaseModel):
    tokens: list[TokenItem]

class BulkIdsBody(BaseModel):
    indexes: list[int]   # 前端传的字段名是 indexes，对应后端 token id


# ── 只读接口 ────────────────────────────────────────────────────────────────

@router.get(
    "/tokens",
    summary="Token Pool Summary",
    dependencies=[Depends(verify_api_key)],
)
async def token_summary(tm: TokenManager = Depends(get_token_manager)):
    return tm.summary()


@router.get(
    "/tokens/detail",
    summary="Token Detail (masked)",
    dependencies=[Depends(verify_api_key)],
)
async def token_detail(tm: TokenManager = Depends(get_token_manager)):
    return {"summary": tm.summary(), "tokens": tm.detail()}


# ── 全量操作 ────────────────────────────────────────────────────────────────

@router.post(
    "/tokens/reload",
    summary="Hot-reload token file",
    dependencies=[Depends(verify_api_key)],
)
async def reload_tokens(tm: TokenManager = Depends(get_token_manager)):
    # Reload the local file immediately, then probe in the background so a
    # large account pool does not make the dashboard request time out.
    await tm.load(refresh=False)
    refresh_scheduled = False
    if (
        tm.tokens
        and os.getenv("REFRESH_ON_START", "true").lower() in {"1", "true", "yes", "on"}
    ):
        tm._track_background(tm.refresh_all(), name="NottoReloadRefresh")
        refresh_scheduled = True
    return {"status": "ok", "refresh_scheduled": refresh_scheduled, **tm.summary()}


@router.post(
    "/tokens/refresh",
    summary="Force-refresh ALL token statuses",
    dependencies=[Depends(verify_api_key)],
)
async def refresh_all_tokens(tm: TokenManager = Depends(get_token_manager)):
    await tm.refresh_all()
    return {"status": "ok", **tm.summary()}


# ── 添加 ────────────────────────────────────────────────────────────────────

@router.post(
    "/tokens/add",
    summary="Add tokens (dedup, persist, async probe)",
    dependencies=[Depends(verify_api_key)],
)
async def add_tokens(
    body: AddTokensBody,
    tm: TokenManager = Depends(get_token_manager),
):
    result = await tm.add(
        [t.model_dump() for t in body.tokens],
        probe=True,
        wait_for_probe=False,
    )
    return {"status": "ok", **result, **tm.summary()}


# ── 批量操作（必须在 /{token_id} 之前注册，避免路径被误匹配）────────────────

@router.post(
    "/tokens/bulk/refresh",
    summary="Refresh selected tokens only (not all)",
    dependencies=[Depends(verify_api_key)],
)
async def bulk_refresh(
    body: BulkIdsBody,
    tm: TokenManager = Depends(get_token_manager),
):
    await tm.refresh_many(body.indexes)
    return {"status": "ok", **tm.summary()}


@router.post(
    "/tokens/bulk/delete",
    summary="Bulk delete tokens permanently",
    dependencies=[Depends(verify_api_key)],
)
async def bulk_delete(
    body: BulkIdsBody,
    tm: TokenManager = Depends(get_token_manager),
):
    removed = tm.remove_many(body.indexes)
    return {"status": "ok", "removed": removed, **tm.summary()}


@router.post(
    "/tokens/bulk/disable",
    summary="Bulk disable tokens (keep in file, skip scheduling)",
    dependencies=[Depends(verify_api_key)],
)
async def bulk_disable(
    body: BulkIdsBody,
    tm: TokenManager = Depends(get_token_manager),
):
    count = tm.disable(body.indexes)
    return {"status": "ok", "disabled": count, **tm.summary()}


@router.post(
    "/tokens/bulk/enable",
    summary="Bulk enable tokens (re-enable and re-probe)",
    dependencies=[Depends(verify_api_key)],
)
async def bulk_enable(
    body: BulkIdsBody,
    tm: TokenManager = Depends(get_token_manager),
):
    count = tm.enable(body.indexes)
    return {"status": "ok", "enabled": count, **tm.summary()}


# ── 单条操作 ────────────────────────────────────────────────────────────────

@router.post(
    "/tokens/{token_id}/refresh",
    summary="Refresh a single token status",
    dependencies=[Depends(verify_api_key)],
)
async def refresh_one(
    token_id: int,
    tm: TokenManager = Depends(get_token_manager),
):
    entry = await tm.refresh_one(token_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Token id={token_id} not found")
    return {
        "status": "ok",
        "token": {
            "index":      entry["id"],
            "token_hint": (
                f"...{entry['session_token'][-8:]}"
                if len(entry["session_token"]) > 8
                else entry["session_token"]
            ),
            "is_alive":   entry["is_alive"] and not entry["disabled"],
            "remaining":  entry["remaining"],
            "disabled":   entry["disabled"],
        },
    }


@router.delete(
    "/tokens/{token_id}",
    summary="Permanently delete a single token",
    dependencies=[Depends(verify_api_key)],
)
async def delete_one(
    token_id: int,
    tm: TokenManager = Depends(get_token_manager),
):
    ok = tm.remove(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Token id={token_id} not found")
    return {"status": "ok", **tm.summary()}


# ── Dashboard UI ────────────────────────────────────────────────────────────

@router.get(
    "/dashboard",
    summary="Token Dashboard UI",
    response_class=HTMLResponse,
)
async def dashboard():
    html_path = Path(__file__).with_name("dashboard.html")
    content = await asyncio.to_thread(html_path.read_text, encoding="utf-8")
    # Avoid a browser/CDN retaining an older mojibake page after a dashboard
    # update.  HTMLResponse emits UTF-8 with an explicit charset; the no-store
    # header makes the fix visible immediately.
    return HTMLResponse(
        content=content,
        media_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )

class StartRegistrationBody(BaseModel):
    count: int = Field(..., gt=0, description="目标注册账号数量，必须大于 0")
    backend: str | None = Field(default=None, description="auto、camoufox 或 http")
    captcha_token: str | None = Field(default=None, description="HTTP 后端使用的 Turnstile token")

@router.post(
    "/register/start",
    summary="Start registration tasks",
    dependencies=[Depends(verify_api_key)],
)
async def start_registration(
    body: StartRegistrationBody,
    tm: TokenManager = Depends(get_token_manager),
):
    try:
        state = await registration_service.start(
            body.count,
            tm,
            backend=body.backend,
            captcha_token=body.captcha_token,
        )
        return {
            "status": "ok",
            "message": f"注册任务已启动，目标账号数：{body.count}",
            "job": state,
        }
    except RegistrationError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.exception("启动注册任务失败")
        raise HTTPException(status_code=500, detail=f"启动失败：{e}")


@router.get(
    "/register/status",
    summary="Registration job status",
    dependencies=[Depends(verify_api_key)],
)
async def registration_status():
    return {"status": "ok", "job": registration_service.status()}


@router.post(
    "/register/stop",
    summary="Request registration job stop",
    dependencies=[Depends(verify_api_key)],
)
async def stop_registration():
    return {"status": "ok", "job": await registration_service.stop()}
