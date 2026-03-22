from app.services.register import runner as runner_module


class _DummyCookies:
    def __init__(self) -> None:
        self._data = {}

    def get(self, name: str, default=None):
        return self._data.get(name, default)

    def set(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        self._data[name] = value

    def keys(self):
        return self._data.keys()

    def get_dict(self):
        return dict(self._data)


def test_extract_action_id_from_server_reference_chunk():
    text = 'let tV=(0,tR.createServerReference)("7f69646bb11542f4cad728680077c67a09624b94e0",tR.callServer,void 0,tR.findSourceMapURL,"default");'

    assert runner_module._extract_action_id_from_text(text) == "7f69646bb11542f4cad728680077c67a09624b94e0"


def test_init_config_falls_back_to_urllib_when_curl_scan_misses_action_id(monkeypatch):
    action_id = "7f69646bb11542f4cad728680077c67a09624b94e0"
    html = """
    <html>
      <body>
        <script src="/_next/static/chunks/a.js"></script>
        <script src="/_next/static/chunks/e74a065e123a76d2.js"></script>
      </body>
    </html>
    """

    class _DummyResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    class _DummySession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def get(self, url: str, timeout: int = 15):
            if "/sign-up" in url:
                return _DummyResponse(html)
            return _DummyResponse("console.log('no action id here');")

    def _fake_fetch_text_via_urllib(url: str, *, referer=None, accept="*/*") -> str:
        if "/sign-up" in url and url.endswith(".js") is False:
            return html
        if url.endswith("/_next/static/chunks/e74a065e123a76d2.js"):
            return f'let action=createServerReference("{action_id}",callServer,void 0,findSourceMapURL,"default");'
        return "console.log('no action id here');"

    monkeypatch.setattr(runner_module.curl_requests, "Session", _DummySession)
    monkeypatch.setattr(runner_module, "_fetch_text_via_urllib", _fake_fetch_text_via_urllib)

    runner = runner_module.RegisterRunner(target_count=1, thread_count=1)
    runner._init_config()

    assert runner._config["action_id"] == action_id


def test_send_email_code_records_http_error_details():
    class _DummyResponse:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text

    class _DummySession:
        def __init__(self) -> None:
            self.cookies = _DummyCookies()

        def post(self, *args, **kwargs):
            return _DummyResponse(403, "forbidden")

    runner = runner_module.RegisterRunner(target_count=1, thread_count=1)

    ok = runner._send_email_code(_DummySession(), "demo@example.com")

    assert ok is False
    assert runner._last_send_code_error == "http 403: forbidden"


def test_send_email_code_maps_cloudflare_403_to_clearance_hint():
    class _DummyResponse:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text

    class _DummySession:
        def __init__(self) -> None:
            self.cookies = _DummyCookies()

        def post(self, *args, **kwargs):
            return _DummyResponse(403, "<title>Attention Required! | Cloudflare</title>")

    runner = runner_module.RegisterRunner(target_count=1, thread_count=1)

    ok = runner._send_email_code(_DummySession(), "demo@example.com")

    assert ok is False
    assert runner._last_send_code_error == "cloudflare challenge blocked request; set grok.cf_clearance"


def test_preflight_signup_with_solver_applies_cf_clearance_and_user_agent(monkeypatch):
    class _DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "signupReady": True,
                "title": "Sign up",
                "bodySnippet": "signup page ready",
                "userAgent": "Mozilla/5.0 Test Solver",
                "cookies": [
                    {
                        "name": "cf_clearance",
                        "value": "demo-clearance",
                        "domain": "accounts.x.ai",
                        "path": "/",
                    }
                ],
            }

    class _DummySession:
        def __init__(self) -> None:
            self.cookies = _DummyCookies()

    monkeypatch.setattr(runner_module.http_requests, "get", lambda *args, **kwargs: _DummyResponse())

    runner = runner_module.RegisterRunner(target_count=1, thread_count=1)
    session = _DummySession()

    ok = runner._preflight_signup_with_solver(session, "Mozilla/5.0 Original")

    assert ok is True
    assert session.cookies.get("cf_clearance") == "demo-clearance"
    assert runner._cf_clearance == "demo-clearance"
    assert runner._solver_user_agent == "Mozilla/5.0 Test Solver"
