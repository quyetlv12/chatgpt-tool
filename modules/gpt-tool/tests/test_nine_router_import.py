import hashlib
import json
from pathlib import Path
from urllib import error

import pytest

from gpt_tool import server


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_nine_router_cli_token_matches_official_local_cli_algorithm(tmp_path: Path) -> None:
    (tmp_path / "auth").mkdir()
    (tmp_path / "machine-id").write_text("machine-id", encoding="utf-8")
    (tmp_path / "auth" / "cli-secret").write_text("cli-secret", encoding="utf-8")

    token = server._nine_router_cli_token(tmp_path)

    expected = hashlib.sha256(b"machine-id9r-cli-authcli-secret").hexdigest()[:16]
    assert token == expected


def test_nine_router_import_posts_accounts_without_exposing_tokens(monkeypatch) -> None:
    captured = {}
    bundle = [{"email": "one@example.com", "accessToken": "secret-access"}]
    monkeypatch.setattr(server, "_nine_router_cli_token", lambda: "cli-token")

    def fake_urlopen(request, timeout):
        captured.update(request=request, timeout=timeout)
        return _Response({"success": 1, "failed": 0, "results": [{"index": 0, "ok": True, "id": "safe-id"}]})

    monkeypatch.setattr(server.request, "urlopen", fake_urlopen)

    result = server._import_nine_router(bundle)

    sent = captured["request"]
    assert sent.full_url == "http://127.0.0.1:20128/api/oauth/codex/bulk-import"
    assert sent.get_header("X-9r-cli-token") == "cli-token"
    assert json.loads(sent.data) == {"accounts": bundle}
    assert result == {"success": 1, "failed": 0, "results": [{"index": 0, "ok": True, "id": "safe-id"}]}


def test_public_job_hides_nine_router_tokens_but_reports_delivery() -> None:
    job = {
        "results": [
            {
                "ok": True,
                "path": "/private/out/one.json",
                "nine_router_payload": {"accessToken": "secret"},
                "nine_router_sent": True,
            }
        ]
    }

    public = server._public_job(job)

    assert "nine_router_payload" not in public["results"][0]
    assert public["results"][0]["nine_router_sent"] is True


def test_nine_router_connection_error_is_safe(monkeypatch) -> None:
    monkeypatch.setattr(server, "_nine_router_cli_token", lambda: "cli-token")
    monkeypatch.setattr(
        server.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error.URLError("secret detail")),
    )

    with pytest.raises(RuntimeError, match="Không kết nối được 9Router") as caught:
        server._import_nine_router([{"accessToken": "secret-access"}])

    assert "secret" not in str(caught.value)
