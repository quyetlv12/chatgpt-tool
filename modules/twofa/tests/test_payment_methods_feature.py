from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from jobs import TwoFAJob, TwoFAJobManager  # noqa: E402
from service import TwoFAService  # noqa: E402
from session_phase import (  # noqa: E402
    SessionError,
    _parse_entitlement_plan,
    _parse_payment_methods,
    fetch_payment_methods,
)


SAMPLE_PAYMENT_RESPONSE = {
    "data": [
        {
            "id": "pm_private_identifier",
            "type": "card",
            "is_default": True,
            "card": {
                "brand": "visa",
                "last4": "4242",
                "exp_month": 12,
                "exp_year": 2030,
                "fingerprint": "private-fingerprint",
            },
            "billing_details": {
                "name": "Private Name",
                "address": {"line1": "Private address"},
            },
            "customer_id": "cus_private_identifier",
        }
    ],
    "raw_token": "must-not-survive",
}


class PaymentParserTests(unittest.TestCase):
    def test_entitlement_exposes_only_safe_billing_date(self) -> None:
        payload = {
            "accounts": {
                "default": {
                    "entitlement": {
                        "subscription_plan": "chatgptplusplan",
                        "has_active_subscription": True,
                        "expires_at": "2026-09-01T08:30:00Z",
                    }
                }
            }
        }

        self.assertEqual(_parse_entitlement_plan(payload)["expires"], "2026-09-01")
        self.assertEqual(_parse_entitlement_plan(payload)["payment_date"], "2026-08-01")
        payload["accounts"]["default"]["entitlement"]["expires_at"] = "2026-08-31T18:00:00Z"
        self.assertEqual(
            _parse_entitlement_plan(payload)["expires"],
            "2026-09-01",
            "expiry date must be converted to Vietnam time before truncating the date",
        )
        self.assertEqual(_parse_entitlement_plan(payload)["payment_date"], "2026-08-01")
        payload["accounts"]["default"]["entitlement"]["expires_at"] = "not-a-date"
        self.assertIsNone(_parse_entitlement_plan(payload)["expires"])
        self.assertIsNone(_parse_entitlement_plan(payload)["payment_date"])

    def test_parses_safe_card_summary_and_drops_private_fields(self) -> None:
        result = _parse_payment_methods(SAMPLE_PAYMENT_RESPONSE)

        self.assertEqual(
            result,
            [
                {
                    "type": "card",
                    "brand": "visa",
                    "last4": "4242",
                    "exp_month": 12,
                    "exp_year": 2030,
                    "is_default": True,
                }
            ],
        )
        encoded = json.dumps(result)
        for private_value in (
            "pm_private_identifier",
            "private-fingerprint",
            "Private Name",
            "Private address",
            "cus_private_identifier",
            "must-not-survive",
        ):
            self.assertNotIn(private_value, encoded)

    def test_supports_root_list_and_payment_methods_envelope(self) -> None:
        direct = _parse_payment_methods(
            [{"type": "card", "brand": "mastercard", "last_four": 1234}]
        )
        enveloped = _parse_payment_methods(
            {"payment_methods": [{"payment_method_type": "paypal"}]}
        )

        self.assertEqual(direct[0]["last4"], "1234")
        self.assertEqual(direct[0]["brand"], "mastercard")
        self.assertEqual(enveloped[0]["type"], "paypal")
        self.assertEqual(_parse_payment_methods({"data": {"payment_methods": []}}), [])

    def test_rejects_unknown_envelope_and_skips_unusable_items(self) -> None:
        with self.assertRaises(SessionError):
            _parse_payment_methods({"unexpected": {"secret": "private"}})

        self.assertEqual(_parse_payment_methods({"data": [None, {}, "bad"]}), [])


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return SAMPLE_PAYMENT_RESPONSE


class _FakeAsyncSession:
    last_init: dict | None = None
    last_get: dict | None = None

    def __init__(self, **kwargs):
        type(self).last_init = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **kwargs):
        type(self).last_get = {"url": url, **kwargs}
        return _FakeResponse()


class PaymentFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetcher_uses_get_query_account_context_and_session_auth(self) -> None:
        requests_module = types.ModuleType("curl_cffi.requests")
        requests_module.AsyncSession = _FakeAsyncSession
        curl_module = types.ModuleType("curl_cffi")
        curl_module.requests = requests_module

        with patch.dict(
            sys.modules,
            {"curl_cffi": curl_module, "curl_cffi.requests": requests_module},
        ):
            result = await fetch_payment_methods(
                access_token="test-bearer-token",
                account_id="acct-test",
                cookies=[{"name": "session", "value": "test-cookie"}],
            )

        self.assertEqual(result[0]["last4"], "4242")
        request = _FakeAsyncSession.last_get or {}
        self.assertEqual(
            request["url"],
            "https://chatgpt.com/backend-api/payments/payment_methods",
        )
        self.assertEqual(request["params"], {"account_id": "acct-test"})
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-bearer-token")
        self.assertIn("session=test-cookie", request["headers"]["Cookie"])

    async def test_fetcher_rejects_invalid_account_id_before_network(self) -> None:
        with self.assertRaises(SessionError):
            await fetch_payment_methods(
                access_token="test-token",
                account_id="invalid\naccount",
            )


class PaymentServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _login_fn(**_kwargs):
        return {
            "accessToken": "test-token",
            "__cookies": [{"name": "session", "value": "test-cookie"}],
            "account": {"id": "acct-test", "planType": "plus"},
        }

    @staticmethod
    async def _entitlement_fn(**_kwargs):
        return {"plan": "plus", "is_plus": True, "expires": "2026-09-01", "payment_date": "2026-08-01"}

    @staticmethod
    async def _usage_fn(**_kwargs):
        return {"used_percent": 12.0, "remaining_percent": 88.0}

    async def test_check_returns_sanitized_payment_methods_with_account_context(self) -> None:
        calls: list[dict] = []

        async def payment_fn(**kwargs):
            calls.append(kwargs)
            return SAMPLE_PAYMENT_RESPONSE["data"]

        service = TwoFAService(
            login_fn=self._login_fn,
            entitlement_fn=self._entitlement_fn,
            usage_fn=self._usage_fn,
            payment_methods_fn=payment_fn,
            login_attempts=1,
        )
        result = await service.check(
            email="demo@example.com",
            password="test-password",
            secret="TESTSECRET",
            timeout=30,
            log=lambda _message: None,
        )

        self.assertEqual(result.payment_methods[0]["last4"], "4242")
        self.assertEqual(result.billing_date, "2026-08-01")
        self.assertEqual(calls[0]["account_id"], "acct-test")
        self.assertEqual(set(result.payment_methods[0]), {
            "type", "brand", "last4", "exp_month", "exp_year", "is_default",
        })

    async def test_payment_failure_is_non_fatal_and_does_not_leak_error(self) -> None:
        logs: list[str] = []

        async def payment_fn(**_kwargs):
            raise RuntimeError("upstream leaked test-bearer-token and private response")

        service = TwoFAService(
            login_fn=self._login_fn,
            entitlement_fn=self._entitlement_fn,
            usage_fn=self._usage_fn,
            payment_methods_fn=payment_fn,
            login_attempts=1,
        )
        result = await service.check(
            email="demo@example.com",
            password="test-password",
            secret="TESTSECRET",
            timeout=30,
            log=logs.append,
        )

        self.assertTrue(result.login_verified)
        self.assertIsNone(result.payment_methods)
        self.assertTrue(any("thanh toán" in line.casefold() for line in logs))
        self.assertFalse(any("test-bearer-token" in line for line in logs))
        self.assertFalse(any("private response" in line for line in logs))


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


class PaymentPersistenceTests(unittest.TestCase):
    def test_safe_summary_is_in_snapshot_state_and_recovery(self) -> None:
        payment_methods = _parse_payment_methods(SAMPLE_PAYMENT_RESPONSE)
        manager = TwoFAJobManager(_JobRepo(), _SettingsRepo())
        job = TwoFAJob(
            id="job-1",
            email="demo@example.com",
            password="test-password",
            secret="TESTSECRET",
            payment_methods=payment_methods,
            billing_date="2026-09-01",
        )

        snapshot = job.snapshot()
        self.assertEqual(snapshot["payment_methods"][0]["brand"], "visa")
        self.assertEqual(snapshot["billing_date"], "2026-09-01")
        self.assertNotIn("billing_details", json.dumps(snapshot))

        recovered = TwoFAJobManager(
            _JobRepo(
                [{
                    "id": "job-1",
                    "email": "demo@example.com",
                    "password": "test-password",
                    "secret": "TESTSECRET",
                    "status": "success",
                    "job_type": "twofa_community",
                    "account_check": manager._state(job),
                    "created_at": 1,
                }]
            ),
            _SettingsRepo(),
        )
        self.assertEqual(recovered.jobs["job-1"].payment_methods[0]["last4"], "4242")
        self.assertEqual(recovered.jobs["job-1"].billing_date, "2026-09-01")


if __name__ == "__main__":
    unittest.main()
