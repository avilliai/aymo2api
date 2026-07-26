"""Temporary mailbox adapter used by the NottoAI registration worker."""

from __future__ import annotations

import asyncio
import email
import logging
import os
import quopri
import random
import re
import string
from email.message import Message

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def extract_verification_code(raw_email: str) -> str | None:
    content = ""
    try:
        message: Message = email.message_from_string(raw_email)
        # 1. 尝试解析 MIME 正文
        if message.is_multipart():
            for part in message.walk():
                content_type = part.get_content_type()
                charset = part.get_content_charset() or "utf-8"

                if content_type in ["text/plain", "text/html"]:
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    try:
                        text = payload.decode(charset, errors="ignore")
                    except Exception:
                        text = payload.decode("utf-8", errors="ignore")
                    if text.strip():
                        content = text
                        break
        else:
            payload = message.get_payload(decode=True)
            if payload:
                content = payload.decode("utf-8", errors="ignore")
    except Exception:
        content = raw_email

    # 如果没有解析到，回退到原生邮件内容
    if not content:
        content = raw_email

    try:
        content = quopri.decodestring(content).decode("utf-8", errors="ignore")
    except Exception:
        pass

    # 2. 移除 HTML 与不可见字符并压缩空白
    content = re.sub(r"<[^>]+>", " ", content)
    content = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", "", content)
    content = re.sub(r"\s+", " ", content)

    # 3. 提取验证码（多策略）
    match = re.search(r"(?:code|验证码)[^\d]{0,10}(\d{4,8})", content, re.IGNORECASE)
    if match:
        return match.group(1)

    match = re.search(r"\b(\d{4,8})\b", content)
    if match:
        return match.group(1)

    return None


