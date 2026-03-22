"""Email service for temporary inbox creation."""
from __future__ import annotations

import os
import random
import string
from typing import Any, Optional, Tuple

import requests

from app.core.config import get_config


class EmailService:
    """Email service wrapper."""

    def __init__(
        self,
        email_provider: Optional[str] = None,
        worker_domain: Optional[str] = None,
        email_domain: Optional[str] = None,
        admin_password: Optional[str] = None,
        moemail_api_base: Optional[str] = None,
        moemail_api_key: Optional[str] = None,
        moemail_domain: Optional[str] = None,
        moemail_expiry_time_ms: Optional[int] = None,
    ) -> None:
        provider_raw = (
            email_provider
            or get_config("register.email_provider", "")
            or os.getenv("EMAIL_PROVIDER", "")
        ).strip().lower()
        self.worker_domain = (
            (worker_domain or get_config("register.worker_domain", "") or os.getenv("WORKER_DOMAIN", "")).strip()
        )
        self.email_domain = (
            (email_domain or get_config("register.email_domain", "") or os.getenv("EMAIL_DOMAIN", "")).strip()
        )
        self.admin_password = (
            (admin_password or get_config("register.admin_password", "") or os.getenv("ADMIN_PASSWORD", "")).strip()
        )
        self.moemail_api_base = (
            (moemail_api_base or get_config("register.moemail_api_base", "") or os.getenv("MOEMAIL_API_BASE", "")).strip()
            or "https://mail.kythron.com"
        ).rstrip("/")
        self.moemail_api_key = (
            (moemail_api_key or get_config("register.moemail_api_key", "") or os.getenv("MOEMAIL_API_KEY", "")).strip()
        )
        self.moemail_domain = (
            (
                moemail_domain
                or get_config("register.moemail_domain", "")
                or os.getenv("MOEMAIL_DOMAIN", "")
                or self.email_domain
                or "moemail.app"
            ).strip()
        )
        expiry_val = (
            moemail_expiry_time_ms
            if moemail_expiry_time_ms is not None
            else get_config("register.moemail_expiry_time_ms", None)
        )
        if expiry_val in (None, ""):
            expiry_val = os.getenv("MOEMAIL_EXPIRY_TIME_MS", "")
        try:
            self.moemail_expiry_time_ms = int(expiry_val or 3600000)
        except Exception:
            self.moemail_expiry_time_ms = 3600000

        if provider_raw in {"worker", "moemail"}:
            self.email_provider = provider_raw
        elif self.moemail_api_key:
            self.email_provider = "moemail"
        else:
            self.email_provider = "worker"

        if self.email_provider == "moemail":
            if not self.moemail_api_key:
                raise ValueError("Missing required email setting: register.moemail_api_key")
        elif not all([self.worker_domain, self.email_domain, self.admin_password]):
            raise ValueError(
                "Missing required email settings: register.worker_domain, register.email_domain, "
                "register.admin_password"
            )

    def _generate_random_name(self) -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 6)))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(0, 5)))
        return letters1 + numbers + letters2

    def _moemail_headers(self, *, json_content_type: bool = False) -> dict[str, str]:
        headers = {
            "X-API-Key": self.moemail_api_key,
        }
        if json_content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _iter_objects(self, payload: Any):
        queue: list[Any] = [payload]
        while queue:
            current = queue.pop(0)
            if isinstance(current, dict):
                yield current
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        queue.append(value)
            elif isinstance(current, list):
                for item in current:
                    if isinstance(item, (dict, list)):
                        queue.append(item)

    def _extract_email_record(self, payload: Any) -> Tuple[Optional[str], Optional[str]]:
        for item in self._iter_objects(payload):
            email_id = (
                item.get("emailId")
                or item.get("id")
                or item.get("_id")
                or item.get("mailboxId")
            )
            address = item.get("address") or item.get("email") or item.get("emailAddress")
            if isinstance(email_id, str) and isinstance(address, str):
                return email_id, address
        return None, None

    def _extract_message_record(self, payload: Any) -> Optional[dict]:
        for item in self._iter_objects(payload):
            if any(
                key in item
                for key in (
                    "messageId",
                    "subject",
                    "text",
                    "textContent",
                    "html",
                    "htmlContent",
                    "raw",
                    "content",
                )
            ):
                return item
        return None

    def _extract_message_id(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        for key in ("messageId", "id", "_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _format_message_content(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        parts = []
        for key in ("subject", "from", "to", "text", "textContent", "plainText", "html", "htmlContent", "raw", "content", "body"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        return "\n".join(parts) if parts else None

    def _resolve_moemail_domain(self) -> str:
        if self.moemail_domain:
            return self.moemail_domain

        try:
            res = requests.get(
                f"{self.moemail_api_base}/api/config",
                headers=self._moemail_headers(),
                timeout=10,
            )
            if res.status_code == 200:
                payload = res.json()
                for item in self._iter_objects(payload):
                    for key in ("domains", "availableDomains"):
                        domains = item.get(key)
                        if isinstance(domains, list):
                            values = [str(domain).strip() for domain in domains if str(domain).strip()]
                            if values:
                                preferred = next((domain for domain in values if domain == "moemail.app"), None)
                                return preferred or values[0]
        except Exception:
            pass
        return "moemail.app"

    def _create_email_moemail(self) -> Tuple[Optional[str], Optional[str]]:
        try:
            random_name = self._generate_random_name()
            res = requests.post(
                f"{self.moemail_api_base}/api/emails/generate",
                json={
                    "name": random_name,
                    "expiryTime": self.moemail_expiry_time_ms,
                    "domain": self._resolve_moemail_domain(),
                },
                headers=self._moemail_headers(json_content_type=True),
                timeout=15,
            )
            if res.status_code in (200, 201):
                payload = res.json()
                email_id, address = self._extract_email_record(payload)
                if address and not email_id:
                    email_id = self._find_email_id_by_address(address)
                if email_id and address:
                    return email_id, address
                print(f"[-] Moemail create response missing email record: {str(payload)[:300]}")
            else:
                print(f"[-] Moemail create failed: {res.status_code} - {res.text}")
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"[-] Moemail create error: {exc}")
        return None, None

    def can_fallback_to_moemail(self) -> bool:
        return self.email_provider != "moemail" and bool(self.moemail_api_key)

    def _find_email_id_by_address(self, address: str) -> Optional[str]:
        try:
            res = requests.get(
                f"{self.moemail_api_base}/api/emails",
                headers=self._moemail_headers(),
                timeout=10,
            )
            if res.status_code != 200:
                return None
            for item in self._iter_objects(res.json()):
                item_address = item.get("address") or item.get("email") or item.get("emailAddress")
                if isinstance(item_address, str) and item_address.strip().lower() == address.strip().lower():
                    return self._extract_email_id_from_item(item)
        except Exception:
            return None
        return None

    def _extract_email_id_from_item(self, item: Any) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        for key in ("emailId", "id", "_id", "mailboxId"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _fetch_first_email_moemail(self, email_id: str) -> Optional[str]:
        try:
            res = requests.get(
                f"{self.moemail_api_base}/api/emails/{email_id}",
                headers=self._moemail_headers(),
                timeout=10,
            )
            if res.status_code != 200:
                return None
            payload = res.json()
            message = self._extract_message_record(payload)
            if not message:
                return None

            direct_content = self._format_message_content(message)
            if direct_content:
                return direct_content

            message_id = self._extract_message_id(message)
            if not message_id:
                return None

            detail_res = requests.get(
                f"{self.moemail_api_base}/api/emails/{email_id}/{message_id}",
                headers=self._moemail_headers(),
                timeout=10,
            )
            if detail_res.status_code != 200:
                return None
            detail_payload = detail_res.json()
            detail_message = self._extract_message_record(detail_payload) or detail_payload
            return self._format_message_content(detail_message)
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"Moemail fetch failed: {exc}")
            return None

    def create_email(self, provider_override: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """Create a temporary mailbox. Returns (jwt, address)."""
        provider = (provider_override or self.email_provider or "").strip().lower()
        if provider == "moemail":
            return self._create_email_moemail()

        url = f"https://{self.worker_domain}/admin/new_address"
        try:
            random_name = self._generate_random_name()
            res = requests.post(
                url,
                json={
                    "enablePrefix": True,
                    "name": random_name,
                    "domain": self.email_domain,
                },
                headers={
                    "x-admin-auth": self.admin_password,
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("jwt"), data.get("address")
            print(f"[-] Email create failed: {res.status_code} - {res.text}")
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"[-] Email create error ({url}): {exc}")
        return None, None

    def fetch_first_email(self, jwt: str) -> Optional[str]:
        """Fetch the first email content for the mailbox."""
        if self.email_provider == "moemail":
            return self._fetch_first_email_moemail(jwt)

        try:
            res = requests.get(
                f"https://{self.worker_domain}/api/mails",
                params={"limit": 10, "offset": 0},
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("results"):
                    return data["results"][0].get("raw")
            return None
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"Email fetch failed: {exc}")
            return None
