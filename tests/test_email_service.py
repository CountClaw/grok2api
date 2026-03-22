from app.services.register.services.email_service import EmailService
from app.services.register import runner as runner_module


class _DummyResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_email_service_auto_selects_moemail_when_api_key_present(monkeypatch):
    monkeypatch.setattr(
        "app.services.register.services.email_service.get_config",
        lambda key, default=None: {
            "register.email_provider": "",
            "register.worker_domain": "",
            "register.email_domain": "",
            "register.admin_password": "",
            "register.moemail_api_base": "https://mail.kythron.com",
            "register.moemail_api_key": "demo-key",
            "register.moemail_domain": "moemail.app",
            "register.moemail_expiry_time_ms": 3600000,
        }.get(key, default),
    )

    service = EmailService()

    assert service.email_provider == "moemail"


def test_moemail_create_and_fetch_email(monkeypatch):
    service = EmailService(
        email_provider="moemail",
        moemail_api_base="https://mail.kythron.com",
        moemail_api_key="demo-key",
        moemail_domain="moemail.app",
        moemail_expiry_time_ms=3600000,
    )

    def _fake_post(url, json, headers, timeout):
        assert url == "https://mail.kythron.com/api/emails/generate"
        return _DummyResponse(
            200,
            {
                "email": {
                    "id": "email-1",
                    "address": "test@moemail.app",
                }
            },
        )

    def _fake_get(url, headers, timeout):
        if url == "https://mail.kythron.com/api/emails/email-1":
            return _DummyResponse(
                200,
                {
                    "messages": [
                        {
                            "id": "msg-1",
                        }
                    ]
                },
            )
        if url == "https://mail.kythron.com/api/emails/email-1/msg-1":
            return _DummyResponse(
                200,
                {
                    "message": {
                        "subject": "ABC-123 xAI confirmation code",
                        "text": "Use ABC-123 to continue",
                    }
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr("app.services.register.services.email_service.requests.post", _fake_post)
    monkeypatch.setattr("app.services.register.services.email_service.requests.get", _fake_get)

    email_id, address = service.create_email()
    content = service.fetch_first_email(email_id or "")

    assert email_id == "email-1"
    assert address == "test@moemail.app"
    assert content is not None
    assert "ABC-123" in content


def test_email_service_can_create_moemail_with_provider_override(monkeypatch):
    service = EmailService(
        email_provider="worker",
        worker_domain="worker.example.com",
        email_domain="003218.xyz",
        admin_password="demo-pass",
        moemail_api_base="https://mail.kythron.com",
        moemail_api_key="demo-key",
        moemail_domain="moemail.app",
    )

    def _fake_post(url, json, headers, timeout):
        if url == "https://mail.kythron.com/api/emails/generate":
            return _DummyResponse(
                200,
                {
                    "email": {
                        "id": "email-2",
                        "address": "fallback@moemail.app",
                    }
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr("app.services.register.services.email_service.requests.post", _fake_post)

    email_id, address = service.create_email(provider_override="moemail")

    assert service.can_fallback_to_moemail() is True
    assert email_id == "email-2"
    assert address == "fallback@moemail.app"


def test_email_service_can_fetch_moemail_with_provider_override(monkeypatch):
    service = EmailService(
        email_provider="worker",
        worker_domain="worker.example.com",
        email_domain="003218.xyz",
        admin_password="demo-pass",
        moemail_api_base="https://mail.kythron.com",
        moemail_api_key="demo-key",
        moemail_domain="moemail.app",
    )

    def _fake_get(url, headers, timeout):
        if url == "https://mail.kythron.com/api/emails/email-2":
            return _DummyResponse(
                200,
                {
                    "messages": [
                        {
                            "id": "msg-2",
                        }
                    ]
                },
            )
        if url == "https://mail.kythron.com/api/emails/email-2/msg-2":
            return _DummyResponse(
                200,
                {
                    "message": {
                        "subject": "DEF-456 xAI confirmation code",
                        "text": "Use DEF-456 to continue",
                    }
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr("app.services.register.services.email_service.requests.get", _fake_get)

    content = service.fetch_first_email("email-2", provider_override="moemail")

    assert content is not None
    assert "DEF-456" in content


def test_extract_email_verification_code_accepts_subject_style_code():
    text = "ABC-123 xAI confirmation code"

    code = runner_module._extract_email_verification_code(text)

    assert code == "ABC123"
