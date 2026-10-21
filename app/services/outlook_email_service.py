import asyncio
import email as emod
import html
import imaplib
import json
import logging
import os
import quopri
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
import requests

logger = logging.getLogger(__name__)

STATUS_FILE = Path(os.getenv("OUTLOOK_STATUS_FILE", "data/outlook_usage.json"))

@dataclass
class OutlookInbox:
    address: str
    index: int
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""


def get_outlook_access_token(client_id: str, refresh_token: str, retries: int = 4) -> str:
    url = os.getenv("OUTLOOK_TOKEN_URL", "https://login.microsoftonline.com/common/oauth2/v2.0/token")
    last_exc = None
    proxies = None
    proxy = os.getenv("PROXY", "")
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                url,
                data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token},
                proxies=proxies,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if "access_token" in data:
                return data["access_token"]
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to obtain access token from Microsoft OAuth: {last_exc}")


def get_folder_highest_uids(email_addr: str, client_id: str, refresh_token: str, access_token: str = None) -> dict[str, int]:
    """Returns mapping of folder name to highest message ID before sending verification request."""
    result = {'"Junk"': 0, 'INBOX': 0}
    try:
        token = access_token or get_outlook_access_token(client_id, refresh_token)
    except Exception:
        return result

    hosts = [h.strip() for h in os.getenv("OUTLOOK_IMAP_HOSTS", "outlook.office365.com;imap-mail.outlook.com").replace(",", ";").split(";") if h.strip()]
    port = int(os.getenv("OUTLOOK_IMAP_PORT", "993"))
    for host in hosts:
        c = None
        try:
            c = imaplib.IMAP4_SSL(host, port, timeout=30)
            auth = f"user={email_addr}\x01auth=Bearer {token}\x01\x01".encode()
            typ, _ = c.authenticate("XOAUTH2", lambda _: auth)
            if typ != "OK":
                continue
            for folder in ['"Junk"', 'INBOX']:
                try:
                    typ, _ = c.select(folder, readonly=True)
                    if typ == "OK":
                        typ, ids = c.search(None, "ALL")
                        if typ == "OK" and ids[0].split():
                            result[folder] = int(ids[0].split()[-1])
                except Exception:
                    pass
            return result
        except Exception:
            pass
        finally:
            if c:
                try:
                    c.logout()
                except Exception:
                    pass
    return result


def _decode_raw_message(raw_bytes: bytes) -> str:
    try:
        msg = emod.message_from_bytes(raw_bytes)
        parts = msg.walk() if msg.is_multipart() else [msg]
        for part in parts:
            if part.get_content_type() in ("text/html", "text/plain"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="ignore")
    except Exception:
        pass
    try:
        return quopri.decodestring(raw_bytes).decode("utf-8", errors="ignore")
    except Exception:
        return raw_bytes.decode("utf-8", errors="ignore")


def check_folder_for_verify_link(c: imaplib.IMAP4_SSL, folder: str, after_id: int = 0) -> str | None:
    """Inspects folder messages with ID > after_id for Aymo verify-email link."""
    try:
        typ, _ = c.select(folder, readonly=True)
        if typ != "OK":
            return None
        typ, ids = c.search(None, "ALL")
        if typ != "OK" or not ids[0].split():
            return None
        all_ids = [int(mid) for mid in ids[0].split()]
        candidates = [mid for mid in all_ids if mid > after_id]
        if not candidates and after_id == 0:
            candidates = all_ids[-1:]

        for mid in candidates[::-1][:5]:
            typ, data = c.fetch(str(mid).encode(), "(RFC822)")
            if typ != "OK":
                continue
            raw_bytes = b"".join(x[1] for x in data if isinstance(x, tuple))
            decoded = _decode_raw_message(raw_bytes)

            if "aymo" in decoded.lower() or "verify" in decoded.lower():
                m = re.search(r'https?://[^\s"\'<>]*?/auth/verify-email\?[^\s"\'<>]+', decoded, re.I)
                if m:
                    return html.unescape(m.group(0))
    except Exception:
        pass
    return None


