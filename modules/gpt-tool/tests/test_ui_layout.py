import re
from pathlib import Path


def test_desktop_layout_is_full_width_with_readable_type() -> None:
    css = (Path(__file__).parents[1] / "web" / "styles.css").read_text(encoding="utf-8")

    assert ".topbar, .page, .footer { width: calc(100% - 32px);" in css
    assert "body { min-width: 320px; margin: 0;" in css and "font-size: 16px;" in css
    assert ".formats label" in css and "font-size: 15px;" in css
    assert 'textarea, input[type="text"], input[type="number"]' in css and "font-size: 15px;" in css


def test_dark_theme_uses_high_contrast_text() -> None:
    css = (Path(__file__).parents[1] / "web" / "styles.css").read_text(encoding="utf-8")

    assert "--ink: #ffffff;" in css
    assert "--muted: #b7c5c4;" in css
    assert "--muted-strong: #e7eeed;" in css
    assert "textarea::placeholder, input::placeholder { color: var(--muted); }" in css
    assert ".footer" in css and "color: var(--muted);" in css


def test_export_actions_use_clear_labels_and_icons() -> None:
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    run = re.search(r'<button[^>]+id="run-export"[^>]*>(.*?)</button>', html).group(1)
    open_files = re.search(r'<button[^>]+id="open-out"[^>]*>(.*?)</button>', html).group(1)

    assert "Chạy tool" in run and "<svg" in run and "↗" not in run
    assert "Mở file hệ thống" in open_files and "<svg" in open_files and "↗" not in open_files
    assert "confirm(" not in source


def test_proxy_is_hidden_and_workers_is_localized() -> None:
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "web" / "styles.css").read_text(encoding="utf-8")

    assert '<div class="field" hidden><label for="proxy">' in html
    assert 'id="proxy"' in html
    assert '<label for="workers">Số luồng</label>' in html
    assert "[hidden] { display: none !important; }" in css


def test_browser_tab_uses_shoptaikhoan_branding() -> None:
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")

    assert "<title>shoptaikhoan - auto import</title>" in html
    assert 'rel="icon"' in html and "data:image/svg+xml" in html


def test_workers_control_is_in_workspace_header() -> None:
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")

    assert html.index('id="workers"') < html.index('id="tab-export"')
    assert 'class="workspace-header-actions"' in html
    assert 'class="control-row"' not in html


def test_workspace_has_one_click_shutdown_action() -> None:
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    shutdown = re.search(r'<button[^>]+id="shutdown-tool"[^>]*>(.*?)</button>', html).group(1)

    assert "Tắt tool" in shutdown and "<svg" in shutdown
    assert html.index('id="shutdown-tool"') < html.index('id="tab-export"')
    assert 'fetch("/api/shutdown", { method: "POST"' in source


def test_run_button_becomes_running_job_stop_action() -> None:
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")

    assert 'id="stop-job"' not in html
    assert 'const RUN_BUTTON_HTML = runExport.innerHTML;' in source
    assert 'runExport.dataset.action = busy ? "stop" : "run";' in source
    assert 'Dừng tiến trình' in source
    assert 'if (runExport.dataset.action === "stop")' in source
    assert 'fetch(`/api/jobs/${encodeURIComponent(currentJobId)}/stop`' in source
    assert 'const cancelled = items.filter((row) => row.state === "cancelled").length;' in source
    assert "const done = ok + fail + cancelled;" in source
