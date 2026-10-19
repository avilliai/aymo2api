"""Cloudflare Turnstile solving via a remote/local solver or YesCaptcha."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

AYMO_SITEKEY = os.getenv("AYMO_SITEKEY", "0x4AAAAAACgaobmeaCdcpmsb")
APP_BASE_URL = os.getenv("AYMO_BASE_URL", "https://aymo.ai").rstrip("/")
DEFAULT_SOLVER_URL = os.getenv("TURNSTILE_SOLVER_URL", "")


class AsyncTurnstileService:
    """Async Turnstile solver adapter (local solver, remote solver, or YesCaptcha)."""

    def __init__(self, solver_url: str | None = None) -> None:
        self.yescaptcha_key = os.getenv("YESCAPTCHA_KEY", "").strip()
        self.solver_url = (solver_url or DEFAULT_SOLVER_URL).rstrip("/")
        self.yescaptcha_api = os.getenv("YESCAPTCHA_API", "https://api.yescaptcha.com")
        self.proxy = os.getenv("PROXY", "http://127.0.0.1:10809")

    def _get_client_kwargs(self) -> dict[str, Any]:
        is_local = "127.0.0.1" in self.solver_url or "localhost" in self.solver_url
        kwargs: dict[str, Any] = {"timeout": 30.0, "verify": False}
        if is_local:
            kwargs["trust_env"] = False
        else:
            if self.proxy:
                kwargs["proxy"] = self.proxy
        return kwargs

    async def get_turnstile_token(
        self,
        siteurl: str,
        sitekey: str,
        max_retries: int = 40,
        initial_delay: int = 2,
        retry_delay: int = 2,
    ) -> str | None:
        client_kwargs = self._get_client_kwargs()
        async with httpx.AsyncClient(**client_kwargs) as client:
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
            siteurl or f"{APP_BASE_URL}/signup",
            sitekey or AYMO_SITEKEY,
        )