def check_folder_for_otp_code(c: imaplib.IMAP4_SSL, folder: str, after_id: int = 0) -> str | None:
    """Inspects folder messages with ID > after_id for Aymo sign-in 6-digit OTP code."""
    try:
        typ, _ = c.select(folder, readonly=True)
        if typ != "OK":
            return None
        typ, ids = c.search(None, "ALL")
        if typ != "OK" or not ids[0].split():
            return None
        all_ids = [int(mid) for mid in ids[0].split()]
        candidates = [mid for mid in all_ids if mid > after_id]
        if not candidates and after_id == 0:
            candidates = all_ids[-1:]

        for mid in candidates[::-1][:5]:
            typ, data = c.fetch(str(mid).encode(), "(RFC822)")
            if typ != "OK":
                continue
            raw_bytes = b"".join(x[1] for x in data if isinstance(x, tuple))
            decoded = _decode_raw_message(raw_bytes)

            if "aymo" in decoded.lower() or "verification" in decoded.lower() or "code" in decoded.lower():
                m = re.search(r'(?:code is|verification code|is:?)\s*[:：]?\s*([0-9]{6})', decoded, re.I)
                if m:
                    return m.group(1).strip()
                m2 = re.search(r'\b([0-9]{6})\b', decoded)
                if m2:
                    return m2.group(1).strip()
    except Exception:
        pass
    return None


def read_outlook_email_with_token(
    email_addr: str,
    access_token: str,
    baseline_folders: dict[str, int] = None,
    mode: str = "link",
) -> str | None:
    """Connects via IMAP using XOAUTH2 and searches for verify-email link or OTP code."""
    hosts = [h.strip() for h in os.getenv("OUTLOOK_IMAP_HOSTS", "outlook.office365.com;imap-mail.outlook.com").replace(",", ";").split(";") if h.strip()]
    port = int(os.getenv("OUTLOOK_IMAP_PORT", "993"))
    baseline = baseline_folders or {'"Junk"': 0, 'INBOX': 0}

    for host in hosts:
        c = None
        try:
            c = imaplib.IMAP4_SSL(host, port, timeout=30)
            auth = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01".encode()
            typ, _ = c.authenticate("XOAUTH2", lambda _: auth)
            if typ != "OK":
                continue

            for folder in ['"Junk"', 'INBOX']:
                after_id = baseline.get(folder, 0)
                if mode == "otp":
                    val = check_folder_for_otp_code(c, folder, after_id)
                else:
                    val = check_folder_for_verify_link(c, folder, after_id)
                if val:
                    return val

            return None
        except Exception:
            pass
        finally:
            if c:
                try:
                    c.logout()
                except Exception:
                    pass
    return None


async def wait_for_outlook_verify_link(
    email_addr: str,
    client_id: str,
    refresh_token: str,
    baseline_folders: dict[str, int] = None,
    timeout: int = 120,
    interval: int = 5,
) -> str:
    access_token = await asyncio.to_thread(get_outlook_access_token, client_id, refresh_token)
    deadline = time.time() + timeout

    while time.time() < deadline:
        await asyncio.sleep(interval)
        try:
            link = await asyncio.to_thread(
                read_outlook_email_with_token,
                email_addr,
                access_token,
                baseline_folders,
                "link",
            )
            if link:
                return link
        except Exception as e:
            logger.debug("wait_for_outlook_verify_link error: %s", e)
    raise TimeoutError(f"Outlook verification link timeout ({timeout}s): {email_addr}")


async def wait_for_outlook_otp_code(
    email_addr: str,
    client_id: str,
    refresh_token: str,
    baseline_folders: dict[str, int] = None,
    timeout: int = 120,
    interval: int = 5,
) -> str:
    access_token = await asyncio.to_thread(get_outlook_access_token, client_id, refresh_token)
    deadline = time.time() + timeout

    while time.time() < deadline:
        await asyncio.sleep(interval)
        try:
            code = await asyncio.to_thread(
                read_outlook_email_with_token,
                email_addr,
                access_token,
                baseline_folders,
                "otp",
            )
            if code:
                return code
        except Exception as e:
            logger.debug("wait_for_outlook_otp_code error: %s", e)
    raise TimeoutError(f"Outlook OTP code timeout ({timeout}s): {email_addr}")


