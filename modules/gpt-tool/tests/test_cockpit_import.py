from urllib.parse import parse_qs, urlparse

from gpt_tool import server


def test_cockpit_import_uses_single_use_local_bundle_without_tokens_in_deep_link(monkeypatch) -> None:
    opened: list[str] = []
    bundle = [{"email": "one@example.com", "access_token": "secret-access"}]
    monkeypatch.setattr(server, "_open_url", opened.append)

    count = server._queue_cockpit_import(bundle, "http://127.0.0.1:8765")

    assert count == 1
    assert len(opened) == 1
    deep_link = urlparse(opened[0])
    query = parse_qs(deep_link.query)
    assert deep_link.scheme == "cockpit-tools"
    assert query["platform"] == ["codex"]
    assert query["auto_import"] == ["true"]
    assert "secret-access" not in opened[0]

    import_url = urlparse(query["import_url"][0])
    token = import_url.path.rsplit("/", 1)[-1]
    assert server._take_cockpit_import(token) == bundle
    assert server._take_cockpit_import(token) is None


def test_public_job_hides_cockpit_tokens_but_reports_delivery() -> None:
    job = {
        "results": [
            {
                "ok": True,
                "index": 0,
                "path": "/private/out/one.json",
                "cockpit_payload": {"access_token": "secret"},
                "cockpit_sent": True,
            }
        ]
    }

    public = server._public_job(job)

    assert "path" not in public["results"][0]
    assert "cockpit_payload" not in public["results"][0]
    assert public["results"][0]["cockpit_sent"] is True
