from app.services.register.services.turnstile_service import TurnstileService


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_get_response_sets_timeout_error_when_solver_keeps_processing(monkeypatch):
    service = TurnstileService(solver_url="http://127.0.0.1:5072")

    def _fake_get(*args, **kwargs):
        return _DummyResponse({"status": "processing"})

    monkeypatch.setattr(
        "app.services.register.services.turnstile_service.requests.get",
        _fake_get,
    )

    token = service.get_response(
        "task-1",
        max_retries=2,
        initial_delay=0,
        retry_delay=0,
    )

    assert token is None
    assert service.last_error == "solver timeout waiting for token"


def test_get_response_uses_solver_error_description(monkeypatch):
    service = TurnstileService(solver_url="http://127.0.0.1:5072")

    def _fake_get(*args, **kwargs):
        return _DummyResponse(
            {
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "timeout-no-token",
            }
        )

    monkeypatch.setattr(
        "app.services.register.services.turnstile_service.requests.get",
        _fake_get,
    )

    token = service.get_response(
        "task-2",
        max_retries=1,
        initial_delay=0,
        retry_delay=0,
    )

    assert token is None
    assert service.last_error == "timeout-no-token"
