import json
from pathlib import Path

import pytest

from gpt_tool import server


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_copy_payload_returns_one_account_by_result_index(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "OUT", tmp_path)
    artifact = tmp_path / "one.json"
    _write_json(artifact, {"email": "one@example.com", "token": "secret-one"})
    job = {"results": [{"ok": True, "index": 4, "path": str(artifact)}]}

    payload = server._copy_payload(job, "4")

    assert payload == {"email": "one@example.com", "token": "secret-one"}


def test_copy_payload_returns_all_successful_accounts_as_array(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "OUT", tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_json(first, {"account": 1})
    _write_json(second, {"account": 2})
    job = {
        "results": [
            {"ok": True, "index": 0, "path": str(first)},
            {"ok": False, "index": 1, "error": "failed"},
            {"ok": True, "index": 2, "path": str(second)},
        ]
    }

    assert server._copy_payload(job, "all") == [{"account": 1}, {"account": 2}]


def test_copy_payload_rejects_artifact_outside_output_directory(tmp_path, monkeypatch) -> None:
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(server, "OUT", out)
    outside = tmp_path / "outside.json"
    _write_json(outside, {"token": "must-not-leak"})
    job = {"results": [{"ok": True, "index": 0, "path": str(outside)}]}

    with pytest.raises(server.ArtifactError, match="JSON không khả dụng"):
        server._copy_payload(job, "0")


def test_public_job_hides_file_paths_and_marks_copyable_results() -> None:
    job = {
        "id": "job123",
        "done": True,
        "results": [
            {"ok": True, "index": 0, "path": "/private/out/one.json"},
            {"ok": False, "index": 1, "path": None, "error": "failed"},
        ],
        "running": {},
        "total": 2,
        "workers": 2,
        "error": None,
    }

    public = server._public_job(job)

    assert "path" not in public["results"][0]
    assert public["results"][0]["copyable"] is True
    assert public["results"][1]["copyable"] is False
