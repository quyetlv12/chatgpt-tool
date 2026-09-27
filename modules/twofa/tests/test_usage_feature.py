from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from jobs import TwoFAJob, TwoFAJobManager  # noqa: E402
from service import TwoFAService  # noqa: E402
from session_phase import SessionError, _parse_codex_weekly_usage  # noqa: E402


SAMPLE_USAGE_RESPONSE = {
    "user_id": "user-demo",
    "account_id": "acct-demo",
    "email": "demo@example.com",
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 28,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 529489,
            "reset_at": 1787325361,
        },
        "secondary_window": None,
    },
    "credits": {
        "has_credits": False,
        "unlimited": False,
        "balance": "0",
    },
    "rate_limit_reset_credits": {"available_count": 3},
}


class UsageParserTests(unittest.TestCase):
    def test_parses_primary_weekly_window_and_safe_summary(self) -> None:
        result = _parse_codex_weekly_usage(SAMPLE_USAGE_RESPONSE)

        self.assertEqual(result["used_percent"], 28.0)
        self.assertEqual(result["remaining_percent"], 72.0)
        self.assertEqual(result["limit_window_seconds"], 604800)
        self.assertEqual(result["reset_after_seconds"], 529489)
        self.assertEqual(result["reset_at"], 1787325361)
        self.assertTrue(result["allowed"])
        self.assertFalse(result["limit_reached"])
        self.assertEqual(result["plan_type"], "plus")
        self.assertEqual(
            result["credits"],
            {"has_credits": False, "unlimited": False, "balance": "0"},
        )
        self.assertEqual(result["reset_credits"], {"available_count": 3})
        for sensitive_key in ("user_id", "account_id", "email"):
            self.assertNotIn(sensitive_key, result)

    def test_prefers_the_window_that_is_actually_weekly(self) -> None:
        payload = json.loads(json.dumps(SAMPLE_USAGE_RESPONSE))
        payload["rate_limit"]["primary_window"] = {
            "used_percent": 5,
            "limit_window_seconds": 18000,
        }
        payload["rate_limit"]["secondary_window"] = {
            "used_percent": 42.5,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 3600,
        }

        result = _parse_codex_weekly_usage(payload)

        self.assertEqual(result["used_percent"], 42.5)
        self.assertEqual(result["remaining_percent"], 57.5)

    def test_rejects_invalid_percentage(self) -> None:
        payload = json.loads(json.dumps(SAMPLE_USAGE_RESPONSE))
        payload["rate_limit"]["primary_window"]["used_percent"] = 101

        with self.assertRaises(SessionError):
            _parse_codex_weekly_usage(payload)

    def test_missing_or_invalid_reset_count_stays_unknown(self) -> None:
        missing = json.loads(json.dumps(SAMPLE_USAGE_RESPONSE))
        missing.pop("rate_limit_reset_credits")
        invalid = json.loads(json.dumps(SAMPLE_USAGE_RESPONSE))
        invalid["rate_limit_reset_credits"]["available_count"] = True

        self.assertIsNone(_parse_codex_weekly_usage(missing)["reset_credits"])
        self.assertIsNone(_parse_codex_weekly_usage(invalid)["reset_credits"])


class TwoFAServiceUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_returns_usage_and_passes_account_context(self) -> None:
        usage_calls: list[dict] = []

        async def login_fn(**_kwargs):
            return {
                "accessToken": "eyJ.demo.token",
                "__cookies": [{"name": "session", "value": "cookie-value"}],
                "account": {"id": "acct-demo", "planType": "plus"},
            }

        async def entitlement_fn(**_kwargs):
            return {"plan": "plus", "is_plus": True}

        async def usage_fn(**kwargs):
            usage_calls.append(kwargs)
            return _parse_codex_weekly_usage(SAMPLE_USAGE_RESPONSE)

        async def payment_methods_fn(**_kwargs):
            return []

        service = TwoFAService(
            login_fn=login_fn,
            entitlement_fn=entitlement_fn,
            usage_fn=usage_fn,
            payment_methods_fn=payment_methods_fn,
            login_attempts=1,
        )

        result = await service.check(
            email="demo@example.com",
            password="password",
            secret="JBSWY3DPEHPK3PXP",
            timeout=30,
            log=lambda _message: None,
        )

        self.assertEqual(result.plan, "plus")
        self.assertEqual(result.usage["used_percent"], 28.0)
        self.assertEqual(len(usage_calls), 1)
        self.assertEqual(usage_calls[0]["account_id"], "acct-demo")
        self.assertEqual(usage_calls[0]["access_token"], "eyJ.demo.token")

    async def test_usage_failure_does_not_fail_a_live_account_or_leak_error(self) -> None:
        logs: list[str] = []

        async def login_fn(**_kwargs):
            return {"accessToken": "eyJ.demo.token", "accountPlan": "plus"}

        async def entitlement_fn(**_kwargs):
            return {"plan": "plus", "is_plus": True}

        async def usage_fn(**_kwargs):
            raise RuntimeError("request failed with eyJ.secret.token")

        async def payment_methods_fn(**_kwargs):
            return []

        service = TwoFAService(
            login_fn=login_fn,
            entitlement_fn=entitlement_fn,
            usage_fn=usage_fn,
            payment_methods_fn=payment_methods_fn,
            login_attempts=1,
        )

        result = await service.check(
            email="demo@example.com",
            password="password",
            secret="JBSWY3DPEHPK3PXP",
            timeout=30,
            log=logs.append,
        )

        self.assertTrue(result.login_verified)
        self.assertIsNone(result.usage)
        self.assertTrue(any("Usage" in line for line in logs))
        self.assertFalse(any("eyJ.secret.token" in line for line in logs))


class _SettingsRepo:
    def list(self, _prefix=None):
        return {}


class _JobRepo:
    def __init__(self, rows=None):
        self.rows = rows or []

    def list_all(self):
        return self.rows

    def get_logs(self, _job_id):
        return []


class JobUsagePersistenceTests(unittest.TestCase):
    def test_usage_is_in_snapshot_state_and_recovery(self) -> None:
        usage = _parse_codex_weekly_usage(SAMPLE_USAGE_RESPONSE)
        manager = TwoFAJobManager(_JobRepo(), _SettingsRepo())
        job = TwoFAJob(
            id="job-1",
            email="demo@example.com",
            password="password",
            secret="secret",
            usage=usage,
        )

        self.assertEqual(job.snapshot()["usage"]["used_percent"], 28.0)
        state = manager._state(job)
        recovered = TwoFAJobManager(
            _JobRepo(
                [
                    {
                        "id": "job-1",
                        "email": "demo@example.com",
                        "password": "password",
                        "secret": "secret",
                        "status": "success",
                        "job_type": "twofa_community",
                        "account_check": state,
                        "created_at": 1,
                    }
                ]
            ),
            _SettingsRepo(),
        )

        self.assertEqual(recovered.jobs["job-1"].usage["remaining_percent"], 72.0)


if __name__ == "__main__":
    unittest.main()
