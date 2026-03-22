"""Grok account registration runner."""
from __future__ import annotations

import concurrent.futures
import random
import re
import string
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from app.core.config import get_config
from app.core.logger import logger
from app.services.register.services import (
    EmailService,
    TurnstileService,
    UserAgreementService,
    BirthDateService,
    NsfwSettingsService,
)


SITE_URL = "https://accounts.x.ai"
SIGNUP_URL = f"{SITE_URL}/sign-up?redirect=grok-com"
DEFAULT_IMPERSONATE = "chrome120"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

CHROME_PROFILES = [
    {"impersonate": "chrome110", "version": "110.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome119", "version": "119.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome120", "version": "120.0.0.0", "brand": "chrome"},
    {"impersonate": "edge99", "version": "99.0.1150.36", "brand": "edge"},
    {"impersonate": "edge101", "version": "101.0.1210.47", "brand": "edge"},
]


def _extract_js_urls_from_html(start_url: str, html: str) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()

    candidates = [
        *[script["src"] for script in BeautifulSoup(html, "html.parser").find_all("script", src=True)],
        *[m.group(0) for m in re.finditer(r"/_next/static/chunks/[^\"'\s>]+\.js", html)],
    ]

    for raw in candidates:
        raw = (raw or "").strip().replace("\\/", "/")
        if not raw or "_next/static" not in raw:
            continue
        url = urljoin(start_url, raw)
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)

    return urls


