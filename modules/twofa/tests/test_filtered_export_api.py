from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import server  # noqa: E402
from service import TwoFAFlowError  # noqa: E402


class _ExportManager:
    settings: dict = {}

    def worker_health(self) -> dict:
        return {
            "started": True,
            "configured": 3,
            "active": 3,
            "busy": 0,
            "queued": 0,
            "restarts": 1,
            "persistence_failures": 0,
            "last_worker_error": None,
            "last_persistence_error": None,
            "degraded": False,
        }

    def export_filtered(self, view: str) -> list[str]:
        if view == "usage-low":
            return ["demo@example.com|password|NEWSECRET"]
        if view == "usage-full":
            return ["full@example.com|password|SECRET100"]
        if view == "free-no-usage":
            return ["free@example.com|password|SECRET-FREE"]
        return []

    def raw_combo(self, job_id: str) -> str:
        if job_id == "demo-job":
            return "demo@example.com|password|NEWSECRET"
        raise KeyError(job_id)

    def twofa_history(self) -> list[dict]:
        return [{
            "id": 1,
            "job_id": "demo-job",
            "email": "demo@example.com",
            "changed_at": 123.5,
            "raw": "demo@example.com|password|NEWSECRET",
        }]

    def clear_failed(self) -> int:
        return 2

    def snapshots(self) -> list[dict]:
        return [{"id": "live-job", "status": "success", "account_state": "live"}]

    def enqueue_change_2fa(self, job_id: str) -> dict:
        if job_id == "blocked-job":
            raise ValueError("Tài khoản chưa đủ điều kiện đổi 2FA")
        return {
            "id": job_id,
            "email": "demo@example.com",
            "mode": "change_2fa",
            "status": "queued",
            "account_state": "live",
        }

    def recheck(self, job_id: str) -> dict:
        if job_id == "blocked-job":
            raise ValueError("Chỉ có thể check lại tài khoản đã hoàn tất thành công")
        if job_id == "missing-job":
            raise KeyError(job_id)
        return {
            "id": job_id,
            "email": "demo@example.com",
            "mode": "check_only",
            "status": "queued",
            "account_state": "unknown",
        }

    async def refresh_usage(self, job_id: str) -> dict:
        if job_id == "blocked-job":
            raise ValueError("Tài khoản chưa đủ điều kiện đọc lại Usage")
        if job_id == "usage-error":
            raise TwoFAFlowError(
                "Chưa đọc được Usage; vui lòng thử lại sau",
                account_state="live",
            )
        return {
            "id": job_id,
            "email": "demo@example.com",
            "mode": "check_only",
            "status": "success",
            "account_state": "live",
            "usage": {"used_percent": 28.0, "remaining_percent": 72.0},
            "usage_refreshing": False,
        }


class FilteredExportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_manager = server.manager
        server.manager = _ExportManager()
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server.manager = self.original_manager

    def test_requires_local_auth_token(self) -> None:
        response = self.client.get("/api/jobs/export?view=usage-low")
        self.assertEqual(response.status_code, 401)

    def test_health_exposes_worker_pool_state(self) -> None:
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["worker_health"]["active"], 3)
        self.assertFalse(response.json()["worker_health"]["degraded"])

    def test_bootstrap_exposes_worker_pool_state(self) -> None:
        response = self.client.get("/api/bootstrap")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["worker_health"]["configured"], 3)

    def test_passkey_handoff_routes_are_registered(self) -> None:
        route_paths = {route.path for route in server.app.routes}

        self.assertIn("/passkey", route_paths)
        self.assertIn("/api/jobs/{job_id}/passkey/start", route_paths)
        self.assertIn("/api/passkey/launch/{launch_token}", route_paths)

    def test_passkey_page_is_a_dedicated_route(self) -> None:
        response = self.client.get("/passkey")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="passkey-workspace"', response.text)
        self.assertNotIn('<dialog id="passkey-workspace"', response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertIn("passkey-ui.js?v=1.1.2", response.text)

    def test_dashboard_document_and_assets_are_never_cached(self) -> None:
        for path in ("/", "/assets/index.css", "/assets/app.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
                self.assertEqual(response.headers["pragma"], "no-cache")

    def test_passkey_ui_script_is_never_cached(self) -> None:
        response = self.client.get("/assets/passkey-ui.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertNotIn("window.confirm(", response.text)

    def test_returns_no_store_text_attachment_and_count(self) -> None:
        response = self.client.get(
            "/api/jobs/export?view=usage-low",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "demo@example.com|password|NEWSECRET\n")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-export-count"], "1")
        self.assertIn("twofa-usage-under-50.txt", response.headers["content-disposition"])

    def test_exports_usage_full_view(self) -> None:
        response = self.client.get(
            "/api/jobs/export?view=usage-full",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "full@example.com|password|SECRET100\n")
        self.assertEqual(response.headers["x-export-count"], "1")
        self.assertIn("twofa-usage-100.txt", response.headers["content-disposition"])

    def test_exports_free_without_usage_view(self) -> None:
        response = self.client.get(
            "/api/jobs/export?view=free-no-usage",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "free@example.com|password|SECRET-FREE\n")
        self.assertEqual(response.headers["x-export-count"], "1")
        self.assertIn("twofa-free-plus-no-usage.txt", response.headers["content-disposition"])

    def test_rejects_unknown_filter(self) -> None:
        response = self.client.get(
            "/api/jobs/export?view=unknown",
            headers={"X-Auth-Token": server.auth_token},
        )
        self.assertEqual(response.status_code, 422)

    def test_raw_combo_requires_local_auth_token(self) -> None:
        response = self.client.get("/api/jobs/demo-job/raw")
        self.assertEqual(response.status_code, 401)

    def test_raw_combo_returns_exact_no_store_plain_text(self) -> None:
        response = self.client.get(
            "/api/jobs/demo-job/raw",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "demo@example.com|password|NEWSECRET")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_raw_combo_returns_404_for_unknown_job(self) -> None:
        response = self.client.get(
            "/api/jobs/missing/raw",
            headers={"X-Auth-Token": server.auth_token},
        )
        self.assertEqual(response.status_code, 404)

    def test_twofa_history_requires_local_auth_token(self) -> None:
        response = self.client.get("/api/twofa-history")
        self.assertEqual(response.status_code, 401)

    def test_twofa_history_returns_no_store_sensitive_records(self) -> None:
        response = self.client.get(
            "/api/twofa-history",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(
            response.json()["history"][0]["raw"],
            "demo@example.com|password|NEWSECRET",
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_clear_failed_requires_local_auth_token(self) -> None:
        response = self.client.delete("/api/jobs/failed")
        self.assertEqual(response.status_code, 401)

    def test_clear_failed_returns_deleted_count(self) -> None:
        response = self.client.delete(
            "/api/jobs/failed",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "deleted": 2,
                "jobs": [
                    {"id": "live-job", "status": "success", "account_state": "live"}
                ],
            },
        )

    def test_row_2fa_change_requires_local_auth_token(self) -> None:
        response = self.client.post("/api/jobs/demo-job/change-2fa")
        self.assertEqual(response.status_code, 401)

    def test_row_2fa_change_returns_safe_queued_snapshot(self) -> None:
        response = self.client.post(
            "/api/jobs/demo-job/change-2fa",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["mode"], "change_2fa")
        self.assertEqual(response.json()["job"]["status"], "queued")
        self.assertNotIn("password", response.json()["job"])
        self.assertNotIn("secret", response.json()["job"])

    def test_row_2fa_change_rejects_ineligible_job(self) -> None:
        response = self.client.post(
            "/api/jobs/blocked-job/change-2fa",
            headers={"X-Auth-Token": server.auth_token},
        )
        self.assertEqual(response.status_code, 409)

    def test_recheck_requires_local_auth_token(self) -> None:
        response = self.client.post("/api/jobs/demo-job/recheck")
        self.assertEqual(response.status_code, 401)

    def test_recheck_returns_safe_queued_snapshot(self) -> None:
        response = self.client.post(
            "/api/jobs/demo-job/recheck",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["mode"], "check_only")
        self.assertEqual(response.json()["job"]["status"], "queued")
        self.assertNotIn("password", response.json()["job"])
        self.assertNotIn("secret", response.json()["job"])

    def test_recheck_rejects_ineligible_job(self) -> None:
        response = self.client.post(
            "/api/jobs/blocked-job/recheck",
            headers={"X-Auth-Token": server.auth_token},
        )
        self.assertEqual(response.status_code, 409)

    def test_recheck_returns_404_for_missing_job(self) -> None:
        response = self.client.post(
            "/api/jobs/missing-job/recheck",
            headers={"X-Auth-Token": server.auth_token},
        )
        self.assertEqual(response.status_code, 404)

    def test_usage_refresh_requires_local_auth_token(self) -> None:
        response = self.client.post("/api/jobs/demo-job/refresh-usage")
        self.assertEqual(response.status_code, 401)

    def test_usage_refresh_returns_safe_updated_snapshot(self) -> None:
        response = self.client.post(
            "/api/jobs/demo-job/refresh-usage",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["usage"]["used_percent"], 28.0)
        self.assertFalse(response.json()["job"]["usage_refreshing"])
        self.assertNotIn("password", response.json()["job"])
        self.assertNotIn("secret", response.json()["job"])

    def test_usage_refresh_rejects_ineligible_job(self) -> None:
        response = self.client.post(
            "/api/jobs/blocked-job/refresh-usage",
            headers={"X-Auth-Token": server.auth_token},
        )
        self.assertEqual(response.status_code, 409)

    def test_usage_refresh_returns_safe_upstream_error(self) -> None:
        response = self.client.post(
            "/api/jobs/usage-error/refresh-usage",
            headers={"X-Auth-Token": server.auth_token},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "Chưa đọc được Usage; vui lòng thử lại sau",
        )


if __name__ == "__main__":
    unittest.main()
