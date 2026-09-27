from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from jobs import TwoFAJob, TwoFAJobManager  # noqa: E402


class _SettingsRepo:
    def list(self, _prefix=None):
        return {}


class _JobRepo:
    def __init__(self):
        self.deleted: list[str] = []
        self.updated: list[tuple[str, str, dict]] = []

    def list_all(self):
        return []

    def delete(self, job_id: str) -> None:
        self.deleted.append(job_id)

    def update_status(self, job_id: str, status: str, **kwargs) -> None:
        self.updated.append((job_id, status, kwargs))


class FilteredExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job_repo = _JobRepo()
        self.manager = TwoFAJobManager(self.job_repo, _SettingsRepo())
        jobs = [
            TwoFAJob(
                id="queued-high",
                email="queued@example.com",
                password="password-1",
                secret="SECRET1",
                status="queued",
                usage={"used_percent": 99.99},
                created_at=1,
            ),
            TwoFAJob(
                id="running-low",
                email="running@example.com",
                password="password-2",
                secret="SECRET2",
                status="running",
                usage={"used_percent": 10},
                created_at=2,
            ),
            TwoFAJob(
                id="success-high",
                email="success@example.com",
                password="password-3",
                secret="NEWSECRET3",
                mode="change_2fa",
                status="success",
                login_verified=True,
                usage={"used_percent": 50.01},
                created_at=3,
            ),
            TwoFAJob(
                id="error-high",
                email="error@example.com",
                password="password-4",
                secret="SECRET4",
                status="error",
                usage={"used_percent": 100},
                created_at=4,
            ),
            TwoFAJob(
                id="cancelled-boundary",
                email="cancelled@example.com",
                password="password-5",
                secret="SECRET5",
                status="cancelled",
                usage={"used_percent": 50},
                created_at=5,
            ),
        ]
        self.manager.jobs = {job.id: job for job in jobs}
        self.manager.order = [job.id for job in jobs]

    def test_exports_reimportable_combos_for_each_filter(self) -> None:
        self.assertEqual(len(self.manager.export_filtered("all")), 5)
        self.assertEqual(
            self.manager.export_filtered("running"),
            [
                "queued@example.com|password-1|SECRET1",
                "running@example.com|password-2|SECRET2",
            ],
        )
        self.assertEqual(
            self.manager.export_filtered("success"),
            ["success@example.com|password-3|NEWSECRET3"],
        )
        self.assertEqual(
            self.manager.export_filtered("error"),
            [
                "error@example.com|password-4|SECRET4",
                "cancelled@example.com|password-5|SECRET5",
            ],
        )

    def test_usage_filter_is_strictly_below_50_percent(self) -> None:
        self.assertEqual(
            self.manager.export_filtered("usage-low"),
            [
                "running@example.com|password-2|SECRET2",
            ],
        )

    def test_usage_full_filter_matches_exactly_100_percent(self) -> None:
        self.assertEqual(
            self.manager.export_filtered("usage-full"),
            ["error@example.com|password-4|SECRET4"],
        )

    def test_free_without_usage_filter_matches_verified_live_free_and_plus_jobs(self) -> None:
        candidates = [
            TwoFAJob(
                id="free-missing",
                email="free-missing@example.com",
                password="password-6",
                secret="SECRET6",
                status="success",
                account_state="live",
                plan="free",
                usage=None,
                created_at=6,
            ),
            TwoFAJob(
                id="free-valid-zero",
                email="free-zero@example.com",
                password="password-7",
                secret="SECRET7",
                status="success",
                account_state="live",
                plan="free",
                usage={"used_percent": 0},
                created_at=7,
            ),
            TwoFAJob(
                id="plus-missing",
                email="plus-missing@example.com",
                password="password-8",
                secret="SECRET8",
                status="success",
                account_state="live",
                plan="plus",
                usage=None,
                created_at=8,
            ),
            TwoFAJob(
                id="free-error",
                email="free-error@example.com",
                password="password-9",
                secret="SECRET9",
                status="error",
                account_state="live",
                plan="FREE",
                usage=None,
                created_at=9,
            ),
        ]
        self.manager.jobs.update({job.id: job for job in candidates})
        self.manager.order.extend(job.id for job in candidates)

        self.assertEqual(
            self.manager.export_filtered("free-no-usage"),
            [
                "free-missing@example.com|password-6|SECRET6",
                "plus-missing@example.com|password-8|SECRET8",
            ],
        )

    def test_rejects_unknown_filter(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.export_filtered("unknown")

    def test_returns_exact_raw_combo_for_one_job(self) -> None:
        self.assertEqual(
            self.manager.raw_combo("running-low"),
            "running@example.com|password-2|SECRET2",
        )

    def test_raw_combo_uses_current_rotated_secret(self) -> None:
        job = self.manager.jobs["success-high"]
        job.secret = "LATESTSECRET"

        self.assertEqual(
            self.manager.raw_combo(job.id),
            "success@example.com|password-3|LATESTSECRET",
        )

    def test_raw_combo_rejects_unknown_job(self) -> None:
        with self.assertRaises(KeyError):
            self.manager.raw_combo("missing")

    def test_clears_failed_accounts_but_keeps_live_and_active_jobs(self) -> None:
        live_error = TwoFAJob(
            id="live-error",
            email="live-error@example.com",
            password="password-6",
            secret="SECRET6",
            status="error",
            account_state="live",
            created_at=6,
        )
        self.manager.jobs[live_error.id] = live_error
        self.manager.order.append(live_error.id)

        deleted = self.manager.clear_failed()

        self.assertEqual(deleted, 2)
        self.assertEqual(
            self.job_repo.deleted,
            ["error-high", "cancelled-boundary"],
        )
        self.assertEqual(
            self.manager.order,
            ["queued-high", "running-low", "success-high", "live-error"],
        )
        self.assertIn("live-error", self.manager.jobs)

    def test_enqueues_checked_live_account_for_2fa_change_and_output(self) -> None:
        checked_live = TwoFAJob(
            id="checked-live",
            email="checked@example.com",
            password="password-7",
            secret="OLDSECRET7",
            mode="check_only",
            status="success",
            account_state="live",
            plan="plus",
            login_verified=True,
            created_at=7,
        )
        self.manager.jobs[checked_live.id] = checked_live
        self.manager.order.append(checked_live.id)

        snapshot = self.manager.enqueue_change_2fa(checked_live.id)

        self.assertEqual(snapshot["mode"], "change_2fa")
        self.assertEqual(snapshot["status"], "queued")
        self.assertFalse(snapshot["login_verified"])
        self.assertNotIn("password", snapshot)
        self.assertNotIn("secret", snapshot)
        self.assertEqual(self.manager._queue.get_nowait(), checked_live.id)
        update = self.job_repo.updated[-1]
        self.assertEqual(update[:2], (checked_live.id, "queued"))
        self.assertNotIn("OLDSECRET7", str(snapshot))

        checked_live.status = "success"
        checked_live.login_verified = True
        checked_live.secret = "NEWSECRET7"
        self.assertIn(
            "checked@example.com|password-7|NEWSECRET7",
            self.manager.output(),
        )

    def test_rejects_row_2fa_change_for_non_live_or_non_checked_jobs(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.enqueue_change_2fa("error-high")
        with self.assertRaises(ValueError):
            self.manager.enqueue_change_2fa("success-high")
        with self.assertRaises(ValueError):
            self.manager.enqueue_change_2fa("running-low")


if __name__ == "__main__":
    unittest.main()