def _extract_action_id_from_text(text: str) -> Optional[str]:
    patterns = [
        r"7f[a-fA-F0-9]{40}",
        r"(?<![a-fA-F0-9])7f[a-fA-F0-9]{30,}(?![a-fA-F0-9])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(0)
    return None


def _apply_config_from_html(config: Dict[str, Optional[str]], html: str) -> None:
    key_match = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
    if key_match:
        config["site_key"] = key_match.group(1)

    tree_match = re.search(r'next-router-state-tree":"([^"]+)"', html)
    if tree_match:
        config["state_tree"] = tree_match.group(1)


def _discover_action_id(start_url: str, html: str, fetch_text: Callable[[str], str]) -> Tuple[Optional[str], int]:
    action_id = _extract_action_id_from_text(html)
    if action_id:
        return action_id, 0

    js_urls = _extract_js_urls_from_html(start_url, html)
    for js_url in js_urls:
        try:
            js_content = fetch_text(js_url)
        except Exception as exc:
            logger.debug("Register: failed to fetch JS chunk {}: {}", js_url, exc)
            continue

        action_id = _extract_action_id_from_text(js_content)
        if action_id:
            return action_id, len(js_urls)

    return None, len(js_urls)


def _fetch_text_via_urllib(url: str, *, referer: Optional[str] = None, accept: str = "*/*") -> str:
    headers = {
        "user-agent": BROWSER_USER_AGENT,
        "accept": accept,
        "accept-language": "en-US,en;q=0.9",
    }
    if referer:
        headers["referer"] = referer

    with urlopen(Request(url, headers=headers), timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


def _extract_email_verification_code(text: str) -> Optional[str]:
    patterns = [
        r">([A-Z0-9]{3}-[A-Z0-9]{3})<",
        r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b",
        r"(?i)(?:confirmation|verification|code|验证码)[^A-Z0-9]{0,24}([A-Z0-9]{6,8})",
        r"(?<!\d)(\d{6,8})(?!\d)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(1).replace("-", "")
    return None


def _random_chrome_profile() -> Tuple[str, str]:
    profile = random.choice(CHROME_PROFILES)
    if profile.get("brand") == "edge":
        chrome_major = profile["version"].split(".")[0]
        chrome_version = f"{chrome_major}.0.0.0"
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36 Edg/{profile['version']}"
        )
    else:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{profile['version']} Safari/537.36"
        )
    return profile["impersonate"], ua


def _generate_random_name() -> str:
    length = random.randint(4, 6)
    return random.choice(string.ascii_uppercase) + "".join(
        random.choice(string.ascii_lowercase) for _ in range(length - 1)
    )


def _generate_random_string(length: int = 15) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def _encode_grpc_message(field_id: int, string_value: str) -> bytes:
    key = (field_id << 3) | 2
    value_bytes = string_value.encode("utf-8")
    payload = struct.pack("B", key) + struct.pack("B", len(value_bytes)) + value_bytes
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def _encode_grpc_message_verify(email: str, code: str) -> bytes:
    p1 = struct.pack("B", (1 << 3) | 2) + struct.pack("B", len(email)) + email.encode("utf-8")
    p2 = struct.pack("B", (2 << 3) | 2) + struct.pack("B", len(code)) + code.encode("utf-8")
    payload = p1 + p2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


class RegisterRunner:
    """Threaded registration runner."""

    def __init__(
        self,
        target_count: int = 100,
        thread_count: int = 8,
        on_success: Optional[Callable[[str, str, str, int, int], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.target_count = max(1, int(target_count))
        self.thread_count = max(1, int(thread_count))
        self.on_success = on_success
        self.on_error = on_error
        self.stop_event = stop_event or threading.Event()

        self._post_lock = threading.Lock()
        self._result_lock = threading.Lock()

        self._success_count = 0
        self._start_time = 0.0
        self._tokens: List[str] = []
        self._accounts: List[Dict[str, str]] = []
        self._last_send_code_error: Optional[str] = None
        self._cf_clearance = str(get_config("grok.cf_clearance", "") or "").strip()

        self._config: Dict[str, Optional[str]] = {
            "site_key": "0x4AAAAAAAhr9JGVDZbrZOo0",
            "action_id": None,
            "state_tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D",
        }

    @property
    def success_count(self) -> int:
        return self._success_count

    @property
    def tokens(self) -> List[str]:
        return list(self._tokens)

    @property
    def accounts(self) -> List[Dict[str, str]]:
        return list(self._accounts)

    def _record_success(self, email: str, password: str, token: str) -> None:
        with self._result_lock:
            if self._success_count >= self.target_count:
                if not self.stop_event.is_set():
                    self.stop_event.set()
                return

            self._success_count += 1
            self._tokens.append(token)
            self._accounts.append({"email": email, "password": password, "token": token})

            avg = (time.time() - self._start_time) / max(1, self._success_count)
            logger.info(
                "Register success: {} | sso={}... | avg={:.1f}s ({}/{})",
                email,
                token[:12],
                avg,
                self._success_count,
                self.target_count,
            )

            if self.on_success:
                try:
                    self.on_success(email, password, token, self._success_count, self.target_count)
                except Exception:
                    pass

            if self._success_count >= self.target_count and not self.stop_event.is_set():
                self.stop_event.set()

    def _record_error(self, message: str) -> None:
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                pass

    def _init_config(self) -> None:
        logger.info("Register: initializing action config...")
        start_url = SIGNUP_URL

        try:
            with curl_requests.Session(impersonate=DEFAULT_IMPERSONATE) as session:
                html = session.get(start_url, timeout=15).text
                _apply_config_from_html(self._config, html)

                action_id, js_count = _discover_action_id(
                    start_url,
                    html,
                    lambda js_url: session.get(js_url, timeout=15).text,
                )
                logger.info("Register: curl_cffi init scan completed, js_chunks={}", js_count)

                if action_id:
                    self._config["action_id"] = action_id
                    logger.info("Register: Action ID found via curl_cffi: {}", action_id)
        except Exception as exc:
            logger.warning("Register: curl_cffi init scan failed: {}", exc)

        if not self._config.get("action_id"):
            logger.warning("Register: action_id not found via curl_cffi, retrying with urllib.")
            try:
                html = _fetch_text_via_urllib(
                    start_url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                )
                _apply_config_from_html(self._config, html)

                action_id, js_count = _discover_action_id(
                    start_url,
                    html,
                    lambda js_url: _fetch_text_via_urllib(js_url, referer=start_url, accept="*/*"),
                )
                logger.info("Register: urllib init scan completed, js_chunks={}", js_count)

                if action_id:
                    self._config["action_id"] = action_id
                    logger.info("Register: Action ID found via urllib: {}", action_id)
            except Exception as exc:
                logger.warning("Register: urllib init scan failed: {}", exc)

        if not self._config.get("action_id"):
            raise RuntimeError("Register init failed: missing action_id")

    def _send_email_code(
        self,
        session: curl_requests.Session,
        email: str,
        user_agent: str = BROWSER_USER_AGENT,
    ) -> bool:
        url = f"{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
        data = _encode_grpc_message(1, email)
        self._last_send_code_error = None
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "origin": SITE_URL,
            "referer": SIGNUP_URL,
            "user-agent": user_agent or BROWSER_USER_AGENT,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        try:
            res = session.post(url, data=data, headers=headers, timeout=15)
            if res.status_code == 200:
                logger.info(
                    "Register: CreateEmailValidationCode ok email={} cookies={}",
                    email,
                    ",".join(sorted(session.cookies.get_dict().keys())) or "-",
                )
                return True
            body = (getattr(res, "text", "") or "").strip().replace("\n", " ")
            lowered_body = body.lower()
            if res.status_code == 403 and ("cloudflare" in lowered_body or "attention required" in lowered_body):
                detail = "cloudflare challenge blocked request"
                if not self._cf_clearance:
                    detail += "; set grok.cf_clearance"
                self._last_send_code_error = detail
            else:
                self._last_send_code_error = f"http {res.status_code}: {body[:200]}" if body else f"http {res.status_code}"
            logger.warning(
                "Register: CreateEmailValidationCode failed email={} status={} cf_clearance={} cookies={} body={}",
                email,
                res.status_code,
                bool(self._cf_clearance),
                ",".join(sorted(session.cookies.get_dict().keys())) or "-",
                body[:200] or "-",
            )
            return False
        except Exception as exc:
            self._last_send_code_error = str(exc)
            logger.warning(
                "Register: CreateEmailValidationCode exception email={} cf_clearance={} error={}",
                email,
                bool(self._cf_clearance),
                self._last_send_code_error,
            )
            return False

    def _verify_email_code(self, session: curl_requests.Session, email: str, code: str) -> bool:
        url = f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
        data = _encode_grpc_message_verify(email, code)
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": SITE_URL,
            "referer": SIGNUP_URL,
        }
        try:
            res = session.post(url, data=data, headers=headers, timeout=15)
            return res.status_code == 200
        except Exception as exc:
            self._record_error(f"verify code error: {email} - {exc}")
            return False

    def _register_single_thread(self) -> None:
        time.sleep(random.uniform(0, 5))

        try:
            email_service = EmailService()
            turnstile_service = TurnstileService()
            user_agreement_service = UserAgreementService()
            birth_date_service = BirthDateService()
            nsfw_service = NsfwSettingsService()
        except Exception as exc:
            self._record_error(f"service init failed: {exc}")
            return

        final_action_id = self._config.get("action_id")
        if not final_action_id:
            self._record_error("missing action id")
            return

        while not self.stop_event.is_set():
            try:
                impersonate_fingerprint, account_user_agent = _random_chrome_profile()

                with curl_requests.Session(impersonate=impersonate_fingerprint) as session:
                    try:
                        preheat_headers = {
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "accept-language": "en-US,en;q=0.9",
                            "user-agent": account_user_agent,
                        }
                        if self._cf_clearance:
                            session.cookies.set("cf_clearance", self._cf_clearance, domain="accounts.x.ai", path="/")
                        preheat_res = session.get(SIGNUP_URL, headers=preheat_headers, timeout=15)
                        body_head = (preheat_res.text or "")[:120].replace("\n", " ")
                        logger.info(
                            "Register: preheat sign-up status={} cf_clearance={} cookies={} body={}",
                            preheat_res.status_code,
                            bool(self._cf_clearance),
                            ",".join(sorted(session.cookies.get_dict().keys())) or "-",
                            body_head or "-",
                        )
                    except Exception as exc:
                        logger.warning("Register: preheat sign-up failed: {}", exc)

                    password = _generate_random_string()

                    jwt, email = email_service.create_email()
                    if not email:
                        self._record_error("create_email failed")
                        time.sleep(5)
                        continue

                    if self.stop_event.is_set():
                        return

                    if not self._send_email_code(session, email, account_user_agent):
                        fallback_applied = False
                        if email_service.can_fallback_to_moemail():
                            fallback_jwt, fallback_email = email_service.create_email(provider_override="moemail")
                            if fallback_email:
                                logger.info(
                                    "Register: send_email_code failed for worker mailbox {}, retrying with moemail {}",
                                    email,
                                    fallback_email,
                                )
                                if self._send_email_code(session, fallback_email, account_user_agent):
                                    jwt, email = fallback_jwt, fallback_email
                                    fallback_applied = True
                        if not fallback_applied:
                            detail = f" ({self._last_send_code_error})" if self._last_send_code_error else ""
                            self._record_error(f"send_email_code failed: {email}{detail}")
                            time.sleep(5)
                            continue

                    verify_code = None
                    for _ in range(30):
                        time.sleep(1)
                        if self.stop_event.is_set():
                            return
                        content = email_service.fetch_first_email(jwt)
                        if content:
                            code = _extract_email_verification_code(content)
                            if code:
                                verify_code = code
                                break

                    if not verify_code:
                        self._record_error(f"verify_code not received: {email}")
                        time.sleep(3)
                        continue

                    if not self._verify_email_code(session, email, verify_code):
                        self._record_error(f"verify_email_code failed: {email}")
                        time.sleep(3)
                        continue

                    for _ in range(3):
                        if self.stop_event.is_set():
                            return

                        try:
                            task_id = turnstile_service.create_task(SIGNUP_URL, self._config["site_key"] or "")
                        except Exception as exc:
                            self._record_error(f"turnstile create_task failed: {exc}")
                            time.sleep(2)
                            continue

                        token = turnstile_service.get_response(
                            task_id,
                            max_retries=60,
                            initial_delay=3,
                            retry_delay=2,
                            stop_event=self.stop_event,
                        )

                        if not token:
                            self._record_error(f"turnstile failed: {turnstile_service.last_error or 'no token'}")
                            time.sleep(2)
                            continue

                        headers = {
                            "user-agent": account_user_agent,
                            "accept": "text/x-component",
                            "content-type": "text/plain;charset=UTF-8",
                            "origin": SITE_URL,
                            "referer": SIGNUP_URL,
                            "cookie": f"__cf_bm={session.cookies.get('__cf_bm','')}",
                            "next-router-state-tree": self._config["state_tree"] or "",
                            "next-action": final_action_id,
                        }
                        payload = [
                            {
                                "emailValidationCode": verify_code,
                                "createUserAndSessionRequest": {
                                    "email": email,
                                    "givenName": _generate_random_name(),
                                    "familyName": _generate_random_name(),
                                    "clearTextPassword": password,
                                    "tosAcceptedVersion": "$undefined",
                                },
                                "turnstileToken": token,
                                "promptOnDuplicateEmail": True,
                            }
                        ]

                        with self._post_lock:
                            res = session.post(
                                f"{SITE_URL}/sign-up",
                                json=payload,
                                headers=headers,
                                timeout=20,
                            )

                        if res.status_code != 200:
                            self._record_error(f"sign_up http {res.status_code}")
                            time.sleep(3)
                            continue

                        match = re.search(r'(https://[^" \s]+set-cookie\?q=[^:" \s]+)1:', res.text)
                        if not match:
                            self._record_error("sign_up missing set-cookie redirect")
                            break

                        verify_url = match.group(1)
                        session.get(verify_url, allow_redirects=True, timeout=15)

                        sso = session.cookies.get("sso")
                        sso_rw = session.cookies.get("sso-rw")
                        if not sso:
                            self._record_error("sign_up missing sso cookie")
                            break

                        tos_result = user_agreement_service.accept_tos_version(
                            sso=sso,
                            sso_rw=sso_rw or "",
                            impersonate=impersonate_fingerprint,
                            user_agent=account_user_agent,
                        )
                        if not tos_result.get("ok") or not tos_result.get("hex_reply"):
                            self._record_error(f"accept_tos failed: {tos_result.get('error') or 'unknown'}")
                            break

                        birth_result = birth_date_service.set_birth_date(
                            sso=sso,
                            sso_rw=sso_rw or "",
                            impersonate=impersonate_fingerprint,
                            user_agent=account_user_agent,
                        )
                        if not birth_result.get("ok"):
                            self._record_error(
                                f"set_birth_date failed: {birth_result.get('error') or 'unknown'}"
                            )
                            break

                        nsfw_result = nsfw_service.enable_nsfw(
                            sso=sso,
                            sso_rw=sso_rw or "",
                            impersonate=impersonate_fingerprint,
                            user_agent=account_user_agent,
                        )
                        if not nsfw_result.get("ok") or not nsfw_result.get("hex_reply"):
                            self._record_error(f"enable_nsfw failed: {nsfw_result.get('error') or 'unknown'}")
                            break

                        self._record_success(email, password, sso)
                        break

            except Exception as exc:
                self._record_error(f"thread error: {str(exc)[:80]}")
                time.sleep(3)

    def run(self) -> List[str]:
        """Run the registration process and return collected tokens."""
        self._init_config()
        self._start_time = time.time()

        logger.info("Register: starting {} threads, target {}", self.thread_count, self.target_count)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as executor:
            futures = [executor.submit(self._register_single_thread) for _ in range(self.thread_count)]
            concurrent.futures.wait(futures)

        return list(self._tokens)
