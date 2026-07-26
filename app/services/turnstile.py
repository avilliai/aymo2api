"""Cloudflare Turnstile solving via a local solver or YesCaptcha.

The local solver defaults to ``http://127.0.0.1:5072`` and speaks the same
GET ``/turnstile?url=&sitekey=`` → ``{taskId}`` / GET ``/result?id=`` →
``{solution:{token}}`` protocol used by the reference notto2api project.

Note: on Windows this environment carries a ghost system proxy that breaks
localhost requests through httpx.  We force ``trust_env=False`` so the solver
(and any 127.0.0.1 service) is reached directly.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# aymo.ai signup/signin Turnstile widget site key (reverse-engineered).
AYMO_SITEKEY = os.getenv("AYMO_SITEKEY", "0x4AAAAAACgaobmeaCdcpmsb")
APP_BASE_URL = os.getenv("AYMO_BASE_URL", "https://aymo.ai").rstrip("/")


class AsyncTurnstileService:
    """Async Turnstile solver adapter (local solver or YesCaptcha)."""

    def __init__(self, solver_url: str | None = None) -> None:
        self.yescaptcha_key = os.getenv("YESCAPTCHA_KEY", "").strip()
        self.solver_url = (solver_url or os.getenv("TURNSTILE_SOLVER_URL", "http://127.0.0.1:5072")).rstrip("/")
        self.yescaptcha_api = os.getenv("YESCAPTCHA_API", "https://api.yescaptcha.com")

    async def get_turnstile_token(
        self,
        siteurl: str,
        sitekey: str,
        max_retries: int = 30,
        initial_delay: int = 5,
        retry_delay: int = 2,
    ) -> str | None:
        # ``trust_env=False`` avoids the local system proxy hijacking 127.0.0.1.
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            try:
                if self.yescaptcha_key:
                    payload = {
                        "clientKey": self.yescaptcha_key,
                        "task": {
                            "type": "TurnstileTaskProxyless",
                            "websiteURL": siteurl,
                            "websiteKey": sitekey,
                        },
                    }
                    resp = await client.post(f"{self.yescaptcha_api}/createTask", json=payload)
                    resp.raise_for_status()
                    task_id = resp.json().get("taskId")
                else:
                    url = f"{self.solver_url}/turnstile?url={siteurl}&sitekey={sitekey}"
                    resp = await client.get(url)
                    resp.raise_for_status()
                    task_id = resp.json().get("taskId")

                if not task_id:
                    return None

                await asyncio.sleep(initial_delay)
                for _ in range(max_retries):
                    if self.yescaptcha_key:
                        resp = await client.post(
                            f"{self.yescaptcha_api}/getTaskResult",
                            json={"clientKey": self.yescaptcha_key, "taskId": task_id},
                        )
                        data = resp.json()
                        if data.get("status") == "ready":
                            return data.get("solution", {}).get("token")
                    else:
                        resp = await client.get(f"{self.solver_url}/result?id={task_id}")
                        data = resp.json()
                        token = (data.get("solution") or {}).get("token")
                        if token and token != "CAPTCHA_FAIL":
                            return token

                    await asyncio.sleep(retry_delay)
            except Exception as exc:
                logger.error("获取 Turnstile token 异常: %s", exc)
        return None

    async def solve(self, siteurl: str | None = None, sitekey: str | None = None) -> str | None:
        return await self.get_turnstile_token(
            siteurl or f"{APP_BASE_URL}/signin",
            sitekey or AYMO_SITEKEY,
        )
