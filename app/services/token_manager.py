"""Aymo.ai account pool: better-auth session persistence, credit tracking and auto-replenishment.

Unlike Clerk, aymo.ai uses better-auth with an opaque rolling session cookie
`__Secure-better-auth.session_token`. Credits are a per-day allowance surfaced
by `/backend-api/users/me/today-usage`.

When tokens/credits are exhausted:
  - Existing accounts are NOT deleted (Aymo credits refresh daily).
  - Background auto-replenishment registers fresh Outlook accounts.
  - Active accounts rotate; exhausted accounts remain available for daily quota refresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv
from fastapi import HTTPException

from app.services.turnstile import AYMO_SITEKEY, AsyncTurnstileService

load_dotenv()
logger = logging.getLogger(__name__)

APP_BASE_URL = os.getenv("AYMO_BASE_URL", "https://aymo.ai").rstrip("/")
BACKEND_API = os.getenv("AYMO_BACKEND_API", f"{APP_BASE_URL}/backend-api").rstrip("/")
AUTH_BASE = f"{BACKEND_API}/auth"
SESSION_COOKIE = os.getenv("AYMO_SESSION_COOKIE", "__Secure-better-auth.session_token")

USER_AGENT = os.getenv(
    "AYMO_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
IMPERSONATE = os.getenv("AYMO_IMPERSONATE", "chrome131")


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


def _env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


REFRESH_CONCURRENCY = _env_int("REFRESH_CONCURRENCY", 5, 1)
MAX_PROBE_RETRIES = _env_int("MAX_PROBE_RETRIES", 2, 1)
PROBE_RETRY_DELAY = _env_float("PROBE_RETRY_DELAY", 1.0, 0.1)
HTTP_TIMEOUT = _env_int("UPSTREAM_TIMEOUT", 120, 10)


class AuthSessionError(RuntimeError):
    """Raised when an account cannot obtain a usable aymo.ai session."""


def _as_int(value: Any, default: int = 0, *, minimum: int | None = None) -> int:
    try:
        parsed = int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def _cookie_dict(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if k and v is not None}
    result: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = str(item.get("value", ""))
    return result


class TokenManager:
    """Round-robin aymo.ai account pool with persistent better-auth cookies and auto-replenishment."""

    def __init__(self, file_path: str = "data/accounts.json") -> None:
        self.file_path = Path(file_path)
        self.tokens: list[dict[str, Any]] = []
        self.current_index = 0
        self._next_id = 1
        self.lock = asyncio.Lock()
        self._file_lock = asyncio.Lock()
        self._entry_locks: dict[int, asyncio.Lock] = {}
        self._pending_write_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()

        # Auto-replenishment settings
        self.min_standby_threshold = _env_int("MIN_STANDBY_THRESHOLD", 2, 0)
        self.auto_replenish_batch = _env_int("AUTO_REPLENISH_BATCH", 2, 1)
        self._replenishing = False
        self._last_replenish_attempt = 0.0
        self._replenish_cooldown = _env_int("REPLENISH_COOLDOWN", 60, 10)

    def _make_entry(self, item: dict[str, Any]) -> dict[str, Any]:
        raw_cookies = _cookie_dict(item.get("cookies"))
        token = str(
            item.get("session_token")
            or item.get("token")
            or raw_cookies.get(SESSION_COOKIE)
            or ""
        ).strip()
        if token and SESSION_COOKIE not in raw_cookies:
            raw_cookies[SESSION_COOKIE] = token
        entry_id = _as_int(item.get("id"), self._next_id, minimum=1)
        self._next_id = max(self._next_id, entry_id + 1)
        credits = item.get("credits", item.get("remaining", 0))
        max_credits = item.get("max_credits", item.get("maxCredits", 20))
        entry = {
            "id": entry_id,
            "email": str(item.get("email") or "").strip(),
            "password": str(item.get("password") or ""),
            "client_id": str(item.get("client_id") or ""),
            "refresh_token": str(item.get("refresh_token") or ""),
            "user_id": str(item.get("user_id") or ""),
            "org_id": str(item.get("org_id") or item.get("organization_id") or ""),
            "session_token": token,
            "cookies": raw_cookies,
            "inbox_token": str(item.get("inbox_token") or item.get("email_token") or ""),
            "proxy": str(item.get("proxy") or ""),
            "tier": str(item.get("tier") or item.get("plan") or "free"),
            "status": str(item.get("status") or "free"),
            "remaining": _as_int(credits, 0, minimum=0),
            "max_credits": _as_int(max_credits, 20, minimum=0),
            "daily_limit": _as_int(item.get("daily_limit"), 20, minimum=0),
            "daily_used": _as_int(item.get("daily_used"), 0, minimum=0),
            "daily_remaining": _as_int(item.get("daily_remaining"), _as_int(credits, 0), minimum=0),
            "monthly_limit": _as_int(item.get("monthly_limit"), 50, minimum=0),
            "monthly_used": _as_int(item.get("monthly_used"), 0, minimum=0),
            "monthly_remaining": _as_int(item.get("monthly_remaining"), 50, minimum=0),
            "credit_reset_date": str(item.get("credit_reset_date") or ""),
            "extra_credits": _as_int(item.get("extra_credits"), 0, minimum=0),
            "message_limit": _as_int(item.get("message_limit"), 500, minimum=0),
            "is_alive": _as_bool(item.get("is_alive", item.get("healthy", True)), True),
            "disabled": _as_bool(item.get("disabled", False), False),
            "last_checked": _as_int(item.get("last_checked"), 0, minimum=0),
            "last_error": str(item.get("last_error") or ""),
            "_inflight": 0,
        }
        self._entry_locks.setdefault(entry_id, asyncio.Lock())
        return entry

    @staticmethod
    def _persistable(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": entry["id"],
            "email": entry["email"],
            "password": entry["password"],
            "client_id": entry.get("client_id", ""),
            "refresh_token": entry.get("refresh_token", ""),
            "user_id": entry.get("user_id", ""),
            "org_id": entry.get("org_id", ""),
            "session_token": entry.get("session_token", ""),
            "cookies": entry.get("cookies", {}),
            "inbox_token": entry.get("inbox_token", ""),
            "proxy": entry.get("proxy", ""),
            "tier": entry.get("tier", "free"),
            "status": entry.get("status", "free"),
            "credits": entry.get("remaining", 0),
            "max_credits": entry.get("max_credits", 20),
            "is_alive": entry.get("is_alive", True),
            "disabled": entry.get("disabled", False),
            "last_checked": entry.get("last_checked", 0),
            "last_error": entry.get("last_error", ""),
        }

    async def _write_file(self) -> None:
        async with self._file_lock:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            data = [self._persistable(entry) for entry in self.tokens]
            text = json.dumps(data, ensure_ascii=False, indent=2)
            await asyncio.to_thread(self.file_path.write_text, text, "utf-8")

    def _schedule_write(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            if self._pending_write_task and not self._pending_write_task.done():
                return
            self._pending_write_task = loop.create_task(self._delayed_write())
        except RuntimeError:
            pass

    async def _delayed_write(self) -> None:
        delay = max(0.05, float(os.getenv("TOKEN_WRITE_DEBOUNCE", "0.5")))
        await asyncio.sleep(delay)
        await self._write_file()

    async def flush(self) -> None:
        task = self._pending_write_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._write_file()

    def _track_background(self, coroutine, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("后台任务执行异常: %s", exc)

        task.add_done_callback(done)
        return task

    async def cancel_background(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def load(self, *, refresh: bool | None = None) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            await asyncio.to_thread(self.file_path.write_text, "[]", "utf-8")

        try:
            raw = json.loads(await asyncio.to_thread(self.file_path.read_text, "utf-8-sig"))
            if not isinstance(raw, list):
                raise ValueError("account file root must be a JSON array")
        except Exception as exc:
            logger.error("账号文件 %s 加载失败: %s", self.file_path, exc)
            raw = []

        seen: set[str] = set()
        used_ids: set[int] = set()
        entries: list[dict[str, Any]] = []
        self._next_id = 1
        self._entry_locks = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip().lower()
            identity = email or str(item.get("user_id") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            entry = self._make_entry(item)
            if entry["id"] in used_ids:
                while self._next_id in used_ids:
                    self._next_id += 1
                entry["id"] = self._next_id
                self._next_id += 1
                self._entry_locks.setdefault(entry["id"], asyncio.Lock())
            used_ids.add(entry["id"])
            entries.append(entry)

        async with self.lock:
            self.tokens = entries
            self.current_index = 0

        logger.info("已加载 %d 个 aymo.ai 账号", len(entries))
        should_refresh = (
            os.getenv("REFRESH_ON_START", "false").lower() in {"1", "true", "yes", "on"}
            if refresh is None
            else bool(refresh)
        )
        if entries and should_refresh:
            await self.refresh_all()

    def create_session(self, entry: dict[str, Any], timeout: int = HTTP_TIMEOUT) -> AsyncSession:
        proxy = entry.get("proxy") or os.getenv("PROXY") or None
        session = AsyncSession(impersonate=IMPERSONATE, proxy=proxy, timeout=timeout)
        cookies = entry.get("cookies") or {}
        if cookies:
            self._apply_cookies(session, cookies)
        return session

    @staticmethod
    def _apply_cookies(session: AsyncSession, cookies: dict[str, str]) -> None:
        host = (urlparse(APP_BASE_URL).hostname or "aymo.ai").lstrip(".")
        for name, value in cookies.items():
            if value is None:
                continue
            try:
                session.cookies.set(name, str(value), domain=host, path="/")
            except Exception:
                try:
                    session.cookies.update({name: str(value)})
                except Exception:
                    pass

    def _merge_session_cookies(self, entry: dict[str, Any], session: AsyncSession) -> None:
        merged = dict(entry.get("cookies") or {})
        try:
            merged.update(session.cookies.get_dict())
        except Exception:
            pass
        entry["cookies"] = merged
        token = merged.get(SESSION_COOKIE)
        if token:
            entry["session_token"] = token

    def app_headers(
        self,
        entry: dict[str, Any],
        session: AsyncSession | None = None,
        *,
        referer: str | None = None,
        json_body: bool = False,
    ) -> dict[str, str]:
        cookies = dict(entry.get("cookies") or {})
        if session is not None:
            try:
                cookies.update(session.cookies.get_dict())
            except Exception:
                pass
        if entry.get("session_token"):
            cookies[SESSION_COOKIE] = entry["session_token"]
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "origin": APP_BASE_URL,
            "referer": referer or f"{APP_BASE_URL}/chats",
            "priority": "u=1, i",
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": USER_AGENT,
            "cookie": cookie_header,
        }
        if json_body:
            headers["content-type"] = "application/json"
        return headers

    async def _get_session_user(self, entry: dict[str, Any], session: AsyncSession) -> dict[str, Any] | None:
        response = await session.get(
            f"{AUTH_BASE}/get-session",
            headers=self.app_headers(entry, session),
        )
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("user"):
            return None
        self._merge_session_cookies(entry, session)
        return data

    async def ensure_session(
        self,
        entry: dict[str, Any],
        session: AsyncSession,
        *,
        force: bool = False,
    ) -> bool:
        lock = self._entry_locks.setdefault(entry["id"], asyncio.Lock())
        async with lock:
            if not force:
                data = await self._get_session_user(entry, session)
                if data:
                    return True
            # If session is invalid, direct sign-in is disabled by aymo upstream
            # Mark is_alive = False and return False
            logger.warning("[%s] 会话已失效且不支持直接密码重登", entry.get("email"))
            return False

    async def _fetch_org_id(self, entry: dict[str, Any], session: AsyncSession) -> str:
        response = await session.get(
            f"{BACKEND_API}/organizations",
            headers=self.app_headers(entry, session),
        )
        if response.status_code != 200:
            return entry.get("org_id", "")
        try:
            data = response.json().get("data") or []
        except Exception:
            data = []
        if data:
            org = data[0]
            entry["org_id"] = str(org.get("id") or entry.get("org_id", ""))
            entry["tier"] = "paid" if org.get("isPaid") else entry.get("tier", "free")
        return entry.get("org_id", "")

    async def _fetch_credits(self, entry: dict[str, Any], session: AsyncSession) -> bool:
        headers = self.app_headers(entry, session)
        
        # 1. Fetch daily usage (/backend-api/users/me/today-usage)
        daily_used, daily_limit = 0, 20
        try:
            response = await session.get(f"{BACKEND_API}/users/me/today-usage", headers=headers)
            if response.status_code == 200:
                data = response.json().get("data") or {}
                daily_used = _as_int(data.get("creditsUsed"), 0, minimum=0)
                daily_limit = _as_int(data.get("dailyCreditLimit"), 20, minimum=0)
        except Exception as exc:
            logger.debug("Failed to fetch today-usage for %s: %s", entry.get("email"), exc)

        daily_remaining = max(0, daily_limit - daily_used)

        # 2. Fetch subscription & monthly quota (/backend-api/subscriptions/current)
        monthly_limit, monthly_used = 50, 0
        credit_reset_date = ""
        message_limit = 500
        plan_name = "Free"
        try:
            sub_resp = await session.get(f"{BACKEND_API}/subscriptions/current", headers=headers)
            if sub_resp.status_code == 200:
                sub_data = sub_resp.json() or {}
                plan = sub_data.get("currentPlan") or {}
                monthly_limit = _as_int(plan.get("credit_limit"), 50, minimum=0)
                monthly_used = _as_int(sub_data.get("usageCredits"), 0, minimum=0)
                credit_reset_date = str(sub_data.get("creditResetDate") or "")
                message_limit = _as_int(plan.get("message_limit"), 500, minimum=0)
                plan_name = str(plan.get("name") or "Free")
                if plan.get("daily_credit_limit") is not None:
                    daily_limit = _as_int(plan.get("daily_credit_limit"), daily_limit, minimum=0)
                    daily_remaining = max(0, daily_limit - daily_used)
        except Exception as exc:
            logger.debug("Failed to fetch subscription for %s: %s", entry.get("email"), exc)

        monthly_remaining = max(0, monthly_limit - monthly_used)

        # 3. Fetch extra credits (/backend-api/organizations/{orgId}/credits/list)
        extra_credits = 0
        org_id = entry.get("org_id")
        if org_id:
            try:
                extra_resp = await session.get(f"{BACKEND_API}/organizations/{org_id}/credits/list", headers=headers)
                if extra_resp.status_code == 200:
                    for pkg in (extra_resp.json().get("data") or []):
                        extra_credits += _as_int(pkg.get("remainingCredits") or pkg.get("credits"), 0, minimum=0)
            except Exception:
                pass

        # Effective usable credits today is bounded by daily remaining and monthly remaining
        effective_remaining = min(daily_remaining, monthly_remaining) + extra_credits

        entry["daily_limit"] = daily_limit
        entry["daily_used"] = daily_used
        entry["daily_remaining"] = daily_remaining
        entry["monthly_limit"] = monthly_limit
        entry["monthly_used"] = monthly_used
        entry["monthly_remaining"] = monthly_remaining
        entry["credit_reset_date"] = credit_reset_date
        entry["extra_credits"] = extra_credits
        entry["message_limit"] = message_limit
        entry["max_credits"] = daily_limit
        entry["remaining"] = effective_remaining
        entry["tier"] = plan_name.lower()
        return True

    async def _probe_once(self, entry: dict[str, Any]) -> str:
        async with self.create_session(entry, timeout=HTTP_TIMEOUT) as session:
            if not await self.ensure_session(entry, session):
                raise AuthSessionError("会话失效")
            await self._fetch_org_id(entry, session)
            if not await self._fetch_credits(entry, session):
                raise RuntimeError("配额查询失败")
            entry["is_alive"] = True
            entry["last_checked"] = int(time.time())
            entry["last_error"] = ""
            self._merge_session_cookies(entry, session)
            return "ok"

    async def _probe(self, entry: dict[str, Any]) -> str:
        previous_alive = bool(entry.get("is_alive", True))
        previous_remaining = _as_int(entry.get("remaining"), 0, minimum=0)
        last_error: Exception | None = None
        for attempt in range(1, MAX_PROBE_RETRIES + 1):
            try:
                return await self._probe_once(entry)
            except AuthSessionError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                if attempt < MAX_PROBE_RETRIES:
                    await asyncio.sleep(PROBE_RETRY_DELAY * attempt)

        error_text = str(last_error or "").lower()
        terminal = isinstance(last_error, AuthSessionError) or any(
            marker in error_text
            for marker in ("http 401", "http 403", "会话失效", "unauthorized")
        )
        entry["is_alive"] = False if terminal else previous_alive
        entry["remaining"] = 0 if terminal else previous_remaining
        entry["last_checked"] = int(time.time())
        entry["last_error"] = str(last_error or "unknown error")
        logger.warning("[%s] 账号探测失败: %s", entry.get("email"), entry["last_error"])
        return "dead" if terminal else "error"

    async def _run_refresh(
        self,
        targets: list[dict[str, Any]],
        label: str,
        concurrency: int | None = None,
    ) -> None:
        if not targets:
            return
        semaphore = asyncio.Semaphore(max(1, concurrency or REFRESH_CONCURRENCY))

        async def run(entry: dict[str, Any]) -> str:
            async with semaphore:
                return await self._probe(entry)

        results = await asyncio.gather(*(run(entry) for entry in targets))
        await self._write_file()
        logger.info(
            "%s完成: total=%d alive=%d dead=%d transient_error=%d",
            label,
            len(results),
            sum(result == "ok" for result in results),
            sum(result == "dead" for result in results),
            sum(result == "error" for result in results),
        )

    async def refresh_all(self, concurrency: int | None = None) -> None:
        await self._run_refresh(
            [entry for entry in self.tokens if not entry["disabled"]],
            "全量刷新",
            concurrency,
        )

    async def refresh_many(self, token_ids: list[int], concurrency: int | None = None) -> None:
        wanted = set(token_ids)
        await self._run_refresh(
            [entry for entry in self.tokens if entry["id"] in wanted and not entry["disabled"]],
            "批量刷新",
            concurrency,
        )

    async def refresh_one(self, token_id: int) -> dict[str, Any] | None:
        entry = self._by_id(token_id)
        if entry is None:
            return None
        await self._probe(entry)
        await self._write_file()
        return entry

    # ── Auto-replenishment ──────────────────────────────────────────────────
    def trigger_auto_replenish_if_needed(self) -> None:
        """Triggers background registration of new Outlook accounts if usable accounts are low."""
        if not _as_bool(os.getenv("AUTO_REPLENISH", "true"), True):
            return
        if self._replenishing:
            return
        now = time.time()
        if now - self._last_replenish_attempt < self._replenish_cooldown:
            return

        usable_count = sum(
            1 for entry in self.tokens
            if not entry["disabled"] and entry["is_alive"] and entry["remaining"] > 0
        )
        if usable_count <= self.min_standby_threshold:
            logger.info(
                "可用账号数量 (%d) 低于阈值 (%d)，触发后台自动注册新账号...",
                usable_count,
                self.min_standby_threshold,
            )
            self._track_background(self._auto_replenish_task(), name="AymoAutoReplenish")

    async def _auto_replenish_task(self) -> None:
        if self._replenishing:
            return
        self._replenishing = True
        self._last_replenish_attempt = time.time()
        try:
            from app.services.register import register_batch_outlook
            logger.info("开始自动注册 %d 个新 Outlook 账号...", self.auto_replenish_batch)
            new_accounts = await register_batch_outlook(count=self.auto_replenish_batch)
            logger.info("自动注册完成，新增账号数：%d", len(new_accounts))
        except Exception as exc:
            logger.warning("自动注册新账号任务异常: %s", exc)
        finally:
            self._replenishing = False

    # ── Scheduling ──────────────────────────────────────────────────────────
    async def get_next(self) -> dict[str, Any]:
        """Returns the next usable account. Never deletes exhausted accounts; triggers auto-replenish."""
        async with self.lock:
            # 1. Check if auto-replenish is needed
            self.trigger_auto_replenish_if_needed()

            if not self.tokens:
                # Trigger auto-replenish if empty
                self.trigger_auto_replenish_if_needed()
                raise HTTPException(status_code=500, detail="账号池为空，正在自动注册新账号，请稍后重试。")

            total = len(self.tokens)
            # Find an active account with remaining > 0
            for _ in range(total):
                entry = self.tokens[self.current_index % total]
                self.current_index = (self.current_index + 1) % total
                if entry["disabled"] or not entry["is_alive"] or entry["remaining"] <= 0:
                    continue
                entry["remaining"] -= 1
                entry["_inflight"] = _as_int(entry.get("_inflight"), 0) + 1
                self._schedule_write()
                return entry

            # If all accounts are exhausted (remaining <= 0), do NOT delete any accounts!
            # Old accounts refresh daily. Check if any accounts have refreshed their quota.
            logger.info("所有账号配额已耗尽，尝试刷新现有账号配额并触发自动注册...")
            self.trigger_auto_replenish_if_needed()

            # Quickly check if any existing account's daily quota has refreshed
            for entry in self.tokens:
                if not entry["disabled"] and entry["is_alive"]:
                    try:
                        async with self.create_session(entry, timeout=15) as session:
                            if await self._fetch_credits(entry, session) and entry["remaining"] > 0:
                                entry["remaining"] -= 1
                                entry["_inflight"] = _as_int(entry.get("_inflight"), 0) + 1
                                self._schedule_write()
                                logger.info("账号 %s 配额已重置，继续使用！", entry.get("email"))
                                return entry
                    except Exception:
                        pass

        raise HTTPException(
            status_code=429,
            detail="所有 aymo.ai 账号当前配额均已耗尽，已触发自动注册新账号，请稍后重试（旧账号次日自动刷新）。"
        )

    def restore_credit(self, entry: dict[str, Any]) -> None:
        inflight = _as_int(entry.get("_inflight"), 0)
        if inflight <= 0:
            return
        entry["_inflight"] = inflight - 1
        maximum = _as_int(entry.get("max_credits"), 20, minimum=0)
        if maximum:
            entry["remaining"] = min(maximum, _as_int(entry.get("remaining"), 0) + 1)
            self._schedule_write()

    def commit_credit(self, entry: dict[str, Any]) -> None:
        inflight = _as_int(entry.get("_inflight"), 0)
        if inflight > 0:
            entry["_inflight"] = inflight - 1

    def mark_request_error(
        self,
        entry: dict[str, Any],
        error: Exception | str,
        *,
        invalidate: bool = False,
    ) -> None:
        entry["last_error"] = str(error)
        if invalidate:
            entry["is_alive"] = False
        self._schedule_write()

    def invalidate_session(self, entry: dict[str, Any], error: Exception | str = "") -> None:
        entry["session_token"] = ""
        if isinstance(entry.get("cookies"), dict):
            entry["cookies"].pop(SESSION_COOKIE, None)
        entry["last_error"] = str(error or "session invalidated")
        entry["is_alive"] = False
        self._schedule_write()

    def _by_id(self, token_id: int) -> dict[str, Any] | None:
        return next((entry for entry in self.tokens if entry["id"] == token_id), None)

    async def add(
        self,
        items: list[dict[str, Any]],
        *,
        probe: bool = False,
        wait_for_probe: bool = False,
    ) -> dict[str, int | bool]:
        existing = {entry["email"].lower() for entry in self.tokens if entry.get("email")}
        new_entries: list[dict[str, Any]] = []
        for item in items:
            email = str(item.get("email") or "").strip().lower()
            if not email or email in existing:
                continue
            existing.add(email)
            new_entries.append(self._make_entry(item))

        if new_entries:
            async with self.lock:
                self.tokens.extend(new_entries)
            await self._write_file()
            if probe:
                if wait_for_probe:
                    await self._run_refresh(new_entries, "新账号探测")
                else:
                    self._track_background(
                        self._run_refresh(new_entries, "新账号探测"),
                        name="AymoNewAccountProbe",
                    )
        result: dict[str, int | bool] = {
            "added": len(new_entries),
            "skipped": len(items) - len(new_entries),
        }
        if probe and not wait_for_probe:
            result["probe_scheduled"] = bool(new_entries)
        return result

    def remove(self, token_id: int) -> bool:
        before = len(self.tokens)
        self.tokens = [entry for entry in self.tokens if entry["id"] != token_id]
        removed = len(self.tokens) != before
        if removed:
            self._entry_locks.pop(token_id, None)
            self._schedule_write()
        return removed

    def remove_many(self, token_ids: list[int]) -> int:
        wanted = set(token_ids)
        before = len(self.tokens)
        self.tokens = [entry for entry in self.tokens if entry["id"] not in wanted]
        for token_id in wanted:
            self._entry_locks.pop(token_id, None)
        removed = before - len(self.tokens)
        if removed:
            self._schedule_write()
        return removed

    def disable(self, token_ids: list[int]) -> int:
        wanted = set(token_ids)
        count = 0
        for entry in self.tokens:
            if entry["id"] in wanted and not entry["disabled"]:
                entry["disabled"] = True
                count += 1
        if count:
            self._schedule_write()
        return count

    def enable(self, token_ids: list[int]) -> int:
        wanted = set(token_ids)
        targets: list[dict[str, Any]] = []
        for entry in self.tokens:
            if entry["id"] in wanted and entry["disabled"]:
                entry["disabled"] = False
                targets.append(entry)
        if targets:
            self._schedule_write()
            self._track_background(
                self._run_refresh(targets, "启用账号探测"),
                name="AymoEnableProbe",
            )
        return len(targets)

    def summary(self) -> dict[str, int]:
        active = [
            entry
            for entry in self.tokens
            if entry["is_alive"] and entry["remaining"] > 0 and not entry["disabled"]
        ]
        disabled = sum(1 for entry in self.tokens if entry["disabled"])
        return {
            "total": len(self.tokens),
            "alive": len(active),
            "disabled": disabled,
            "exhausted": len(self.tokens) - len(active) - disabled,
            "total_remaining": sum(int(entry["remaining"]) for entry in active),
        }

    def detail(self) -> list[dict[str, Any]]:
        result = []
        for entry in self.tokens:
            token = entry.get("session_token") or ""
            result.append(
                {
                    "index": entry["id"],
                    "email": entry["email"],
                    "user_id": entry.get("user_id", ""),
                    "org_id": entry.get("org_id", ""),
                    "token_hint": f"...{token[-8:]}" if len(token) > 8 else token,
                    "is_alive": entry["is_alive"] and not entry["disabled"],
                    "remaining": entry["remaining"],
                    "max_credits": entry.get("max_credits", 20),
                    "daily_limit": entry.get("daily_limit", 20),
                    "daily_used": entry.get("daily_used", 0),
                    "daily_remaining": entry.get("daily_remaining", entry["remaining"]),
                    "monthly_limit": entry.get("monthly_limit", 50),
                    "monthly_used": entry.get("monthly_used", 0),
                    "monthly_remaining": entry.get("monthly_remaining", 50),
                    "credit_reset_date": entry.get("credit_reset_date", ""),
                    "extra_credits": entry.get("extra_credits", 0),
                    "message_limit": entry.get("message_limit", 500),
                    "tier": entry.get("tier", "free"),
                    "status": entry.get("status", "free"),
                    "disabled": entry["disabled"],
                    "last_checked": entry.get("last_checked", 0),
                    "last_error": entry.get("last_error", ""),
                }
            )
        return result

    async def keep_alive_loop(self, interval_minutes: int = 30) -> None:
        while True:
            await asyncio.sleep(max(1, interval_minutes) * 60)
            try:
                await self.refresh_all()
            except Exception as exc:
                logger.error("keep_alive_loop 异常: %s", exc)


# Module-level default instance
token_manager = TokenManager()
