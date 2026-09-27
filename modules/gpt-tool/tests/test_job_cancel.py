import http.client
import json
import threading

import pytest

from gpt_tool import export, http_client, server


def test_http_client_rejects_new_requests_after_cancel() -> None:
    cancelled = threading.Event()
    session = http_client.build_client(cancel_event=cancelled)
    cancelled.set()

    with pytest.raises(InterruptedError, match="Đã dừng"):
        session.get("https://example.com")


def test_export_bulk_marks_running_and_queued_accounts_cancelled(tmp_path, monkeypatch) -> None:
    cancelled = threading.Event()
    started = threading.Event()

    def fake_login(_creds, _proxy, cancel_event=None):
        started.set()
        cancel_event.wait(2)
        raise InterruptedError("Đã dừng")

    monkeypatch.setattr(export, "login_keep_session", fake_login)
    result: list[export.Outcome] = []
    thread = threading.Thread(
        target=lambda: result.extend(
            export.export_bulk(
                [
                    "one@example.com|password|JBSWY3DPEHPK3PXP",
                    "two@example.com|password|JBSWY3DPEHPK3PXP",
                ],
                "sub2api",
                tmp_path,
                workers=1,
                cancel_event=cancelled,
            )
        )
    )
    thread.start()
    assert started.wait(1)
    cancelled.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(result) == 2
    assert all(item.cancelled and item.step == "cancelled" for item in result)
    assert not (tmp_path / "failed.txt").exists()


def test_job_stop_endpoint_sets_cancel_event() -> None:
    job_id = "cancel-test"
    cancelled = threading.Event()
    with server._lock:
        server._jobs[job_id] = {"id": job_id, "done": False, "cancelling": False, "results": []}
        server._job_cancellations[job_id] = cancelled

    httpd, _ = server._bind("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

    try:
        connection.request(
            "POST",
            f"/api/jobs/{job_id}/stop",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 200
        assert payload == {"ok": True}
        assert cancelled.is_set()
        assert server._jobs[job_id]["cancelling"] is True
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        with server._lock:
            server._jobs.pop(job_id, None)
            server._job_cancellations.pop(job_id, None)
