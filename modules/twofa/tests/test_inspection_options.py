from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from jobs import TwoFAJob, TwoFAJobManager  # noqa: E402
from service import TwoFAService  # noqa: E402


class _SettingsRepo:
    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def list(self, _prefix=None):
        return dict(self.values)

    def set(self, key, value) -> None:
        self.values[key] = value


class _JobRepo:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.created: list[dict] = []
        self.updated: list[tuple[str, str, dict]] = []

    def list_all(self):
        return list(self.rows)

    def get_logs(self, _job_id):
        return []

    def create(self, payload) -> None:
        self.created.append(dict(payload))

    def update_status(self, job_id, status, **kwargs) -> None:
        self.updated.append((job_id, status, kwargs))

    def append_log(self, _job_id, _line) -> None:
        return None


class InspectionServiceOptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_readers_do_not_make_usage_or_payment_requests(self) -> None:
        calls = {"usage": 0, "payment": 0}

        async def login_fn(**_kwargs):
            return {
                "accessToken": "test-token",
                "account": {"id": "acct-test", "planType": "plus"},
            }

        async def entitlement_fn(**_kwargs):
            return {"plan": "plus", "is_plus": True}

        async def usage_fn(**_kwargs):
            calls["usage"] += 1
            return {"used_percent": 10.0, "remaining_percent": 90.0}

        async def payment_fn(**_kwargs):
            calls["payment"] += 1
            return []

        service = TwoFAService(
            login_fn=login_fn,
            entitlement_fn=entitlement_fn,
            usage_fn=usage_fn,
            payment_methods_fn=payment_fn,
            login_attempts=1,
        )

        result = await service.check(
            email="demo@example.com",
            password="test-password",
            secret="TESTSECRET",
            timeout=30,
            log=lambda _message: None,
            read_usage=False,
            read_payment_methods=False,
        )

        self.assertEqual(calls, {"usage": 0, "payment": 0})
        self.assertEqual(result.plan, "plus")
        self.assertIsNone(result.usage)
        self.assertIsNone(result.payment_methods)


class _CapturingService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def check(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            secret=kwargs["secret"],
            login_verified=True,
            account_state="live",
            plan="plus",
            plan_source="session",
            usage=None,
            payment_methods=None,
        )


class InspectionJobOptionTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_are_enabled_and_new_jobs_snapshot_settings(self) -> None:
        repo = _JobRepo()
        manager = TwoFAJobManager(repo, _SettingsRepo())

        snapshot = manager.add(
            ["demo@example.com|test-password|TESTSECRET"],
            mode="check_only",
        )[0]

        self.assertTrue(snapshot["usage_enabled"])
        self.assertTrue(snapshot["payment_methods_enabled"])
        state = json.loads(repo.created[0]["account_check"])
        self.assertTrue(state["usage_enabled"])
        self.assertTrue(state["payment_methods_enabled"])

    def test_disabled_settings_are_snapshotted_and_recovered(self) -> None:
        settings = _SettingsRepo({
            "twofa.read_usage": False,
            "twofa.read_payment_methods": False,
        })
        repo = _JobRepo()
        manager = TwoFAJobManager(repo, settings)
        created = manager.add(
            ["disabled@example.com|test-password|TESTSECRET"],
            mode="check_only",
        )[0]
        state = repo.created[0]["account_check"]

        recovered = TwoFAJobManager(_JobRepo([{
            "id": created["id"],
            "email": "disabled@example.com",
            "password": "test-password",
            "secret": "TESTSECRET",
            "status": "success",
            "job_type": "twofa_community",
            "account_check": state,
        }]), _SettingsRepo())
        snapshot = recovered.snapshots()[0]

        self.assertFalse(snapshot["usage_enabled"])
        self.assertFalse(snapshot["payment_methods_enabled"])

    def test_legacy_recovery_defaults_both_readers_to_enabled(self) -> None:
        manager = TwoFAJobManager(_JobRepo([{
            "id": "legacy-job",
            "email": "legacy@example.com",
            "password": "test-password",
            "secret": "TESTSECRET",
            "status": "success",
            "job_type": "twofa_community",
            "account_check": json.dumps({"mode": "check_only"}),
        }]), _SettingsRepo())

        snapshot = manager.snapshots()[0]
        self.assertTrue(snapshot["usage_enabled"])
        self.assertTrue(snapshot["payment_methods_enabled"])

    async def test_worker_uses_job_snapshot_and_retry_preserves_it(self) -> None:
        service = _CapturingService()
        manager = TwoFAJobManager(_JobRepo(), _SettingsRepo({
            "twofa.read_usage": False,
            "twofa.read_payment_methods": False,
        }), service=service)
        job = TwoFAJob(
            id="snapshot-job",
            email="demo@example.com",
            password="test-password",
            secret="TESTSECRET",
            mode="check_only",
            usage_enabled=False,
            payment_methods_enabled=False,
        )
        manager.jobs[job.id] = job
        manager.order.append(job.id)

        await manager._run(job)
        self.assertFalse(service.calls[0]["read_usage"])
        self.assertFalse(service.calls[0]["read_payment_methods"])

        manager.settings["twofa.read_usage"] = True
        manager.settings["twofa.read_payment_methods"] = True
        retried = manager.retry(job.id)
        self.assertFalse(retried["usage_enabled"])
        self.assertFalse(retried["payment_methods_enabled"])

    def test_recheck_and_change_2fa_take_current_settings(self) -> None:
        manager = TwoFAJobManager(_JobRepo(), _SettingsRepo())
        job = TwoFAJob(
            id="live-job",
            email="demo@example.com",
            password="test-password",
            secret="TESTSECRET",
            mode="change_2fa",
            status="success",
            account_state="live",
            login_verified=True,
        )
        manager.jobs[job.id] = job
        manager.order.append(job.id)
        manager.settings["twofa.read_usage"] = False
        manager.settings["twofa.read_payment_methods"] = False

        rechecked = manager.recheck(job.id)
        self.assertFalse(rechecked["usage_enabled"])
        self.assertFalse(rechecked["payment_methods_enabled"])

        job.status = "success"
        job.account_state = "live"
        manager.settings["twofa.read_usage"] = True
        manager.settings["twofa.read_payment_methods"] = True
        changed = manager.enqueue_change_2fa(job.id)
        self.assertTrue(changed["usage_enabled"])
        self.assertTrue(changed["payment_methods_enabled"])

    async def test_refresh_usage_is_rejected_when_job_reader_is_disabled(self) -> None:
        manager = TwoFAJobManager(_JobRepo(), _SettingsRepo())
        job = TwoFAJob(
            id="usage-disabled",
            email="demo@example.com",
            password="test-password",
            secret="TESTSECRET",
            mode="check_only",
            status="success",
            account_state="live",
            usage_enabled=False,
        )
        manager.jobs[job.id] = job

        with self.assertRaisesRegex(ValueError, "đã tắt"):
            await manager.refresh_usage(job.id)

    def test_usage_disabled_job_is_not_classified_as_missing_usage(self) -> None:
        manager = TwoFAJobManager(_JobRepo(), _SettingsRepo())
        job = TwoFAJob(
            id="intentional-no-usage",
            email="demo@example.com",
            password="test-password",
            secret="TESTSECRET",
            mode="check_only",
            status="success",
            account_state="live",
            plan="plus",
            usage=None,
            usage_enabled=False,
        )

        self.assertFalse(manager._matches_export_filter(job, "free-no-usage"))


if __name__ == "__main__":
    unittest.main()
