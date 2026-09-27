# Project Agent Guide

## Mandatory entry point

This repository is a localhost FastAPI application with a vanilla JavaScript dashboard. It checks ChatGPT accounts, reads plan, weekly Usage, and privacy-safe saved payment-method summaries, rotates TOTP 2FA, runs an explicitly confirmed per-account chat-deletion action, and runs a separate common-target password-change workflow through pure HTTP requests.

Before changing any behavior, read docs/TOOL-FLOW.md. It is the source of truth for the complete active app: launch, API routes, dashboard state, queue and workers, account operations, SQLite recovery, SSE, output/history, shutdown, packaging, privacy boundaries, and known safety gaps.

The browser opened at startup is only the localhost dashboard. Automated Community account operations must not depend on Playwright, Camoufox, Selenium, or a visible remote browser session. The explicit passkey handoff is the sole exception: the operator completes WebAuthn on OpenAI's page in their own browser.

## Active runtime boundary

The Community entry point is change 2fa community/server.py.

Treat signup, Outlook, iCloud Hide My Email, payment-link/purchase, browser-session, and other product-variant functions or tables as shared/legacy code unless the active call graph from server.py reaches them. The read-only saved payment-method inspection reached through `TwoFAService._inspect_account()` is active Community behavior.

One deliberate shared side effect exists: SettingsRepository writes active Community settings to settings and appends a redacted audit record to icloud_audit_log. This does not make the iCloud workflow active.

## Read code in this order

1. docs/TOOL-FLOW.md — complete runtime model and change-impact map.
2. change 2fa community/server.py — initialization, localhost auth, routes, SSE, startup, shutdown.
3. change 2fa community/jobs.py — snapshots, queue, workers, state, checkpoint, retry, output, subscriptions.
4. change 2fa community/service.py — check, rotate, verify, plan, Usage, payment-method summary, error classification.
5. For password work: change 2fa community/password_jobs.py, password_service.py, then root password_phase.py.
6. change 2fa community/static/app.js and password-ui.js — independent dashboard states and commands.
7. change 2fa community/static/realtime-ui.js, usage-ui.js, and payment-ui.js — event batching and safe Usage/payment rendering contracts.
8. session_phase.py — get_session_pure_request(), entitlement, weekly Usage, and saved payment-method fetch/parsing.
9. request_phase.py, sentinel_quickjs.py, sentinel_pow.py, user_agent_profile.py — OAuth/Sentinel HTTP protocol.
10. mfa_phase.py and totp_helper.py — factor disable, enrollment, activation, and TOTP.
11. Active methods in db/repositories.py, then db/schema.py and db/engine.py.
12. start-unix.sh, start-windows.bat, macos_integration.py, MenuBarApp.swift, build-macos.sh, and ShoptaikhoanTool.spec only when launch or packaging is in scope.

## Contracts to preserve

- check_only, Recheck, and Refresh Usage never call the rotation path.
- Delete all chats is available only for a successful Live row, requires explicit API confirmation, stays RAM-only, and never changes or persists job state, output, history, Usage, payment, 2FA, or password data.
- Logout all sessions is available only for a successful Live row, requires explicit API confirmation, sends one official logout-all request after reauthentication, never retries or logs in again, stays RAM-only, and blocks same-account conflicting work while pending.
- Add passkey is available only for a successful Live row with verified login, requires explicit API confirmation, prepares one official OpenAI enrollment handoff, keeps the URL RAM-only and one-use, never creates/stores a private key, and does not claim enrollment success.
- Successful rotation requires activation, durable secret checkpoint, and a fresh login with the stored new secret.
- rotated_pending_verify always resumes with verify(); it must never rotate again.
- Verified job completion and twofa_history insertion stay atomic.
- Queue deletion never deletes verified 2FA history.
- Password change remains a distinct job_type, API namespace, queue, SSE stream, output, and history.
- A password success requires fresh login with pending_password; completion promotes it atomically and inserts password_history.
- Once mutation_started is persisted, recovery never resends the non-idempotent password mutation automatically.
- password_change.target_password is read once at enqueue. It is returned only by the token-protected password workspace bootstrap or explicit Settings `reveal=true` view, both with no-store/nosniff; default status, snapshots, SSE, logs, and errors still omit it.
- Public snapshots, SSE, toasts, normal logs, and console output contain no passwords, secrets, bearer tokens, or cookies.
- Payment inspection is read-only and fail-soft. Only the entitlement billing date plus method type, brand, last four digits, expiry month/year, and default status may reach persistence, snapshots, SSE, or the dashboard; upstream IDs, billing details, fingerprints, customer IDs, tokens, cookies, and raw responses stay out.
- twofa.read_usage and twofa.read_payment_methods default on and are snapshotted per job. When disabled, their upstream endpoints are not called; Retry preserves the snapshot, while Recheck and per-row Change 2FA take current Settings.
- Credential-bearing responses stay behind the local token and should use Cache-Control: no-store.
- Job mutations remain on the manager application event loop.
- Worker recovery and reconciliation remain idempotent and self-healing.
- Server binding remains limited to 127.0.0.1, localhost, or ::1.
- Source and frozen builds retain the pure-request and Sentinel dependency contract.

## Known behavior, not a guarantee

- twofa.change_enabled is a frontend mode selection, not backend authorization.
- Rotation is disable-then-enable and is not atomic.
- MfaError.partial_state is not currently checkpointed by TwoFAService after an activation-path failure.
- Passwords, TOTP secrets, and the input draft are persisted as SQLite text.
- The common target and password history are also persisted as SQLite text; Settings audit stores `***`.
- /api/output is token-protected but currently lacks an explicit no-store header.
- An SSE subscriber can drop its oldest queued event at capacity 100; reconnect supplies a fresh snapshot, but there is no live revision/gap detector.
- Graceful shutdown persists active account work as cancelled; unclean persisted running/queued work is recovered as queued.
- The UI offers Retry only for error/cancelled rows, but backend retry() currently accepts any terminal row; directly retrying a successful change_2fa job can queue another rotation.

## Verification

Use the repository virtual environment:

~~~bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
node tests/test_usage_ui.js
node tests/test_payment_ui.js
node tests/test_inspection_options_ui.js
node tests/test_realtime_ui.js
node tests/test_password_ui.js
.venv/bin/python 'change 2fa community/server.py' --check-runtime-dependencies
.venv/bin/python -m compileall -q \
  'change 2fa community' session_phase.py request_phase.py mfa_phase.py db
node --check 'change 2fa community/static/app.js'
node --check 'change 2fa community/static/password-ui.js'
node --check 'change 2fa community/static/realtime-ui.js'
node --check 'change 2fa community/static/usage-ui.js'
node --check 'change 2fa community/static/payment-ui.js'
git diff --check
~~~

Run the closest tests first for scoped work, then the complete verification above. For docs changes, also compare the route catalog to server.py, confirm named symbols and files, and parse every Mermaid block with Mermaid v11.

## Project memory

Read MEMORY.md when present. After meaningful work, append a concise note with the date, summary, files changed, decisions, and validation. Never store real account data, tokens, cookies, passwords, or TOTP secrets in project memory.
