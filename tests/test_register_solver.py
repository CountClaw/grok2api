import sys

from app.services.register import solver as solver_module


def test_ensure_playwright_browsers_skips_install_when_chromium_preinstalled(monkeypatch, tmp_path):
    browser_root = tmp_path / "ms-playwright"
    (browser_root / "chromium-1234").mkdir(parents=True)

    process = solver_module.TurnstileSolverProcess(
        solver_module.SolverConfig(url="http://127.0.0.1:5072")
    )
    process._repo_root = tmp_path
    process._actual_browser_type = "chromium"

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))

    called = {"value": False}

    def _fake_check_call(*args, **kwargs):
        called["value"] = True

    monkeypatch.setattr(solver_module.subprocess, "check_call", _fake_check_call)

    process._ensure_playwright_browsers(sys.executable)

    assert called["value"] is False
    assert (tmp_path / "data" / ".locks" / "playwright_chromium_v1.lock").exists()
