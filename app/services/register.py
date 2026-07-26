"""aymo.ai account registration via better-auth + Turnstile.

Flow (validated end-to-end):
  1. create a disposable mailbox (EmailService)
  2. solve the signup Turnstile
  3. POST ``/backend-api/auth/sign-up/email`` with ``x-captcha-response``
  4. poll the mailbox for the ``.../auth/verify-email?token=..`` link
  5. GET the backend verify endpoint → 302 auto sign-in sets the session cookie
     (fallback: ``sign-in/email`` with a fresh captcha)
  6. read org id + daily credits and add the account to the pool
"""

from __future__ import annotations

import asyncio
import email as emod
import html
import json
import logging
import os
import quopri
import random
import re
import string
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

from app.services.email_service import EmailService
from app.services.token_manager import (
    APP_BASE_URL,
    AUTH_BASE,
    BACKEND_API,
    IMPERSONATE,
    SESSION_COOKIE,
    USER_AGENT,
    TokenManager,
)
from app.services.turnstile import AYMO_SITEKEY, AsyncTurnstileService

load_dotenv()
logger = logging.getLogger(__name__)


class RegistrationError(RuntimeError):
    pass


def generate_password(length: int = 16) -> str:
    length = max(12, length)
    required = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%_-"),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%_-"
    required.extend(random.choices(alphabet, k=length - len(required)))
    random.shuffle(required)
    return "".join(required)


def _decode_email_body(raw: str) -> str:
    try:
        message = emod.message_from_string(raw)
        parts = message.walk() if message.is_multipart() else [message]
        for part in parts:
            if part.get_content_type() in ("text/html", "text/plain"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="ignore")
    except Exception:
        pass
    try:
        return quopri.decodestring(raw).decode("utf-8", errors="ignore")
    except Exception:
        return raw


def _extract_verify_link(body: str) -> str | None:
    match = re.search(
        r'https?://[^\s"\'<>]*?/auth/verify-email\?[^\s"\'<>]+', body, re.I
    )
    if not match:
        return None
    return html.unescape(match.group(0))


async def _wait_for_verify_link(inbox_token: str, timeout: int, interval: int) -> str:
    service = EmailService()
    deadline = asyncio.get_running_loop().time() + timeout
    seen: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        raw = await service.fetch_first_email(inbox_token)
        if raw and raw != seen:
            link = _extract_verify_link(_decode_email_body(raw))
            if link:
                return link
            seen = raw
        await asyncio.sleep(interval)
    raise RegistrationError(f"等待验证邮件超时（{timeout}s）")