class OutlookEmailService:
    def __init__(self, account_file: str = None):
        self.path = account_file or os.getenv("OUTLOOK_ACCOUNT_FILE", "outlook_live_20261018_222014.txt")
        self.accounts: list[dict[str, str]] = []
        self._accounts_by_email: dict[str, dict[str, str]] = {}
        self._load_accounts()

    def _load_accounts(self):
        self.accounts = []
        self._accounts_by_email = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8-sig") as f:
                for line in f:
                    parts = line.strip().split("----")
                    if len(parts) >= 4:
                        item = {
                            "email": parts[0].strip(),
                            "password": parts[1].strip(),
                            "client_id": parts[2].strip(),
                            "refresh_token": parts[3].strip(),
                        }
                        self.accounts.append(item)
                        self._accounts_by_email[item["email"].lower()] = item

    def get_account_by_email(self, email_addr: str) -> dict[str, str] | None:
        """Look up Outlook credentials by email address."""
        if not email_addr:
            return None
        return self._accounts_by_email.get(email_addr.strip().lower())

    def get_used_status(self) -> dict:
        if STATUS_FILE.exists():
            try:
                return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"used": {}, "updated_at": 0}

    def save_used_status(self, data: dict):
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = int(time.time())
        STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def mark_email_used(self, email_addr: str, status: str = "registered", extra: dict = None):
        data = self.get_used_status()
        used = data.setdefault("used", {})
        used[email_addr] = {
            "status": status,
            "time": int(time.time()),
            **(extra or {}),
        }
        self.save_used_status(data)

    @staticmethod
    def is_transient_error(err: str) -> bool:
        """Determines if an error is transient (solver offline, network timeout, 429) rather than fatal."""
        if not err:
            return True
        err_lower = err.lower()
        transient_keywords = (
            "turnstile", "captcha", "solver", "timeout", "timed out",
            "connect", "connection", "remotedisconnected", "proxy",
            "429", "rate limit", "500", "502", "503", "504",
            "temporary", "reset by peer", "ssl", "gateway", "超时", "解析失败", "未能在限定时间", "验证码", "网络", "掉线", "无法获取", "临时"
        )
        return any(k in err_lower for k in transient_keywords)

    def clean_and_recover_status(self) -> dict[str, int]:
        """Scans status file and clears false-positive 'failed' statuses (e.g. from solver offline)."""
        data = self.get_used_status()
        used = data.get("used", {})
        recovered = 0
        converted_already_reg = 0
        to_delete = []

        for email, info in used.items():
            st = info.get("status")
            err = str(info.get("error", ""))
            if st == "failed":
                if "EMAIL_ALREADY_EXISTS" in err or "already exists" in err.lower():
                    info["status"] = "already_registered"
                    converted_already_reg += 1
                elif self.is_transient_error(err):
                    to_delete.append(email)
                    recovered += 1

        for email in to_delete:
            del used[email]

        if recovered or converted_already_reg:
            self.save_used_status(data)
            logger.info(
                "Cleaned outlook_usage.json: %d accounts recovered from transient failure, %d updated to already_registered",
                recovered, converted_already_reg
            )
        return {"recovered": recovered, "converted": converted_already_reg}

    def get_candidate_accounts(self, count: int = 10, exclude_emails: set[str] = None) -> list[dict[str, str]]:
        """Returns available candidate accounts, excluding permanently consumed or broken accounts."""
        used_dict = self.get_used_status().get("used", {})
        permanently_used = set()
        for email, info in used_dict.items():
            status = info.get("status")
            err = str(info.get("error", ""))
            # If registered or already_registered on Aymo, it's not a candidate for fresh sign-up
            if status == "registered":
                permanently_used.add(email.lower())
            elif status == "failed":
                # If error was NOT transient (e.g. invalid Microsoft OAuth grant, disabled mailbox), mark as unusable
                if not self.is_transient_error(err) and ("already exists" not in err.lower()):
                    permanently_used.add(email.lower())

        excluded = {e.lower() for e in (set(exclude_emails or set()) | permanently_used)}
        candidates = []
        for acc in self.accounts:
            if acc["email"].lower() not in excluded:
                candidates.append(acc)
                if len(candidates) >= count:
                    break
        return candidates

    def get_stats(self) -> dict:
        used_dict = self.get_used_status().get("used", {})
        total = len(self.accounts)
        used_count = len(used_dict)
        return {
            "total_accounts_in_file": total,
            "used_count": used_count,
            "unused_count": max(0, total - used_count),
            "used_emails": list(used_dict.keys()),
        }
