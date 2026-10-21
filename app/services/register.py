"""aymo.ai account registration via better-auth + Turnstile (Outlook and Worker modes).

Flow:
  1. Obtain email mailbox (Outlook OAuth/IMAP or disposable EmailService)
  2. Solve Cloudflare Turnstile captcha for signup
  3. POST /backend-api/auth/sign-up/email with x-captcha-response
  4. Poll the mailbox for the verify-email link
  5. GET backend verify endpoint to obtain session cookie (__Secure-better-auth.session_token)
  6. Read organization id + daily credits and add to account pool
  7. Record email usage in data/outlook_usage.json
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
from app.services.outlook_email_service import (
    OutlookEmailService,
    get_folder_highest_uids,
    wait_for_outlook_verify_link,
    wait_for_outlook_otp_code,
)
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
    """Register a single aymo.ai account using disposable worker mailbox."""
    solver = AsyncTurnstileService()
    try:
        poll_timeout = max(30, int(os.getenv("POLL_TIMEOUT", "120")))
    except (TypeError, ValueError):
        poll_timeout = 120
    try:
        poll_interval = max(1, int(os.getenv("POLL_INTERVAL", "3")))
    except (TypeError, ValueError):
        poll_interval = 3

    proxy = proxy or os.getenv("PROXY") or None

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

        # 3) hit backend verify -> auto sign-in
        verify = await session.get(
            f"{AUTH_BASE}/verify-email",
            params={"token": verify_token, "callbackURL": f"{APP_BASE_URL}/chats"},
            headers={
                "accept": "*/*",
                "user-agent": USER_AGENT,
                "referer": f"{APP_BASE_URL}/",
            },
            allow_redirects=False,
        )
        cookies = session.cookies.get_dict()
        session_token = cookies.get(SESSION_COOKIE, "")
        if not session_token:
            raise RegistrationError("注册验证后未获得 better-auth 会话 cookie")

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


async def sign_in_outlook_account(
    record: dict[str, str],
    proxy: str | None = None,
    captcha_token: str | None = None,
) -> dict[str, Any]:
    """Sign in an existing Aymo account via Outlook OTP verification."""
    email = record["email"]
    password = record.get("password") or ""
    client_id = record["client_id"]
    refresh_token = record["refresh_token"]

    proxy = proxy or os.getenv("PROXY") or None
    try:
        poll_timeout = max(30, int(os.getenv("POLL_TIMEOUT", "120")))
    except (TypeError, ValueError):
        poll_timeout = 120
    try:
        poll_interval = max(1, int(os.getenv("POLL_INTERVAL", "3")))
    except (TypeError, ValueError):
        poll_interval = 3

    # 1) Get folder baseline UIDs before requesting sign-in OTP
    baseline = await asyncio.to_thread(get_folder_highest_uids, email, client_id, refresh_token)
    logger.info("[%s] Outlook IMAP baseline UIDs (登录): %s", email, baseline)

    solver = AsyncTurnstileService()
    cf_token1 = captcha_token or await solver.solve(f"{APP_BASE_URL}/signin", AYMO_SITEKEY)
    if not cf_token1:
        raise RegistrationError(f"Turnstile 登录 Step 1 验证码求解失败 ({email})")

    random_ip = f"{random.randint(20, 220)}.{random.randint(1, 254)}.{random.randint(1, 254)}.{random.randint(1, 254)}"
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": APP_BASE_URL,
        "referer": f"{APP_BASE_URL}/signin",
        "user-agent": USER_AGENT,
        "x-captcha-response": cf_token1,
        "x-forwarded-for": random_ip,
        "x-real-ip": random_ip,
    }

    async with AsyncSession(impersonate=IMPERSONATE, proxy=proxy, timeout=90) as session:
        # Step 1: POST email and password
        step1_resp = await session.post(
            f"{AUTH_BASE}/sign-in/email-password-step1",
            json={"email": email, "password": password},
            headers=headers,
        )
        if step1_resp.status_code not in (200, 201):
            raise RegistrationError(f"登录 Step 1 失败 HTTP {step1_resp.status_code}: {step1_resp.text[:300]}")

        # Step 2: Poll OTP code from mailbox
        logger.info("[%s] 等待 Outlook 登录验证码 (OTP)...", email)
        otp = await wait_for_outlook_otp_code(
            email,
            client_id,
            refresh_token,
            baseline_folders=baseline,
            timeout=poll_timeout,
            interval=poll_interval,
        )
        if not otp:
            raise RegistrationError(f"未能获取到 Outlook 登录验证码 ({email})")

        # Step 3: Solve Turnstile token for Step 2
        cf_token2 = await solver.solve(f"{APP_BASE_URL}/signin", AYMO_SITEKEY)
        if not cf_token2:
            raise RegistrationError(f"Turnstile 登录 Step 2 求解失败 ({email})")

        headers["x-captcha-response"] = cf_token2
        step2_resp = await session.post(
            f"{AUTH_BASE}/sign-in/email-password-step2",
            json={"email": email, "otp": otp, "callbackURL": f"{APP_BASE_URL}/chats"},
            headers=headers,
        )
        if step2_resp.status_code not in (200, 201):
            raise RegistrationError(f"登录 Step 2 失败 HTTP {step2_resp.status_code}: {step2_resp.text[:300]}")

        cookies = session.cookies.get_dict()
        session_token = cookies.get(SESSION_COOKIE, "")
        if not session_token:
            raise RegistrationError(f"登录后未取得 {SESSION_COOKIE} 会话 cookie")

        user_id = ""
        try:
            user_id = str((step2_resp.json().get("user") or {}).get("id") or "")
        except Exception:
            pass

        # Step 4: Fetch org id + credits
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

        remaining, max_credits = 20, 20
        try:
            usage_resp = await session.get(f"{BACKEND_API}/users/me/today-usage", headers=common)
            if usage_resp.status_code == 200:
                usage = usage_resp.json().get("data") or {}
                max_credits = int(usage.get("dailyCreditLimit") or 20)
                remaining = max(0, max_credits - int(usage.get("creditsUsed") or 0))
        except Exception:
            pass

        OutlookEmailService().mark_email_used(
            email,
            status="registered",
            extra={"user_id": user_id, "org_id": org_id, "signed_in_at": int(time.time())},
        )

        return {
            "email": email,
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh_token,
            "user_id": user_id,
            "org_id": org_id,
            "session_token": session_token,
            "cookies": cookies,
            "inbox_token": "",
            "proxy": proxy or "",
            "tier": "free",
            "status": "free",
            "credits": remaining,
            "max_credits": max_credits,
            "is_alive": True,
        }

async def register_outlook_account(
    record: dict[str, str],
    proxy: str | None = None,
    captcha_token: str | None = None,
) -> dict[str, Any]:
    """Register a single aymo.ai account using Outlook OAuth/IMAP."""
    email = record["email"]
    password = record.get("password") or generate_password()
    client_id = record["client_id"]
    refresh_token = record["refresh_token"]

    proxy = proxy or os.getenv("PROXY") or None
    try:
        poll_timeout = max(30, int(os.getenv("POLL_TIMEOUT", "120")))
    except (TypeError, ValueError):
        poll_timeout = 120
    try:
        poll_interval = max(1, int(os.getenv("POLL_INTERVAL", "4")))
    except (TypeError, ValueError):
        poll_interval = 4

    # 1) Get folder baseline UIDs before requesting sign-up
    baseline = await asyncio.to_thread(get_folder_highest_uids, email, client_id, refresh_token)
    logger.info("[%s] Outlook IMAP baseline UIDs: %s", email, baseline)

    # 2) Solve Turnstile captcha
    solver = AsyncTurnstileService()
    cf_token = captcha_token or await solver.solve(f"{APP_BASE_URL}/signup", AYMO_SITEKEY)
    if not cf_token:
        raise RegistrationError(f"Turnstile 验证码求解失败 ({email})")

    # 3) POST sign-up (with random client IP to prevent DigitalOcean/better-auth 429 rate limit)
    random_ip = f"{random.randint(20, 220)}.{random.randint(1, 254)}.{random.randint(1, 254)}.{random.randint(1, 254)}"
    signup_headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": APP_BASE_URL,
        "referer": f"{APP_BASE_URL}/signup",
        "user-agent": USER_AGENT,
        "x-captcha-response": cf_token,
        "x-forwarded-for": random_ip,
        "x-real-ip": random_ip,
    }

    async with AsyncSession(impersonate=IMPERSONATE, proxy=proxy, timeout=90) as session:
        resp = await session.post(
            f"{AUTH_BASE}/sign-up/email",
            json={
                "name": "Aymo User",
                "email": email,
                "password": password,
                "callbackURL": f"{APP_BASE_URL}/chats",
            },
            headers=signup_headers,
        )
        if resp.status_code not in (200, 201):
            body_snippet = resp.text[:300]
            if resp.status_code == 422 and ("EMAIL_ALREADY_EXISTS" in body_snippet or "already exists" in body_snippet.lower()):
                logger.info("[%s] 邮箱已在 Aymo 注册 (EMAIL_ALREADY_EXISTS)，直接执行登录流程...", email)
                acc = await sign_in_outlook_account(record, proxy=proxy)
                OutlookEmailService().mark_email_used(
                    email,
                    status="registered",
                    extra={"user_id": acc.get("user_id"), "org_id": acc.get("org_id"), "registered_at": int(time.time())},
                )
                return acc
            raise RegistrationError(f"注册请求失败 HTTP {resp.status_code}: {body_snippet}")

        user_id = ""
        try:
            user_id = str((resp.json().get("user") or {}).get("id") or "")
        except Exception:
            pass

        # 4) Wait for verify email link in Outlook inbox/junk
        logger.info("[%s] 等待 Outlook 验证邮件...", email)
        link = await wait_for_outlook_verify_link(
            email, client_id, refresh_token, baseline_folders=baseline, timeout=poll_timeout, interval=poll_interval
        )
        match = re.search(r"[?&]token=([^&]+)", link)
        if not match:
            raise RegistrationError(f"验证链接中缺少 token: {link}")
        verify_token = unquote(match.group(1))

        # 5) GET verify-email to capture session cookies
        v_resp = await session.get(
            f"{AUTH_BASE}/verify-email",
            params={"token": verify_token, "callbackURL": f"{APP_BASE_URL}/chats"},
            headers={
                "accept": "*/*",
                "user-agent": USER_AGENT,
                "referer": f"{APP_BASE_URL}/",
            },
            allow_redirects=False,
        )
        cookies = session.cookies.get_dict()
        session_token = cookies.get(SESSION_COOKIE, "")
        if not session_token:
            raise RegistrationError(f"验证邮件后未取得 {SESSION_COOKIE} 会话 cookie")

        # 6) Get org id + credits
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

        remaining, max_credits = 20, 20
        try:
            usage_resp = await session.get(f"{BACKEND_API}/users/me/today-usage", headers=common)
            if usage_resp.status_code == 200:
                usage = usage_resp.json().get("data") or {}
                max_credits = int(usage.get("dailyCreditLimit") or 20)
                remaining = max(0, max_credits - int(usage.get("creditsUsed") or 0))
        except Exception:
            pass

        # 7) Mark email as registered in data/outlook_usage.json
        OutlookEmailService().mark_email_used(
            email,
            status="registered",
            extra={"user_id": user_id, "org_id": org_id, "registered_at": int(time.time())},
        )

        return {
            "email": email,
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh_token,
            "user_id": user_id,
            "org_id": org_id,
            "session_token": session_token,
            "cookies": cookies,
            "inbox_token": "",
            "proxy": proxy or "",
            "tier": "free",
            "status": "free",
            "credits": remaining,
            "max_credits": max_credits,
            "is_alive": True,
        }


async def register_batch_outlook(count: int = 10, concurrency: int = 1) -> list[dict[str, Any]]:
    """Register a batch of Outlook accounts from file and persist to accounts.json."""
    from app.services.token_manager import token_manager

    svc = OutlookEmailService()
    registered: list[dict[str, Any]] = []
    proxy = os.getenv("PROXY") or None

    while len(registered) < count:
        existing_emails = {entry["email"].lower() for entry in token_manager.tokens if entry.get("email")}
        candidates = svc.get_candidate_accounts(count=count - len(registered), exclude_emails=existing_emails)
        if not candidates:
            logger.info("所有可用 Outlook 邮箱已注册使用完毕，切换为已有账号池轮询模式。")
            break

        used_status = svc.get_used_status().get("used", {})
        for rec in candidates:
            if len(registered) >= count:
                break
            email = rec["email"]
            is_already_reg = used_status.get(email.lower(), {}).get("status") == "already_registered"
            logger.info("处理 Outlook 账号 (%d/%d): %s (已知已注册: %s) ...", len(registered) + 1, count, email, is_already_reg)
            try:
                if is_already_reg:
                    logger.info("[%s] 属于已注册账号，直接走邮箱 OTP 登录流程恢复并加入活跃池...", email)
                    account = await sign_in_outlook_account(rec, proxy=proxy)
                    svc.mark_email_used(email, status="registered", extra={"user_id": account.get("user_id"), "org_id": account.get("org_id")})
                else:
                    account = await register_outlook_account(rec, proxy=proxy)
                await token_manager.add([account], probe=False)
                registered.append(account)
                logger.info("Outlook 账号 %s 成功加入活跃池，当前额度：%d (新增进度: %d/%d)", email, account.get("credits", 0), len(registered), count)
                await asyncio.sleep(2)
            except Exception as exc:
                err_msg = str(exc)
                logger.warning("Outlook 账号 %s 注册/登录异常: %s", email, err_msg)
                
                # Case 1: 邮箱已存在 -> 尝试直接邮箱登录恢复
                if "EMAIL_ALREADY_EXISTS" in err_msg or "already exists" in err_msg.lower():
                    logger.info("[%s] 邮箱已在 Aymo 注册过，尝试通过邮箱 OTP 登录接入活跃池...", email)
                    try:
                        account = await sign_in_outlook_account(rec, proxy=proxy)
                        await token_manager.add([account], probe=False)
                        registered.append(account)
                        logger.info("已注册 Outlook 账号 %s 成功恢复并加入活跃池，额度：%d", email, account.get("credits", 0))
                        continue
                    except Exception as signin_exc:
                        logger.warning("[%s] 已注册邮箱暂无法完成重登: %s", email, signin_exc)
                        svc.mark_email_used(email, status="already_registered", extra={"error": str(signin_exc)})
                        await asyncio.sleep(2)
                        continue

                # Case 2: 429 频率限制
                if "429" in err_msg or "rate limit" in err_msg.lower():
                    logger.warning("遇到 429 频率限制，等待 15 秒后重试下一候选账号（保留本邮箱）...")
                    await asyncio.sleep(15)
                    continue

                # Case 3: Turnstile solver 掉线或网络超时等临时故障 -> 绝对不标记为永久废弃！
                if svc.is_transient_error(err_msg):
                    logger.warning("[%s] 遇到外部服务/验证码求解临时故障，保留该候选账号以便恢复后重新使用", email)
                    await asyncio.sleep(3)
                    continue

                # Case 4: 真正的永久凭证损坏（如微软 OAuth invalid_grant）
                logger.error("Outlook 账号 %s 永久失效: %s", email, err_msg)
                svc.mark_email_used(email, status="failed", extra={"error": err_msg})
                await asyncio.sleep(2)

    return registered


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
        token_manager: Any,
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
        token_manager: Any,
        *,
        captcha_token: str | None,
    ) -> None:
        mode = os.getenv("EMAIL_MODE", "outlook").lower()
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
        default_proxy = os.getenv("PROXY") or None

        if mode == "outlook":
            svc = OutlookEmailService()
            existing_emails = {entry["email"].lower() for entry in token_manager.tokens if entry.get("email")}
            candidates = svc.get_candidate_accounts(count=count * 2, exclude_emails=existing_emails)
            cand_iter = iter(candidates)
            cand_lock = asyncio.Lock()

            async def get_next_candidate() -> dict[str, str] | None:
                async with cand_lock:
                    return next(cand_iter, None)
        else:
            get_next_candidate = None

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
                    proxy = random.choice(proxies) if proxies else default_proxy
                    if mode == "outlook":
                        cand = await get_next_candidate()
                        if not cand:
                            raise RegistrationError("Outlook 候选账号已用尽")
                        account = await register_outlook_account(
                            cand,
                            proxy=proxy,
                            captcha_token=captcha_token,
                        )
                    else:
                        inbox_token, address = await EmailService().create_email()
                        password = generate_password()
                        account = await register_account(
                            address,
                            password,
                            inbox_token,
                            proxy=proxy,
                            captcha_token=captcha_token,
                        )

                    added = await token_manager.add([account], probe=False)
                    if int(added.get("added", 0)) < 1:
                        raise RegistrationError("注册账号未能加入账号池（可能是重复邮箱）")
                    await self._share([account])

                    async with claim_lock:
                        if self._state["success"] >= count:
                            return
                        self._state["success"] += 1
                    logger.info("注册成功 worker=%d attempt=%d email=%s", worker_id, attempt_no, account.get("email"))
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