async def register_account(
    email_address: str,
    password: str,
    inbox_token: str,
    proxy: str | None = None,
    captcha_token: str | None = None,
) -> dict[str, Any]:
    """Register a single aymo.ai account and return a pool entry dict."""
    solver = AsyncTurnstileService()
    try:
        poll_timeout = max(30, int(os.getenv("POLL_TIMEOUT", "120")))
    except (TypeError, ValueError):
        poll_timeout = 120
    try:
        poll_interval = max(1, int(os.getenv("POLL_INTERVAL", "3")))
    except (TypeError, ValueError):
        poll_interval = 3

    signup_headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": APP_BASE_URL,
        "referer": f"{APP_BASE_URL}/signup",
        "user-agent": USER_AGENT,
    }

    async with AsyncSession(impersonate=IMPERSONATE, proxy=proxy, timeout=90) as session:
        # 1) signup
        token = captcha_token or await solver.solve(f"{APP_BASE_URL}/signup", AYMO_SITEKEY)
        if not token:
            raise RegistrationError("无法获取注册 Turnstile token")
        resp = await session.post(
            f"{AUTH_BASE}/sign-up/email",
            json={
                "name": "Aymo User",
                "email": email_address,
                "password": password,
                "callbackURL": f"{APP_BASE_URL}/chats",
            },
            headers={**signup_headers, "x-captcha-response": token},
        )
        if resp.status_code not in (200, 201):
            raise RegistrationError(f"注册失败 HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            user_id = str((resp.json().get("user") or {}).get("id") or "")
        except Exception:
            user_id = ""

        # 2) verify email link
        link = await _wait_for_verify_link(inbox_token, poll_timeout, poll_interval)
        match = re.search(r"[?&]token=([^&]+)", link)
        if not match:
            raise RegistrationError("验证链接缺少 token")
        verify_token = unquote(match.group(1))

        # 3) hit backend verify → auto sign-in
        verify = await session.get(
            f"{AUTH_BASE}/verify-email",
            params={"token": verify_token, "callbackURL": f"{APP_BASE_URL}/chats"},
            headers={
                "accept": "*/*",
                "user-agent": USER_AGENT,
                "referer": f"{APP_BASE_URL}/",
            },
        )
        cookies = session.cookies.get_dict()
        if SESSION_COOKIE not in cookies:
            # fallback: explicit sign-in with a fresh captcha
            signin_token = await solver.solve(f"{APP_BASE_URL}/signin", AYMO_SITEKEY)
            if not signin_token:
                raise RegistrationError("验证后未获得会话，且无法获取登录 Turnstile token")
            si = await session.post(
                f"{AUTH_BASE}/sign-in/email",
                json={
                    "email": email_address,
                    "password": password,
                    "rememberMe": True,
                    "callbackURL": f"{APP_BASE_URL}/chats",
                },
                headers={**signup_headers, "referer": f"{APP_BASE_URL}/signin",
                         "x-captcha-response": signin_token},
            )
            if si.status_code != 200:
                raise RegistrationError(
                    f"验证后登录失败 HTTP {si.status_code}: {si.text[:300]}"
                )
            cookies = session.cookies.get_dict()

        session_token = cookies.get(SESSION_COOKIE, "")
        if not session_token:
            raise RegistrationError("注册流程未获得 better-auth 会话 cookie")

        # 4) org id + credits
        common = {
            "accept": "*/*",
            "user-agent": USER_AGENT,
            "origin": APP_BASE_URL,
            "referer": f"{APP_BASE_URL}/chats",
        }
        org_id = ""
        try:
            org_resp = await session.get(f"{BACKEND_API}/organizations", headers=common)
            data = (org_resp.json().get("data") or []) if org_resp.status_code == 200 else []
            if data:
                org_id = str(data[0].get("id") or "")
        except Exception:
            pass

        remaining, max_credits = 0, 0
        try:
            usage_resp = await session.get(
                f"{BACKEND_API}/users/me/today-usage", headers=common
            )
            if usage_resp.status_code == 200:
                usage = usage_resp.json().get("data") or {}
                max_credits = int(usage.get("dailyCreditLimit") or 0)
                remaining = max(0, max_credits - int(usage.get("creditsUsed") or 0))
        except Exception:
            pass

        return {
            "email": email_address,
            "password": password,
            "user_id": user_id,
            "org_id": org_id,
            "session_token": session_token,
            "cookies": cookies,
            "inbox_token": inbox_token,
            "proxy": proxy or "",
            "tier": "free",
            "status": "free",
            "credits": remaining,
            "max_credits": max_credits,
            "is_alive": True,
        }


class RegistrationService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._state_file = Path(os.getenv("REGISTER_JOB_FILE", "data/register-job.json"))
        self._state_lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "running": False,
            "job_id": None,
            "target": 0,
            "success": 0,
            "failed": 0,
            "attempts": 0,
            "last_error": "",
            "started_at": None,
            "finished_at": None,
            "stop_requested": False,
        }
        self._load_state()

    def _load_state(self) -> None:
        try:
            if not self._state_file.exists():
                return
            data = json.loads(self._state_file.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                self._state.update(data)
                if self._state.get("running"):
                    self._state["running"] = False
                    self._state["stop_requested"] = True
        except Exception as exc:
            logger.debug("读取注册任务状态失败: %s", exc)

    def _schedule_state_write(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self._write_state())
        except RuntimeError:
            pass

    async def _write_state(self) -> None:
        async with self._state_lock:
            try:
                self._state_file.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(self._state, ensure_ascii=False, indent=2)
                temp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
                await asyncio.to_thread(temp.write_text, payload, "utf-8")
                await asyncio.to_thread(temp.replace, self._state_file)
            except Exception as exc:
                logger.debug("写入注册任务状态失败: %s", exc)

    def status(self) -> dict[str, Any]:
        return dict(self._state)

    async def stop(self) -> dict[str, Any]:
        if self._task and not self._task.done() and self._stop_event is not None:
            self._state["stop_requested"] = True
            self._stop_event.set()
            if os.getenv("REGISTER_STOP_CANCEL", "true").lower() in {"1", "true", "yes", "on"}:
                self._task.cancel()
            self._schedule_state_write()
        return self.status()

    async def start(
        self,
        count: int,
        token_manager: TokenManager,
        *,
        backend: str | None = None,
        captcha_token: str | None = None,
    ) -> dict[str, Any]:
        if self._task and not self._task.done():
            raise RegistrationError("已有注册任务正在运行")
        if count < 1:
            raise RegistrationError("目标数量必须大于 0")

        job_id = f"reg_{uuid.uuid4().hex[:12]}"
        self._state = {
            "running": True,
            "job_id": job_id,
            "target": count,
            "success": 0,
            "failed": 0,
            "attempts": 0,
            "last_error": "",
            "started_at": int(time.time()),
            "finished_at": None,
            "stop_requested": False,
        }
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(count, token_manager, captcha_token=captcha_token),
            name=f"AymoRegister-{job_id}",
        )
        self._schedule_state_write()
        return self.status()

    async def _run(
        self,
        count: int,
        token_manager: TokenManager,
        *,
        captcha_token: str | None,
    ) -> None:
        try:
            concurrency = max(1, int(os.getenv("THREADS", "1")))
        except (TypeError, ValueError):
            concurrency = 1
        try:
            max_attempts = max(1, int(os.getenv("REGISTER_MAX_ATTEMPTS", "3")))
        except (TypeError, ValueError):
            max_attempts = 3

        proxies = [
            item.strip()
            for item in re.split(r"[,\r\n]+", os.getenv("PROXY_LIST", ""))
            if item.strip()
        ]

        worker_count = min(max(1, concurrency), count)
        claim_lock = asyncio.Lock()
        claimed = 0
        in_progress = 0

        async def claim_attempt() -> int | None:
            nonlocal claimed, in_progress
            async with claim_lock:
                if self._stop_event is not None and self._stop_event.is_set():
                    return None
                if self._state["success"] + in_progress >= count:
                    return None
                if claimed >= count * max_attempts:
                    return None
                claimed += 1
                in_progress += 1
                self._state["attempts"] = claimed
                self._schedule_state_write()
                return claimed

        async def one(worker_id: int) -> None:
            nonlocal in_progress
            while True:
                attempt_no = await claim_attempt()
                if attempt_no is None:
                    return
                try:
                    inbox_token, address = await EmailService().create_email()
                    password = generate_password()
                    proxy = random.choice(proxies) if proxies else None
                    account = await register_account(
                        address,
                        password,
                        inbox_token,
                        proxy=proxy,
                        captcha_token=captcha_token,
                    )
                    added = await token_manager.add([account], probe=True)
                    if int(added.get("added", 0)) < 1:
                        raise RegistrationError("注册账号未能加入账号池（可能是重复邮箱）")
                    await self._share([account])

                    async with claim_lock:
                        if self._state["success"] >= count:
                            return
                        self._state["success"] += 1
                    logger.info("注册成功 worker=%d attempt=%d email=%s", worker_id, attempt_no, address)
                    if self._state["success"] >= count and self._stop_event is not None:
                        self._stop_event.set()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._state["failed"] += 1
                    self._state["last_error"] = str(exc)
                    logger.warning("注册 worker=%d attempt=%d 失败: %s", worker_id, attempt_no, exc)
                    self._schedule_state_write()
                    if self._stop_event is not None and self._stop_event.is_set():
                        return
                    try:
                        sleep_interval = max(1, int(os.getenv("SLEEP_INTERVAL", "5")))
                    except (TypeError, ValueError):
                        sleep_interval = 5
                    await asyncio.sleep(sleep_interval)
                finally:
                    async with claim_lock:
                        in_progress = max(0, in_progress - 1)
                    self._schedule_state_write()

        try:
            await asyncio.gather(*(one(index + 1) for index in range(worker_count)))
        except asyncio.CancelledError:
            raise
        finally:
            self._state["running"] = False
            self._state["finished_at"] = int(time.time())
            if self._stop_event is not None and self._stop_event.is_set() and self._state["success"] < count:
                self._state["stop_requested"] = True
            self._schedule_state_write()

    async def _share(self, accounts: list[dict[str, Any]]) -> None:
        if os.getenv("SHARE_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
            return
        url = os.getenv("SHARE_URL", "").strip()
        if not url:
            return
        key = os.getenv("SHARE_BEARER_KEY", "")
        payload = {
            "tokens": [
                {
                    "email": item.get("email", ""),
                    "password": item.get("password", ""),
                    "session_token": item.get("session_token", ""),
                    "user_id": item.get("user_id", ""),
                    "org_id": item.get("org_id", ""),
                    "cookies": item.get("cookies", {}),
                }
                for item in accounts
            ]
        }
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}"} if key else {},
            )
            response.raise_for_status()


registration_service = RegistrationService()
