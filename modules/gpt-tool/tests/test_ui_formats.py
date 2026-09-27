import re
from pathlib import Path


def test_ui_keeps_three_formats_and_defaults_to_cockpit() -> None:
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    formats_block = source.split("];", 1)[0]

    assert re.findall(r'\["([^"]+)", "([^"]+)"\]', formats_block) == [
        ("cockpit", "Cockpit"),
        ("9router", "9router"),
        ("sub2api", "sub2api"),
    ]
    assert 'const defaultFormat = FORMATS.some(([id]) => id === prefs.format) ? prefs.format : "cockpit";' in source
    assert 'cockpit_import: format === "cockpit"' in source
    assert 'nine_router_import: format === "9router"' in source
    assert 'id="cockpit-import"' not in html
    assert 'id="nine-router-import"' not in html


def test_selected_format_confirmation_changes_by_destination() -> None:
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="format-confirmation"' in html
    assert 'id="format-confirmation-text"' in html
    assert 'sub2api: "Xuất JSON sub2api"' in source
    assert 'cockpit: "Tự nhập vào Cockpit"' in source
    assert '"9router": "Tự thêm vào 9Router"' in source
    assert "updateFormatConfirmation();" in source