class EmailService:
    def __init__(self) -> None:
        self.worker_domain = (os.getenv("WORKER_DOMAIN") or "").strip().rstrip("/")
        self.email_domain = (os.getenv("EMAIL_DOMAIN") or "").strip()
        self.admin_password = (os.getenv("ADMIN_PASSWORD") or "").strip()
        try:
            self.poll_timeout = max(10, int(os.getenv("POLL_TIMEOUT", "120")))
        except (TypeError, ValueError):
            self.poll_timeout = 120
        try:
            self.poll_interval = max(1, int(os.getenv("POLL_INTERVAL", "3")))
        except (TypeError, ValueError):
            self.poll_interval = 3

    def _validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("WORKER_DOMAIN", self.worker_domain),
                ("EMAIL_DOMAIN", self.email_domain),
                ("ADMIN_PASSWORD", self.admin_password),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"缺少邮箱配置: {', '.join(missing)}")

    @staticmethod
    def _random_name() -> str:
        prefix = "".join(random.choices(string.ascii_lowercase, k=random.randint(7, 11)))
        suffix = "".join(random.choices(string.digits, k=random.randint(2, 4)))
        return prefix + suffix

    @property
    def base_url(self) -> str:
        domain = self.worker_domain
        if domain.startswith(("http://", "https://")):
            return domain
        return f"https://{domain}"

    async def create_email(self) -> tuple[str, str]:
        self._validate()
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            response = await client.post(
                f"{self.base_url}/admin/new_address",
                json={
                    "enablePrefix": True,
                    "name": self._random_name(),
                    "domain": self.email_domain,
                },
                headers={"x-admin-auth": self.admin_password},
            )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = {**data, **data["data"]}
        token = str(
            data.get("jwt")
            or data.get("token")
            or data.get("access_token")
            or data.get("accessToken")
            or ""
        )
        address = str(data.get("address") or data.get("email") or "")
        if not token or not address:
            raise RuntimeError(f"邮箱接口响应缺少 jwt/address: {data}")
        return token, address

    async def fetch_first_email(self, inbox_token: str) -> str | None:
        self._validate()
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            response = await client.get(
                f"{self.base_url}/api/mails",
                params={"limit": 10, "offset": 0},
                headers={"Authorization": f"Bearer {inbox_token}"},
            )
        if response.status_code != 200:
            logger.debug("邮箱查询 HTTP %s", response.status_code)
            return None
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = {**payload, **payload["data"]}
        results = payload.get("results") if isinstance(payload, dict) else []
        results = results or []
        if not results:
            return None
        first = results[0] if isinstance(results[0], dict) else {}
        return str(
            first.get("raw")
            or first.get("content")
            or first.get("body")
            or first.get("html")
            or ""
        ) or None

    async def wait_for_code(self, inbox_token: str) -> str:
        deadline = asyncio.get_running_loop().time() + self.poll_timeout
        seen_raw = None
        while asyncio.get_running_loop().time() < deadline:
            raw = await self.fetch_first_email(inbox_token)
            if raw and raw != seen_raw:
                code = extract_verification_code(raw)
                if code:
                    return code
                seen_raw = raw
            await asyncio.sleep(self.poll_interval)
        raise TimeoutError(f"等待邮箱验证码超时（{self.poll_timeout}s）")

    async def get_address_id_by_name(self, name: str) -> int | None:
        """通过邮箱名搜索，拿到 address id"""
        self._validate()
        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                response = await client.get(
                    f"{self.base_url}/admin/address",
                    params={"query": name, "limit": 10, "offset": 0},
                    headers={"x-admin-auth": self.admin_password},
                )
                if response.status_code == 200:
                    data = response.json()
                    results = data.get("results", [])
                    # 精确匹配，避免前缀相似的误命中
                    full_address = f"{name}@{self.email_domain}"
                    for item in results:
                        if item.get("name") == full_address or item.get("name") == name:
                            return item.get("id")
                    logger.debug("未找到精确匹配: %s，返回结果: %s", name, [r.get("name") for r in results])
                else:
                    logger.debug("查询地址失败: %s - %s", response.status_code, response.text)
        except Exception as e:
            logger.debug("查询地址异常: %s", e)
        return None

    async def get_jwt_by_address_id(self, address_id: int) -> str | None:
        """用 address id 直接生成 JWT，无需删除重建"""
        self._validate()
        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                response = await client.get(
                    f"{self.base_url}/admin/show_password/{address_id}",
                    headers={"x-admin-auth": self.admin_password},
                )
                if response.status_code == 200:
                    return response.json().get("jwt")
                logger.debug("获取 JWT 失败: %s - %s", response.status_code, response.text)
        except Exception as e:
            logger.debug("获取 JWT 异常: %s", e)
        return None

    async def recover_email_jwt(self, name: str) -> tuple[str | None, str | None]:
        """
        通过邮箱名恢复 JWT（用于找回之前未保存的 JWT）
        :return: (jwt, address)
        """
        address_id = await self.get_address_id_by_name(name)
        if not address_id:
            return None, None

        jwt = await self.get_jwt_by_address_id(address_id)
        if jwt:
            address = f"{name}@{self.email_domain}"
            logger.debug("成功恢复 JWT: %s", address)
            return jwt, address
        return None, None

    async def wait_for_code_by_email(self, email_address: str, timeout: int | None = None) -> str | None:
        """
        恢复旧临时邮箱，并等待提取登录验证码
        可以直接从 TokenManager 中传递邮箱调用此方法获取 Code。
        """
        name = email_address.split("@")[0]

        logger.debug("正在尝试恢复邮箱 [%s] 的 JWT 权限...", email_address)
        jwt, addr = await self.recover_email_jwt(name)
        if not jwt:
            logger.debug("恢复失败，该邮箱可能已被彻底清理或不存在。")
            return None

        logger.debug("恢复成功！开始监听验证码邮件 (超时设置: %s 秒)...", timeout or self.poll_timeout)

        limit = timeout or self.poll_timeout
        deadline = asyncio.get_running_loop().time() + limit
        seen_raw = None

        while asyncio.get_running_loop().time() < deadline:
            raw = await self.fetch_first_email(jwt)
            if raw and raw != seen_raw:
                code = extract_verification_code(raw)
                if code:
                    return code
                seen_raw = raw
            await asyncio.sleep(self.poll_interval)

        logger.debug("等待邮件超时。")
        return None