import http.client
import json
import threading
from pathlib import Path

from gpt_tool import ensure_deps, server


def test_runtime_paths_use_project_out_when_running_from_source(tmp_path) -> None:
    web, out = server._runtime_paths(frozen=False, bundle_root=tmp_path, home=tmp_path / "home")

    assert web == tmp_path / "web"
    assert out == tmp_path / "out"


def test_runtime_paths_use_home_directory_when_frozen(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    home = tmp_path / "home"

    web, out = server._runtime_paths(frozen=True, bundle_root=bundle, home=home)

    assert web == bundle / "web"
    assert out == home / "GPT-Tool" / "out"


def test_frozen_app_never_attempts_runtime_pip_install(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(ensure_deps.sys, "frozen", True, raising=False)
    monkeypatch.setattr(ensure_deps, "chrome_supported", lambda: False)
    monkeypatch.setattr(ensure_deps, "_pip", lambda *args: calls.append(args))

    ensure_deps.ensure_deps()

    assert calls == []


def test_windows_build_produces_one_self_contained_executable() -> None:
    root = Path(__file__).parents[1]
    build_script = (root / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "build-desktop.yml").read_text(encoding="utf-8")

    assert "-m nuitka" in build_script
    assert '"--mode=onefile"' in build_script
    assert '"--windows-console-mode=disable"' in build_script
    assert '"--include-data-dir=web=web"' in build_script
    assert 'dist\\GPT-Tool.exe' in build_script
    assert 'dist\\GPT-Tool.exe' in workflow
    assert "path: dist/GPT-Tool.exe" in workflow


def test_nuitka_compiled_app_never_attempts_runtime_pip_install(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(ensure_deps, "__compiled__", object(), raising=False)
    monkeypatch.setattr(ensure_deps, "chrome_supported", lambda: False)
    monkeypatch.setattr(ensure_deps, "_pip", lambda *args: calls.append(args))

    ensure_deps.ensure_deps()

    assert calls == []


def test_nuitka_onefile_loads_web_assets_from_its_extracted_root(tmp_path) -> None:
    root = Path(__file__).parents[1]
    server_source = (root / "gpt_tool" / "server.py").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "build-desktop.yml").read_text(encoding="utf-8")

    assert server._module_root(tmp_path / "server.py", compiled=True) == tmp_path
    assert server._module_root(tmp_path / "gpt_tool" / "server.py", compiled=False) == tmp_path
    assert "containing_dir" not in server_source
    assert 'BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))' in server_source
    assert 'Invoke-WebRequest "http://127.0.0.1:8877/"' in workflow
    assert 'shoptaikhoan - auto import' in workflow


def test_windowed_server_logs_safely_without_standard_error(monkeypatch) -> None:
    handler = object.__new__(server.Handler)
    monkeypatch.setattr(server.sys, "stderr", None)

    handler.log_message("%s", "request")


def test_windowed_server_responds_without_standard_error(monkeypatch) -> None:
    monkeypatch.setattr(server.sys, "stderr", None)
    httpd, _ = server._bind("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=2)

    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 200
        assert payload["ok"] is True
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_shutdown_endpoint_stops_server() -> None:
    httpd, _ = server._bind("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

    try:
        connection.request(
            "POST",
            "/api/shutdown",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        thread.join(timeout=2)

        assert response.status == 200
        assert payload == {"ok": True}
        assert not thread.is_alive()
    finally:
        connection.close()
        if thread.is_alive():
            httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_shutdown_endpoint_rejects_cross_origin_request() -> None:
    httpd, _ = server._bind("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=2)

    try:
        connection.request(
            "POST",
            "/api/shutdown",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": "https://example.com"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 403
        assert payload == {"error": "forbidden"}
        assert thread.is_alive()
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
