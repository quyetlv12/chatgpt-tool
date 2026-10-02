# Shoptaikhoan Tool — Complete Application Flow

Last code trace: 2026-09-27

Scope: Community Source v1.0.0, the FastAPI localhost application, its vanilla JavaScript dashboard, SQLite state, pure-request account operations, launchers, and macOS packaging.

This is the runtime source of truth for an Agent. Read it before changing behavior.

## 1. What the application is

The tool accepts one account per line:

~~~text
email|password|CURRENT_TOTP_SECRET
~~~

It provides three account operations; password change is deliberately isolated
from the 2FA queue and UI:

- check_only: log in, classify the account, always read plan, and optionally read weekly Usage and saved payment methods without changing 2FA.
- change_2fa: log in with the current secret, replace the TOTP factor, save the new secret, and prove the new secret works with a fresh login.
- password_change: snapshot the common target password from Settings, re-authenticate the current password and TOTP, submit one mutation, and prove the target password works with a fresh login.

A separate, explicitly confirmed per-row action can delete all chats for an
already verified Live account. It never runs from check, 2FA, password, retry,
or recovery flows and is not persisted for replay.

A separate, explicitly confirmed per-row action can log out all active sessions
for an already verified Live account. It reauthenticates, sends one
`POST /backend-api/accounts/logout_all`, never retries or signs in again, and
is not persisted for replay. Other sessions can take up to 30 minutes to
disappear; third-party and Codex CLI sessions are outside this surface. See
[OpenAI's Active sessions guidance](https://help.openai.com/en/articles/20001257-managing-active-sessions-in-chatgpt).

A separate, explicitly confirmed per-row action can prepare a passkey handoff
for an already verified Live account. It reauthenticates, obtains the official
OpenAI enrollment redirect, and opens it through a one-use localhost link. The
operator completes WebAuthn with Touch ID, Face ID, a device PIN, or a security
key; this tool never generates or stores a private key and never claims that
enrollment succeeded. See [OpenAI's passkey guidance](https://help.openai.com/en/articles/20001039-passkeys-to-secure-your-openai-account).

A dedicated `/passkey` page prepares the same official handoff for a batch of
`email|password|2FA` records with a bounded concurrent queue. Each ready record
opens a separate native Chrome window on macOS through a one-use localhost
redirect. Its isolated temporary profile explicitly defaults compatible
passkey creation to iCloud Keychain, so Chrome goes directly to the native
Passwords/Touch ID prompt instead of asking the operator to choose a provider.
The bulk queue is RAM-only, is not recovered after restart, and never reports
WebAuthn enrollment as complete.

The application has two very different browser meanings:

- Local browser: server.py normally opens http://127.0.0.1:5033 so the operator can use the dashboard.
- Remote account flow: no Playwright, Camoufox, Selenium, rendered page, or visible ChatGPT browser session is used. TwoFAService uses get_session_pure_request() and rotate_2fa(); PasswordService uses the same pure-request login and password_phase.change_password_pure_request().

Passing --no-browser suppresses only the automatic local-dashboard launch. It does not change how account login or 2FA rotation works.

The Suite dashboard exposes a single `Đóng tab Chrome` control in its top
navigation. It closes only Chrome processes owned by the Suite: passkey
windows from TwoFA, ChatGPT Web/Codex Web windows from Browser Login, and the
legacy OAuth login worker. It does not scan or close the operator's normal
Chrome windows and runs without a confirmation dialog.

## 2. Active runtime versus shared or legacy code

The Community executable starts at change 2fa community/server.py. Its active call graph is:

~~~mermaid
flowchart TD
  operator["Operator"] --> localUI["Local dashboard<br/>app.js + password-ui.js + passkey-ui.js"]
  localUI -->|"HTTP + local token"| api["FastAPI<br/>server.py"]
  api --> manager["TwoFAJobManager<br/>jobs.py"]
  api --> passwordManager["PasswordJobManager<br/>password_jobs.py"]
  api --> passkeyManager["PasskeyJobManager<br/>passkey_jobs.py"]
  manager --> service["TwoFAService<br/>service.py"]
  passwordManager --> passwordService["PasswordService<br/>password_service.py"]
  passkeyManager --> service
  service --> login["Pure-request login<br/>session_phase.py"]
  login --> oauth["OAuth + password + TOTP<br/>request_phase.py"]
  oauth --> sentinel["Sentinel support<br/>QuickJS or Python PoW"]
  service --> inspect["Entitlement + optional Usage/payment"]
  service --> mfa["TOTP replacement<br/>mfa_phase.py"]
  passwordService --> passwordHttp["HTTP re-auth + reset<br/>password_phase.py"]
  manager --> db[("SQLite")]
  passwordManager --> db
  passkeyManager --> ram[("RAM only")]
  manager --> sse["SSE snapshots and events"]
  sse --> localUI
~~~

Active Community modules:

| Layer | Files | Active responsibility |
|---|---|---|
| Launch and API | change 2fa community/server.py | Runtime directory, auth token, FastAPI routes, static files, SSE, browser launch, shutdown |
| Job lifecycle | change 2fa community/jobs.py | Parsing, queue, workers, state, retries, recovery, logs, output, subscriptions |
| Account orchestration | change 2fa community/service.py | Check, rotate, verify, plan/Usage/payment-method lookup, confirmed chat deletion, safe error classification |
| Password lifecycle | change 2fa community/password_jobs.py, password_service.py | Independent queue, mutation checkpoint, ambiguity recovery, verified output/history |
| Passkey lifecycle | change 2fa community/passkey_jobs.py, passkey_service.py | Independent RAM-only concurrent handoff queue; no persistence or WebAuthn result |
| Passkey windows | change 2fa community/passkey_windows.py | macOS Chrome window creation and tiling; isolated profile preference defaults compatible WebAuthn creation to iCloud Keychain; RAM-only native IDs, no remote page automation |
| Password HTTP adapter | password_phase.py | Current-password/TOTP re-auth and one password-reset mutation without a browser |
| Pure HTTP login | session_phase.py | get_session_pure_request(), auth callback, session export, entitlement, Usage, and saved payment-method helpers |
| OAuth and browser-like protocol | request_phase.py, user_agent_profile.py | CSRF/OAuth steps, headers, cookies, device identity, HTTP persona |
| Sentinel | sentinel_quickjs.py, sentinel_pow.py, openai_sentinel_quickjs.js | Challenge token generation; embedded Node/QuickJS first, Python PoW fallback |
| MFA | mfa_phase.py, totp_helper.py | Factor discovery, disable, enroll, activate, TOTP generation |
| Persistence | db/engine.py, db/schema.py, db/repositories.py | SQLite migration, settings, jobs, logs, verified history |
| Dashboard | static/index.html, static/passkey.html, app.js, password-ui.js, passkey-ui.js, password-history-ui.js, usage-ui.js, payment-ui.js, realtime-ui.js, dashboard.css | Main 2FA/password workspace plus a dedicated full-page passkey route, safe rendering, downloads, dialogs |
| macOS integration | macos_integration.py, packaging/macos/MenuBarApp.swift | Optional frozen-build menu companion |

Shared files contain substantial dormant code for signup, Outlook, iCloud Hide My Email, browser sessions, payment links, and other product variants. Schema tables and repository classes for those features are not proof that the Community app runs those flows.

Important exception: SettingsRepository is shared infrastructure. Active Community setting writes also append a redacted settings audit row to icloud_audit_log. That table write does not activate any iCloud workflow.

## 3. Complete application lifecycle

~~~mermaid
sequenceDiagram
  autonumber
  actor User as Operator
  participant Launch as Launcher or app bundle
  participant API as server.py
  participant DB as SQLite
  participant JM as TwoFAJobManager
  participant UI as Dashboard
  participant Remote as Remote account APIs

  User->>Launch: Start tool
  Launch->>API: Python server.py on loopback
  API->>API: Prepare UTF-8, CA bundle, runtime paths
  API->>DB: Copy legacy DB once if needed
  API->>DB: Open, configure, migrate
  API->>DB: Load or create web.auth_token
  API->>JM: Construct manager and recover persisted jobs
  API->>JM: lifespan start and create workers
  API->>UI: Open localhost dashboard unless --no-browser
  UI->>API: GET /api/bootstrap
  API-->>UI: token, settings, safe jobs, worker health
  UI->>API: GET /api/events?token=...
  API-->>UI: SSE snapshot, job events, health, keepalives
  User->>UI: Submit or manage accounts
  UI->>API: Protected command
  API->>JM: Validate transition and queue work
  JM->>Remote: Pure HTTP login, inspect, optional rotate
  JM->>DB: Persist each durable transition
  JM-->>UI: Realtime privacy-safe state
  User->>Launch: Ctrl+C, SIGTERM, or menu Quit
  API->>JM: Cancel active work and stop workers
  API->>DB: Close writer and read connections
  API->>Launch: Terminate menu companion
~~~

## 4. Source-mode startup

start-unix.sh and start-windows.bat perform the same high-level flow:

1. Resolve the repository root.
2. Find Python; the project requires 3.11 or newer, although the launcher does not explicitly enforce the version.
3. Create .venv when it does not exist.
4. Upgrade pip and install requirements-source.txt.
5. Enable UTF-8 I/O.
6. Ensure the platform application-data directory exists.
7. run server.py --host 127.0.0.1 --port 5033.

The server accepts only 127.0.0.1, localhost, or ::1. Any other --host fails argument validation. The port must be in 1..65535.

Default runtime directories:

| Platform | Directory |
|---|---|
| Windows | %LOCALAPPDATA%\InfinityAIStore\Change2FA |
| macOS | ~/Library/Application Support/InfinityAIStore/Change2FA |
| Linux | $XDG_DATA_HOME/InfinityAIStore/Change2FA, or ~/.local/share/InfinityAIStore/Change2FA |

Key runtime files are twofa.db and cacert.pem.

At module initialization, _prepare_runtime():

- forces UTF-8 where possible;
- creates the runtime directory;
- copies the certifi CA bundle to cacert.pem when missing or changed;
- points CURL_CA_BUNDLE, SSL_CERT_FILE, and REQUESTS_CA_BUNDLE at that copy.

_migrate_legacy_database() copies change 2fa community/runtime/twofa.db into the native per-user directory only when the destination does not already exist. It uses SQLite backup rather than a byte copy. A release must not bundle a user database.

## 5. Server construction and lifespan

server.py performs these steps before Uvicorn starts:

1. Create or migrate SQLite through db.get_engine().
2. Resolve JobRepository and SettingsRepository.
3. Read web.auth_token.
4. Generate a token with secrets.token_urlsafe(32) when missing or too short, then persist it.
5. Construct TwoFAJobManager, the independent PasswordJobManager, and the RAM-only PasskeyJobManager.
6. Recover each manager's own job_type; the 2FA manager also backfills verified pre-v14 rotations into history.
7. Build the FastAPI app and static mount.

FastAPI lifespan:

- startup starts all three managers;
- shutdown awaits the RAM-only passkey manager and both durable managers, then calls engine.close().

main() optionally starts the macOS menu helper, starts a daemon health-poll thread that opens the local browser, then runs Uvicorn with a three-second graceful-shutdown bound.

The dependency-only command:

~~~bash
.venv/bin/python 'change 2fa community/server.py' --check-runtime-dependencies
~~~

checks the pure-request function contract, Sentinel runtime asset, and—inside a frozen macOS build—the native menu helper. It does not start the API or perform an account operation.

## 6. SQLite model, migrations, and recovery

DatabaseEngine uses:

- SQLite WAL mode;
- foreign keys;
- a 5-second busy timeout;
- one shared writer connection protected by a reentrant lock;
- BEGIN IMMEDIATE by default for writes;
- thread-local read connections for concurrent reads;
- automatic schema migration to CURRENT_VERSION 15.

A fresh database runs the latest ALL_DDL. An existing database runs incremental MIGRATIONS in order. Migration temporarily disables foreign-key checks, executes within one immediate transaction, writes _schema_version, then restores foreign keys. Failure rolls back and raises SchemaError.

Tables used directly by this Community runtime:

| Table | Use |
|---|---|
| jobs | Durable job, current password, optional pending_password checkpoint, secret, status, job_type, and JSON state |
| job_logs | Timestamped per-job operator log lines; deleted with its job |
| settings | Local auth token, worker settings, mode switch, and input draft |
| twofa_history | One independent, append-only row per verified rotation |
| password_history | One independent row per password change verified by fresh login |
| icloud_audit_log | Shared SettingsRepository audit side effect only; sensitive setting values are redacted |

The other schema tables belong to shared or legacy variants.

The manager persists these orthogonal values as JSON in jobs.account_check:

- mode;
- rotated_pending_verify;
- login_verified;
- retry_count;
- error_kind;
- account_state;
- plan and plan_source;
- privacy-safe Usage summary;
- privacy-safe saved payment-method summaries;
- per-job usage_enabled and payment_methods_enabled snapshots.

Legacy jobs that predate the two inspection flags recover with both enabled,
which preserves their original behavior.

Recovery behavior:

1. Manager construction loads only rows whose job_type is change_2fa.
2. Persisted queued or running states are normalized to queued in memory.
3. Logs are reloaded from job_logs.
4. Legacy errors are classified conservatively.
5. manager.start() creates the configured worker count and enqueues all recovered queued jobs.
6. Verified successful change_2fa rows missing from twofa_history are inserted idempotently.

Password recovery is separate. PasswordJobManager loads only
job_type=password_community. A recovered job with mutation_started=false may
perform its first mutation. With mutation_started=true, PasswordService never
submits again: it tries a fresh login with pending_password first, then the old
password, and classifies the state as verified, not-applied, or uncertain.

An unclean interruption therefore resumes queued or running work. A graceful shutdown cancels active _run tasks and persists them as cancelled; those require retry after restart.

SettingsRepository validates key name, whitelist, type, and range. Writes are JSON encoded, audited in the same transaction, and retried on SQLite busy-lock with short backoff.
password_change.target_password is limited to 12..128 characters, rejects `|`
and control characters, and is always audited as `***`.

## 7. Dashboard bootstrap and client state

app.js owns one in-memory state object:

- token;
- jobs Map containing privacy-safe snapshots;
- selected filter;
- settings and worker health;
- EventSource connection and 100 ms event batcher;
- verified output text;
- draft-save timer;
- temporary launch, row-change, recheck, result, and history state.
- temporary delete-all-chats confirmation state.
- temporary multi-account logout selection and confirmation state; only successful Live rows can be selected, and the batch uses bounded workers while reusing the single-account logout route.

init() runs:

1. GET /api/bootstrap.
2. Store the returned local token, settings, worker health, and snapshots.
3. Restore twofa.input_draft only when at least one 2FA job is still queued or running; otherwise start with an empty editor so an old completed draft cannot look like a newly entered account.
4. Load settings controls and render editor, connection, queue, counters, and empty output.
5. If the document is visible, connect EventSource to /api/events?token=....
6. Fetch /api/output.

password-ui.js owns a second state object, EventSource, queue, output, and
history. Its normal status refresh receives only the password setting's
configured flag. Opening Settings explicitly calls
`/api/password-settings?reveal=true`; opening “Đổi mật khẩu” calls
`/api/password/bootstrap`. Those token-protected, no-store responses return the
stored target so the operator can view and edit it in either screen. Both
inputs are cleared from the DOM when their owning drawer/dialog closes.
Its `/api/password/events` stream is lazy: it opens only while the password
workspace is visible and closes with that dialog.

If bootstrap fails, the UI marks the connection offline and shows a localhost error. EventSource later reports online or reconnecting; worker-health degradation can change the visible connection label to degraded.

GET /api/bootstrap is intentionally unprotected and returns the token because the service is loopback-only. The token is a local control-plane guard, not multi-user authentication and not a defense against a malicious process already running as the same user.

## 8. Complete route catalog

Public or bootstrap routes:

| Method and path | Consumer | Response or behavior |
|---|---|---|
| GET / | Browser | static/index.html |
| GET /passkey | Browser | Dedicated full-width static/passkey.html page; not a modal |
| GET /assets/* | Browser | Static CSS, JavaScript, icons |
| GET /api/bootstrap | app.js | Brand, token, safe snapshots, settings, worker health |
| GET /api/health | browser-open poll, health checks | ok, runtime port, 2FA and password worker health |
| GET /api/events?token=... | EventSource | Token checked in query; SSE initial snapshot, events, keepalive |
| GET /api/password/events?token=... | password-ui.js | Independent password-job SSE stream |
| GET /api/passkey/events?token=... | passkey-ui.js | RAM-only passkey-job SSE stream |

Routes protected by X-Auth-Token:

| Method and path | Manager operation | Success result |
|---|---|---|
| POST /api/jobs | add(lines, mode) | Created safe snapshots |
| GET /api/jobs/export?view=... | export_filtered(view) | Credential-bearing text download |
| GET /api/jobs/{id}/raw | raw_combo(id) | One credential-bearing combo |
| GET /api/twofa-history | twofa_history() | Credential-bearing verified history |
| POST /api/jobs/{id}/change-2fa | enqueue_change_2fa(id) | Eligible row queued for mutation |
| POST /api/jobs/{id}/retry | retry(id) | Retryable terminal row queued |
| POST /api/jobs/{id}/recheck | recheck(id) | Successful row queued as check_only |
| POST /api/jobs/{id}/refresh-usage | refresh_usage(id) | Updated safe snapshot |
| POST /api/jobs/{id}/delete-chats | delete_all_chats(id) | Reauthenticated bulk chat deletion; requires body confirmation `DELETE_ALL_CHATS` |
| POST /api/jobs/{id}/logout-sessions | logout_all_sessions(id) | Reauthenticated one-shot session revocation; requires body confirmation `LOGOUT_ALL_SESSIONS` |
| POST /api/jobs/{id}/passkey/start | prepare_passkey(id) | Reauthenticated official passkey handoff; requires body confirmation `ADD_PASSKEY` |
| GET /api/passkey/bootstrap | PasskeyJobManager | Safe RAM-only bulk snapshots and worker health |
| POST /api/passkey/jobs | PasskeyJobManager.add | Queue `email|password|2FA` records for concurrent handoff preparation |
| POST /api/passkey/jobs/{id}/launch | PasskeyJobManager.issue_handoff | Issue one-use redirect; requires body confirmation `LAUNCH_PASSKEY`; optional `native_window=true` opens/tile Chrome on macOS and returns `opened` plus fallback `launch_path` |
| POST /api/passkey/windows/close | PasskeyWindows.close_all | Gracefully close only Chrome processes opened by this tool; force-kill a stubborn owned process after a bounded timeout |
| POST /api/passkey/jobs/{id}/retry | retry(id) | Retry a definitive bulk preparation error |
| POST /api/passkey/jobs/{id}/stop | stop(id) | Stop one RAM-only bulk job |
| DELETE /api/passkey/jobs/{id} | delete(id) | Delete one terminal RAM-only job |
| DELETE /api/passkey/jobs | clear_async() | Clear only terminal bulk passkey jobs |
| GET /api/passkey/jobs/{id}/logs | logs(id) | RAM-only fixed milestone log lines |
| GET /api/passkey/output | output() | Non-sensitive list of handoffs opened; no credentials or enrollment claim |
| GET /api/passkey/launch/{launch_token} | PasskeyHandoffs.consume | One-use, expiring redirect to exact `https://auth.openai.com/passkey-enroll` |
| POST /api/jobs/{id}/stop | stop(id) | Cancelled or current snapshot |
| DELETE /api/jobs/failed | clear_failed() | Delete count and replacement snapshot |
| DELETE /api/jobs/{id} | delete(id) | ok |
| POST /api/jobs/stop-all | stop_all() | ok |
| DELETE /api/jobs | clear_async() | Delete count; SQLite cleanup runs off the FastAPI event loop |
| GET /api/jobs/{id}/logs | logs(id) | Safe log lines |
| GET /api/output | output() | Verified rotation output text |
| PUT /api/settings | update_settings(values) | Persisted settings |
| GET /api/password/bootstrap | PasswordJobManager | configured flag, target for the open password workspace, safe jobs, worker health; token + no-store |
| GET /api/password-settings | SettingsRepository | configured flag by default; `reveal=true` adds the target for the open Settings drawer; token + no-store |
| PUT /api/password-settings | SettingsRepository | Store the SecretStr target password |
| DELETE /api/password-settings | SettingsRepository | Explicitly clear the target password |
| POST /api/password/jobs | PasswordJobManager.add | Created safe password-job snapshots |
| POST /api/password/jobs/{id}/retry | retry(id) | Human-authorized safe retry |
| POST /api/password/jobs/{id}/stop | stop(id) | Stop, or uncertain after mutation checkpoint |
| DELETE /api/password/jobs/{id} | delete(id) | Delete one terminal password job |
| DELETE /api/password/jobs | clear_async() | Clear only the password queue; SQLite cleanup runs off the FastAPI event loop |
| GET /api/password/jobs/{id}/logs | logs(id) | Redacted password-job logs |
| GET /api/password/output | output() | Fresh-login-verified output text |
| GET /api/password/history | history() | Durable verified password history |

Expected API error mappings:

- validation errors are generally 422;
- missing job IDs are 404;
- invalid job state transitions are 409;
- missing or wrong token is 401;
- an upstream Usage refresh or chat-deletion failure is 502.

Static DELETE /api/jobs/failed must remain declared before dynamic DELETE /api/jobs/{job_id}.

## 9. Editor, mode, draft, and settings flow

Editor behavior:

- input and line numbers share scroll position;
- line count and current-line UI update as text changes;
- multi-line paste returns the editor to the first line;
- the manual jump action moves scroll and caret to the top;
- non-empty trimmed lines become a launch batch.

Draft behavior:

1. Every input event schedules a 450 ms debounce.
2. The latest editor value is sent through PUT /api/settings as input_draft.
3. The full settings payload is sent because SettingsRequest is not a partial-update model.
4. On restart, bootstrap restores the persisted draft only for a queue with queued/running work; after all work is terminal, the editor starts empty. Typing still saves the draft normally, so a restart during active work preserves it.

The draft contains raw credentials. It is stored as JSON text in settings, while the SettingsRepository audit records only a redacted value for twofa.input_draft.

Mode behavior:

~~~text
twofa.change_enabled false -> submitted mode check_only
twofa.change_enabled true  -> submitted mode change_2fa
~~~

Changing the switch persists immediately. It is a UI selection only: BatchRequest and manager.add() accept change_2fa regardless of the stored switch value.

The compact `Đọc Usage` and `Đọc Payment` switches also persist immediately.
They control read-only inspection calls, not authentication or authorization.
Both default to on. A new job snapshots their values, so later Settings edits
cannot change network behavior for work already queued. Recheck and per-row
Change 2FA deliberately take a fresh snapshot from the current Settings; Retry
preserves the failed job's existing snapshot.

Runtime settings and limits:

| Key | Default | Accepted by API |
|---|---:|---:|
| twofa.max_concurrent | 3 | integer 1..10 |
| twofa.job_timeout | 180 | 30..600 seconds |
| twofa.auto_retry | false | boolean |
| twofa.auto_retry_max | 1 | integer 0..5 |
| twofa.auto_retry_delay | 3 | 0..60 seconds |
| twofa.change_enabled | false | boolean |
| twofa.read_usage | true | boolean |
| twofa.read_payment_methods | true | boolean |
| twofa.input_draft | empty | up to 1,000,000 characters |
| password_change.target_password | absent | string 12..128; no pipe/control characters |

The password target uses dedicated GET/PUT/DELETE routes. Blank UI input means
unchanged; deletion is explicit. GET returns only `configured`, never the value.

The quick concurrency selector saves before the confirmation dialog opens. Changing concurrency calls manager.update_settings(), which grows the pool immediately or sends retirement sentinels so excess workers exit safely between jobs.

## 10. Batch creation and queue dispatch

The operator launch flow:

1. Read and trim non-empty editor lines.
2. Store lines and mode in pendingLaunch.
3. Show a mode-specific confirmation dialog.
4. On confirmation, POST /api/jobs with a 15-second client-side response
   deadline. The same deadline applies when the launch button first needs to
   persist a changed quick-concurrency value.
5. Insert returned snapshots into the client Map.
6. Scroll to the queue.
7. Treat subsequent SSE state as authoritative.

The deadline only releases the dashboard from a stalled localhost request; it
does not cancel or roll back work that the server may already have accepted.
On timeout the launch button is always re-enabled and the operator is told to
inspect the realtime queue before submitting again, which avoids accidental
duplicate batches after a response was lost.

manager.add():

1. Require mode check_only or change_2fa.
2. Parse each line as exactly three non-empty pipe-separated fields.
3. Require an at-sign in the email.
4. Case-fold email; remove spaces from and uppercase the TOTP secret.
5. Deduplicate emails within this submitted batch.
6. Create a UUID job with status queued.
7. Snapshot the current Usage/payment inspection switches onto the job.
8. Persist password and secret; store [redacted] in the legacy combo column.
9. Add the job to manager order, reconcile workers, enqueue its ID, and broadcast a safe snapshot.

The dedupe scope is one request only. It does not prevent the same email from existing in older jobs or a later batch.

## 11. Worker pool and job execution

The manager owns an asyncio queue and configured workers. Each worker:

1. waits for a job ID;
2. ignores missing or no-longer-queued jobs;
3. creates one _run task;
4. tracks that task by job ID so Stop can cancel it;
5. awaits completion and marks the queue item done.

_run first persists running state, then selects exactly one service branch:

~~~text
mode == check_only                              -> service.check()
mode == change_2fa and rotated_pending_verify  -> service.verify()
otherwise                                       -> service.rotate()
~~~

Unexpected worker termination is observed by _worker_done(). The manager increments health counters, records the exception type, broadcasts health, and reconciles back to the configured pool size. add(), retry(), recheck(), and row Change 2FA also reconcile workers before enqueueing.

Worker health fields:

| Field | Meaning |
|---|---|
| started | Manager lifespan has started |
| configured | Desired worker count |
| active | Live worker tasks |
| busy | Tracked _run tasks |
| queued | Jobs currently marked queued |
| restarts | Unexpected worker exits observed |
| persistence_failures | Repository operations that failed through _persist |
| last_worker_error | Latest worker exception type |
| last_persistence_error | Latest persistence exception type |
| degraded | Started and active is below configured |

All manager mutation methods assert they run on the manager application event loop. Preserve this ownership model.

## 12. Job state machine

~~~mermaid
stateDiagram-v2
  [*] --> queued: create or recover
  queued --> running: worker claims
  running --> success: operation and required verification pass
  running --> error: technical or account failure
  running --> cancelled: Stop, shutdown, or cancellation
  error --> queued: eligible manual or automatic retry
  cancelled --> queued: eligible manual retry
  success --> queued: Recheck as check_only
  success --> queued: eligible row Change 2FA
  success --> [*]: delete or clear
  error --> [*]: delete or clear
  cancelled --> [*]: delete or clear
~~~

Primary statuses are queued, running, success, error, and cancelled. Never infer the complete business state from status alone.

| Orthogonal field | Meaning |
|---|---|
| mode | check_only or change_2fa |
| rotated_pending_verify | New secret is durable; only fresh-login verification remains |
| login_verified | Current stored secret completed login |
| account_state | live, die, or unknown |
| error_kind | technical_error, account_die, invalid_credentials, or none |
| retry_count | Number of manager retries |
| usage_refreshing | Separate in-flight Usage operation; job status stays success |
| usage_enabled | Whether this job may call the weekly Usage endpoint |
| payment_methods_enabled | Whether this job may call the payment-method endpoint |

retryable is false for account_die and invalid_credentials. Technical failures remain retryable.

## 13. Pure-request login: why no remote browser is needed

TwoFAService._resolve_dependencies() binds login to get_session_pure_request(). Synchronous curl_cffi work runs through asyncio.to_thread() inside the pure-request implementation so it does not block FastAPI rendering or SSE.

The protocol:

1. Create a curl_cffi session impersonating a supported Chrome network fingerprint.
2. Warm chatgpt.com to establish Cloudflare and NextAuth prerequisites.
3. Fetch /api/auth/csrf.
4. POST /api/auth/signin/openai and obtain the OAuth authorize URL.
5. Maintain one device ID through cookies, query parameters, headers, and Sentinel requests.
6. Generate a Sentinel token using the downloaded JavaScript SDK through bundled Node/QuickJS; use Python PoW fallback when needed.
7. Resolve the account flow and submit password verification.
8. When MFA is required, request a challenge, generate the current six-digit TOTP, and verify it.
9. Follow redirects and consume the NextAuth callback.
10. Require a session-token cookie.
11. GET /api/auth/session and require a non-empty accessToken.
12. Export cookies as __cookies for entitlement, Usage, saved payment-method, and MFA calls.

The HTTP client supplies TLS fingerprinting, User-Agent and Client Hints, fetch metadata, Origin, Referer, trace headers, cookies, and Sentinel data. It reproduces the network protocol but never renders a remote page.

Passwordless email-OTP accounts need a mail provider. The Community service passes no mail provider, so that branch cannot complete.

_login() tries up to three times by default. Fatal credential errors stop early. Password/MFA verification HTTP 403, 429, redirects, and 5xx are treated as upstream/WAF/state failures rather than proof of bad credentials; the next attempt gets a fresh OAuth state through the legacy fallback flow. Final failure is classified as account_die, invalid_credentials, or technical_error; only conservative account failures become non-retryable.

## 14. Check-only flow

service.check():

1. Log in using email, password, and the stored current TOTP secret.
2. Always inspect entitlement; include weekly Usage and saved payment methods concurrently only when their job flags are enabled.
3. Prefer entitlement plan; fall back to the session plan.
4. Treat Usage or payment-method failure as non-fatal and return the unavailable value as null.
5. Return the same secret, login_verified true, and account_state live.

The manager persists success but does not insert a twofa_history row because no rotation occurred.

Recheck uses the same branch. It changes the existing row to mode check_only, resets classification, Usage, and payment-method fields, preserves the current secret and password, and queues it. Recheck can therefore inspect a previously rotated account without rotating again.

## 15. Change 2FA flow

~~~mermaid
sequenceDiagram
  autonumber
  participant JM as Job manager
  participant SVC as TwoFAService
  participant Login as Pure-request login
  participant MFA as mfa_phase
  participant DB as SQLite

  JM->>SVC: rotate(email, password, old_secret)
  SVC->>Login: Authenticate with old secret
  Login-->>SVC: accessToken + cookies
  par Account inspection
    SVC->>Login: Fetch entitlement
  and Weekly Usage
    SVC->>Login: Fetch Usage
  and Saved payment methods
    SVC->>Login: GET payments/payment_methods
  end
  SVC->>MFA: rotate_2fa(accessToken, cookies)
  MFA->>MFA: GET mfa_info
  MFA->>MFA: Disable active old TOTP factor
  MFA->>MFA: Enroll replacement TOTP factor
  MFA->>MFA: Generate code from replacement secret
  MFA->>MFA: Activate enrollment
  MFA-->>SVC: activated true + new secret
  SVC->>JM: checkpoint(new_secret)
  JM->>DB: Persist secret and rotated_pending_verify
  SVC->>Login: Fresh login with new secret
  Login-->>SVC: Verified accessToken
  SVC-->>JM: login_verified true
  JM->>DB: Atomic job success and history insert
~~~

Detailed MFA mutation:

1. Create curl_cffi.AsyncSession with the selected Chrome persona.
2. Inject the authenticated account cookies.
3. GET /backend-api/accounts/mfa_info.
4. Select an active TOTP/authenticator/in-house factor.
5. POST /backend-api/accounts/mfa/user/disable_in_house with its factor ID.
6. POST /backend-api/accounts/mfa/enroll with factor_type totp.
7. Normalize the returned Base32 secret.
8. Generate the current six-digit code with pyotp.
9. POST /backend-api/accounts/mfa/user/activate_enrollment.
10. Accept a meaningful already-active response as idempotent success.
11. Optionally read mfa_info again; activation success remains authoritative.
12. Require activated true and a non-empty new secret.

After rotate_2fa() returns, service.rotate() immediately invokes the manager checkpoint callback. The callback:

- replaces job.secret with the new value;
- sets rotated_pending_verify true;
- sets login_verified false;
- persists password, secret, and JSON state while status remains running;
- broadcasts only the safe snapshot.

Then a fresh pure-request login uses the new secret. Only after this succeeds does _run clear rotated_pending_verify, set login_verified true, and mark success. JobRepository.complete_twofa_success() updates the job and inserts twofa_history in one SQLite transaction.

If a retry or restart sees rotated_pending_verify true, _run calls service.verify() with the checkpointed secret. It must not call rotate() a second time.

## 16. Plan, weekly Usage, and saved payment-method flow

After login, _inspect_account() always reads plan and conditionally adds Usage
and saved payment-method calls to the same concurrent inspection group. A
disabled reader is never invoked; the service records only a safe “Đã tắt theo
cấu hình” milestone. The dashboard renders `Đã tắt` instead of unavailable or
error, and the intentional missing Usage is excluded from the
`free-no-usage` filter.

Plan:

- fetch_account_entitlement() is authoritative when available;
- session accountPlan or account.planType is fallback;
- unknown plan does not fail a live account;
- plan_source records entitlement, session, or none;
- a valid entitlement `expires_at` is reduced to `YYYY-MM-DD` in the account timezone; for monthly Plus/Pro plans, the payment date is the previous monthly anniversary (`expires_at` is the next renewal), while an explicit current-period start is preferred when the endpoint supplies one. Invalid or unavailable values remain null.

Usage:

- fetch_codex_weekly_usage() receives access token, account ID, cookies, and timeout;
- parser selects the actual weekly rate-limit window;
- persisted and emitted data is a privacy-safe summary only;
- the existing Usage response also contributes the validated available reset count; no reset-detail or consume endpoint is called;
- failure logs only the exception type, then returns null.

usage-ui.js:

- accepts percentages only from 0 through 100;
- derives used, remaining, reset duration, limit state, available reset label, credit label, and visual tone;
- marks warning at used percentage 80 or higher;
- marks limited when the upstream summary says the limit was reached or access was disallowed.

Saved payment methods:

- fetch_payment_methods() performs GET `/backend-api/payments/payment_methods` with the authenticated account ID as a query parameter and account context header;
- the same pure-request bearer token, cookies, curl persona, and browser-like headers are reused—no remote browser is opened;
- response envelopes may be a root list or use `data`, `methods`, or `payment_methods`;
- the parser accepts at most eight entries and keeps only type, brand, last four digits, expiry month/year, and default status;
- billing details, method/customer IDs, fingerprints, tokens, cookies, and raw response data are discarded before the service result;
- failure logs only the exception class and returns null, so account verification and 2FA behavior continue;
- an empty list means the account was read successfully but has no saved payment method, while null means the lookup was unavailable.

payment-ui.js defensively validates the safe summary and payment date again, displays up to two methods plus an additional count, formats the date as `Thanh toán DD/MM/YYYY`, and renders separate loading, empty, unavailable, and not-yet-read states. When no explicit current-period start is returned, the monthly date is derived from the next renewal date.

Filters are mirrored in frontend and manager export logic:

| Filter | Exact condition |
|---|---|
| all | Every current Community job |
| running | status queued or running |
| success | status success |
| usage-low | valid used percentage below 50; 50 is excluded |
| usage-full | valid used percentage exactly 100 |
| free-no-usage | success + live + plan free/plus + no valid Usage |
| error | status error or cancelled |

Refresh Usage is available only for success + live + Usage null when that job's
usage_enabled snapshot is true. It:

1. sets usage_refreshing true without changing job status;
2. logs in again using the current stored secret;
3. reads Usage only;
4. persists the safe summary into account_check;
5. always clears usage_refreshing and broadcasts;
6. never changes 2FA.

While usage_refreshing is true, retry, recheck, row Change 2FA, chat deletion,
and job deletion are blocked.

## 17. Per-row actions

The queue renders actions based on safe snapshot state:

| Action | Eligibility | Flow |
|---|---|---|
| Copy raw | Existing row | Protected no-store GET, then Clipboard API |
| 2FA | check_only + success + live | Confirmation, queue same row as change_2fa |
| Recheck | success | Queue same row as check_only |
| Refresh Usage | success + live + missing Usage + usage_enabled | Reauthenticate and read Usage only |
| Xóa hết chat | success + live | Destructive confirmation, reauthenticate, bulk-hide all conversations once |
| Logs | Existing row | Protected GET and detail drawer |
| Retry | UI: error/cancelled and retryable | Preserve current secret; queue again |
| Stop | queued or running | Cancel task or mark queued row cancelled |
| Delete | terminal and no auxiliary action running | Delete job and logs, broadcast removed |

Row Change 2FA UI:

1. Shows a confirmation with email, plan, and remaining Usage—not raw credentials.
2. Adds the job ID to pendingRowChangeResults.
3. Calls POST /change-2fa.
4. Watches SSE for that job to transition into successful change_2fa.
5. Refreshes verified output.
6. Opens the success dialog and fetches /raw with no-store.
7. Clears the raw value, email, active job ID, and related DOM state when the dialog closes.

Delete-all-chats UI and manager:

1. Shows the action only for a successful Live row.
2. Warns that project chats are included, deletion cannot be undone, and separately saved Library files are not deleted.
3. Requires the modal action and exact API confirmation body before execution.
4. Reauthenticates, then sends one `PATCH /backend-api/conversations` with `{"is_visible": false}` using the existing account session recipe.
5. Keeps `chat_deleting` in RAM only, broadcasts it through SSE, and blocks conflicting per-row actions until completion.
6. Never changes or persists job status, mode, secret, plan, Usage, payment data, output, or history; upstream response bodies, tokens, and cookies are never logged.

Logout-all-sessions UI and manager:

1. Shows the action only for a successful Live row.
2. Warns that every active session, including the current session, is affected; propagation can take up to 30 minutes and other tools using the account may be disconnected.
3. Requires the exact API confirmation body `LOGOUT_ALL_SESSIONS`.
4. Reauthenticates, sends one `POST /backend-api/accounts/logout_all` with redirects disabled, and never submits a second request.
5. Keeps `sessions_logging_out` in RAM/SSE only, blocks same-account check/2FA/password/chat/delete/clear actions, and clears the flag in `finally`.
6. Does not log in again after revocation; ambiguous outcomes are reported for manual inspection.

Passkey UI and manager:

1. Shows the action only for a successful Live row with `login_verified=true`.
2. Requires explicit confirmation, then reauthenticates with up to three bounded attempts and POSTs `/backend-api/accounts/mfa/user/request_mfa_token_in_house` with the fresh session bearer token, the preserved ChatGPT/OpenAI cookie set, and browser-equivalent request headers. The complete preparation has one deadline, capped at 180 seconds; a transient login failure is retried before the handoff request.
3. Requires a valid `state_token` response and constructs only `https://auth.openai.com/passkey-enroll?origin_app_name=ChatGPT&mfa_token=...`; no arbitrary host, path, fragment, redirect, extra parameter, or control character is accepted.
4. Stores the redirect in a bounded RAM-only one-use handoff for two minutes; the token is consumed before redirect and returns 410 on reuse/expiry.
5. The browser performs WebAuthn. No private key, credential object, passkey name, token, cookie, or upstream body is persisted or emitted in snapshots/SSE/logs.
6. The UI says enrollment is not confirmed; it never automatically logs in, changes 2FA, logs out, or updates job/history state.
7. The pending lock ends when preparation returns. The operator must finish or close the official page before starting other work on the same account; the tool cannot observe external WebAuthn completion.

8. The dashboard waits at most the preparation budget plus 30 seconds, retains errors in the dialog, and releases its controls on failure. A client timeout does not cancel server work; server-side same-account guards remain authoritative.
9. Pure-request login runs in a thread, which asyncio cancellation cannot stop. On deadline/cancellation, preparation waits for that single login to settle before releasing the account lock and never requests an enrollment token afterward. Cleanup can exceed the preparation budget; the UI deadline still releases the dialog controls without declaring the account idle.

Bulk passkey page:

1. `/passkey` renders a dedicated full-width page rather than a modal. It accepts `email|password|2FA` lines and uses the configured 2FA concurrency and timeout values for its RAM-only workers. The input/output panes and queue receive independent, taller scroll areas so the operator can inspect more data at once.
2. After each account's official handoff is ready, requests a native Chrome window (`native_window=true`). Every submitted record snapshots its 1-based window position and the deduplicated batch size. `PasskeyWindows` starts the Google Chrome executable directly with a unique temporary profile plus `--new-window`, `--window-position`, and `--window-size`; before launch it writes only Chromium's registered `webauthn.create_in_icloud_keychain=true` preference to that profile. On supported macOS/Chrome versions, a compatible creation request therefore goes directly to the native Passwords/iCloud Keychain prompt. This matches ChatGPT Web's independent-browser behavior and does not require macOS Automation/Apple Events permission. All accounts in one submitted batch therefore receive separate, pre-tiled Chrome processes. No existing Chrome or default-browser tab is reused, moved, or closed. The temporary profile is removed after the operator closes that Chrome process. If Chrome is missing, profile preparation fails, launch fails, or the platform is unsupported, the dashboard retains a one-use manual fallback link valid for two minutes. Suite mode uses `twofa.localhost:<configured hub port>`; standalone mode uses its configured loopback host/port. Neither arbitrary URLs nor commands can be submitted to the native launcher.
3. Exposes only email, status, phase, and fixed milestone logs. Passwords, TOTP secrets, access tokens, cookies, enrollment URLs, and private keys never enter snapshots, SSE, output, or persistence.
4. Each bulk job can issue one handoff. Only queued/running jobs lock the account. A later explicit submission can create a fresh job for the same account with the newly entered credentials, keeping previous terminal jobs and issued links. Retry cannot overlap another active passkey job for the same email. A restart loses all bulk jobs and pending handoffs.
5. The UI keeps the entered input while the page remains open and failed submissions preserve it. Opening a window never means WebAuthn enrollment succeeded. Clearing jobs, leaving the page, or engine shutdown does not close the operator's Chrome windows. The explicit `Đóng tất cả cửa sổ` action runs immediately without a confirmation dialog and closes only the Chrome processes tracked by `PasskeyWindows`; it never scans or closes the operator's normal Chrome windows or tabs. The passkey SSE stays connected while `/passkey` is visible or while automatic handoffs remain pending, even when Chrome hides the page.

Protocol evidence (2026-09-27): the current official `BrowserMfaEnrollPage` in
`https://chatgpt.com/cdn/assets/async/141188.6e7ea20b77.js` requests the MFA token
and constructs the enrollment URL as above. `/auth/enroll_mfa?factor=passkey`
returns an HTTP 200 application shell, not an HTTP redirect. A controlled
authenticated check confirmed the token endpoint returns HTTP 200 with a
`state_token`. WebAuthn completion remains operator-controlled and unverified;
the tool never creates a credential itself. Unexpected responses fail closed.

## 18. Retry, auto-retry, stop, delete, and clear

Manual retry:

- the UI sends it only for error or cancelled rows;
- the backend retry() method currently accepts success, error, or cancelled because all three are in TERMINAL;
- rejects account_die and invalid_credentials;
- increments retry_count;
- preserves the current password and secret;
- preserves rotated_pending_verify, so a checkpointed rotation resumes at verification;
- resets timestamps and queues the row.

A direct authenticated retry request against a successful change_2fa row can therefore queue a new rotation because rotated_pending_verify is already false. The dashboard does not expose that action, but the backend currently permits it; do not treat retry() as failure-only without first adding a server-side state guard.

Auto-retry:

- runs only for retryable errors;
- obeys twofa.auto_retry, max count, and delay;
- schedules _delayed_retry();
- retries only if the row is still error when the delay expires.

Retry all in the UI iterates retryable error/cancelled rows sequentially and calls the per-row API. It is not one atomic backend operation.

Stop:

- running row: cancel the tracked _run task, which persists cancelled in its exception path;
- queued row: mark cancelled immediately;
- stop all: apply Stop to all queued and running rows.

Delete and clear:

- Delete one requires terminal status and no Usage refresh.
- Clear failed removes only error/cancelled rows whose account_state is not live.
- Clear failed deliberately preserves Live rows, successful rows, and active rows.
- Clear all requires every job to be terminal and no Usage refresh in progress.
- Clear all deletes only job_type twofa_community rows.
- Deleting jobs removes their logs but never deletes twofa_history.
- Clear-all SQLite deletion is dispatched to a worker thread so a large
  `job_logs` cascade or brief SQLite writer contention cannot freeze SSE,
  health, or the dashboard event loop. The manager serializes clear requests,
  keeps its in-memory snapshot until the transaction completes, and publishes
  one empty snapshot afterward. The UI disables the button during the request
  and aborts its wait after 60 seconds with a retryable error.
- Password clear uses the same non-blocking transaction boundary and an
  independent in-flight guard; it never touches the 2FA queue or history.

The UI Clear all action has no separate confirmation dialog in app.js; the
backend state guard is the final protection. A clear request never deletes
verified 2FA or password history.

## 19. Realtime SSE flow

EventSource connects with the auth token in the query string because the browser EventSource API cannot attach the custom X-Auth-Token header used by normal requests.

For each connection, server.py:

1. compares the query token;
2. creates a subscriber asyncio.Queue with capacity 100;
3. immediately sends a complete snapshot plus worker health;
4. sends job, worker_health, snapshot, or removed events;
5. emits an SSE comment keepalive every 15 seconds;
6. unsubscribes in finally when the stream ends.

When a subscriber queue is full, the manager drops its oldest pending event before inserting the newest. There is no event revision number or immediate gap-resync protocol. A reconnect receives a fresh complete snapshot, but a live saturated connection can temporarily miss intermediate transitions.

The dashboard treats SSE connections as foreground resources. The main 2FA
stream closes when its tab becomes hidden and reconnects with a fresh snapshot
when the tab becomes visible. The password stream additionally exists only
while the password workspace is open. The passkey stream belongs to the
dedicated `/passkey` page and remains alive while a submitted handoff still
needs to open Chrome; otherwise it follows page visibility. All streams close
on `pagehide`. This
prevents several retained dashboard tabs from exhausting Chromium's per-origin
HTTP/1.1 connection pool and blocking a later page reload.

realtime-ui.js batches events for 100 ms. app.js then:

- replaces all jobs for snapshot;
- upserts one job for job;
- updates health without rebuilding the table for worker_health-only bursts;
- deletes client state for removed;
- refreshes /api/output only when a change_2fa job transitions into success;
- opens the per-row result dialog only for IDs tracked as pending row changes.

Snapshots and SSE contain no password or TOTP secret.

## 20. Output, raw data, filtered export, and history

There are six credential-bearing views with different semantics:

| View | Eligibility and lifetime |
|---|---|
| Verified output | Current jobs where mode change_2fa + status success + login_verified true |
| Raw row | Any existing job; current persisted email, password, and secret |
| Filtered export | Any current job matching the selected filter; may include queued, running, failed, old, new, or checkpointed secrets |
| 2FA history | Verified rotations only; survives queue deletion |
| Password output | Password jobs where status success + login_verified true |
| Password history | Verified password changes only; survives password queue deletion |

All use the re-importable form email|password|CURRENT_SECRET.

GET /api/jobs/export:

- whitelists view and filename;
- includes Cache-Control no-store, nosniff, and X-Export-Count;
- adds a final newline when non-empty.

GET /api/jobs/{id}/raw and GET /api/twofa-history include no-store and nosniff.

GET /api/output is token-protected and supplies an attachment filename, but currently has no explicit Cache-Control no-store. This is a known hardening gap.

The dashboard:

- holds verified output in state.output and a textarea;
- can copy it or download it from the server;
- downloads filtered exports using the server filename and count;
- loads history only while its modal is open;
- can copy one/all history rows or build a local twofa-history.txt Blob;
- clears history arrays and DOM nodes when the modal closes.

## 21. Logging, privacy, and local security boundary

Sensitive data:

- account password;
- pending common target password;
- old and new TOTP secrets;
- local auth token;
- access and session tokens;
- cookies;
- SQLite database, backups, exports, clipboard, draft, raw responses, and history.

Current protections:

- job.snapshot() excludes password and secret;
- PasswordJob.snapshot() also excludes pending_password;
- normal SSE payloads use snapshots;
- _append_job_log() replaces the current password and current secret with stars and truncates the line;
- Usage failures log exception type instead of upstream response content;
- raw/history/filter endpoints require the token and set no-store;
- password output/history endpoints require the token and set no-store/nosniff;
- result and history dialogs clear client state on close;
- server binding is restricted to loopback;
- sensitive SettingsRepository audit values are redacted.

Limits of those protections:

- jobs, settings draft, target password, twofa_history, and password_history store credentials as SQLite text;
- a log message containing an older secret after job.secret changes would not be removed by the current-secret replacement;
- bootstrap exposes the control token to any caller that can reach the loopback endpoint;
- the SSE query token can appear in local URL/request diagnostics;
- Clipboard and downloaded files leave application control;
- /api/output lacks no-store.

Never put credential material in routine logs, toasts, public snapshots, SSE, console output, issue text, MEMORY.md, or test fixtures copied from real accounts.

## 22. Shutdown and macOS menu flow

Source mode stops through Ctrl+C or another termination signal.

Frozen macOS mode:

1. server.py detects darwin + frozen.
2. macos_integration.py locates the bundled ShoptaikhoanMenuBar executable.
3. It launches the helper detached with parent PID and localhost URL.
4. The Swift helper creates an accessory menu-bar item.
5. Mở Tool opens the localhost URL.
6. Thoát Shoptaikhoan Tool sends SIGTERM to the FastAPI parent and exits.
7. A one-second timer exits the helper when the parent no longer exists.
8. server.py finally terminates the helper if it is still alive.

FastAPI shutdown cancels active job and worker tasks. _run converts active cancellation to persisted cancelled. Engine.close() prevents new transactions, waits briefly for the writer lock, closes the writer, then closes tracked read connections.

## 23. macOS build and packaging

build-macos.sh currently targets Apple Silicon macOS 13.5 or newer. It:

1. checks macOS arm64, Python environment, PyInstaller, Node, and Swift compiler;
2. creates the app icon;
3. compiles MenuBarApp.swift;
4. invokes PyInstaller with ShoptaikhoanTool.spec;
5. ad-hoc signs and verifies the app;
6. runs the frozen dependency check under Finder's sparse PATH;
7. verifies embedded Node;
8. creates a ZIP and a DMG with Applications symlink and instructions.

ShoptaikhoanTool.spec:

- entry point is change 2fa community/server.py;
- bundles static assets, Sentinel JavaScript, Node, curl_cffi files, and menu helper;
- lists pure-request hidden imports;
- explicitly excludes Playwright and Camoufox;
- builds a UI-element app with no console window and one-instance metadata.

Packaging changes must preserve the Sentinel asset, embedded Node resolution, CA bundle behavior, static directory, and menu helper contract.

## 24. Known failure windows and sharp edges

### Rotation is disable-before-enable

The old factor is disabled before the replacement is enrolled and activated. The sequence is not atomic. A failure in the middle can leave no active TOTP factor or uncertain server state.

### Partial enrollment is not checkpointed

enable_2fa() can attach secret, factor_id, and session_id to MfaError.partial_state after enrollment. TwoFAService.rotate() currently converts that exception to TwoFAFlowError without forwarding partial state to the job checkpoint.

The highest-risk case is activation succeeding server-side while its response is lost: the old secret may fail and the new secret may never reach SQLite. rotated_pending_verify protection starts only after rotate_2fa() returns successfully.

### Frontend mode is not a backend safety gate

twofa.change_enabled selects the batch mode in app.js. A direct authenticated POST /api/jobs can still request change_2fa.

### SSE can drop intermediate events

Subscriber queues discard the oldest event at capacity 100. Reconnect gives a fresh snapshot, but there is no revision/gap detector during one live connection.

### Shared schema is larger than the app

Do not revive signup, browser automation, Outlook, iCloud, payment, or session-export behavior merely because related functions, settings, repositories, and tables exist.

## 25. Invariants future changes must preserve

1. check_only, Recheck, and Refresh Usage never mutate 2FA.
2. A reported rotation success requires a fresh login with the stored new secret.
3. rotated_pending_verify always selects verify(), never rotate().
4. A recoverable new secret must be persisted before post-rotation verification.
5. Completion and verified history insertion remain atomic.
6. Clearing the queue never clears verified history.
7. Snapshots, SSE, logs, toasts, and routine console output contain no credentials.
8. Credential-bearing API responses remain token-protected and should use no-store.
9. Manager mutations remain on its application event-loop thread.
10. Worker reconciliation remains idempotent and self-healing.
11. Fatal credential classification remains conservative.
12. Server binding remains loopback-only.
13. Source and frozen builds keep the pure-request dependency contract.
14. Do not add unrelated network work inside disable, enroll, and activate.
15. Disabled Usage/payment inspection flags never call their upstream endpoints.
16. Password mutation success requires a fresh login with pending_password.
17. Once mutation_started is durable, recovery never sends the mutation again automatically.
18. Target passwords never appear in bootstrap, snapshots, SSE, logs, or toasts.
19. Password reset is never submitted unless OAuth re-auth reaches and lands on
    `/reset-password/new-password`.
20. Password-login logs expose only whitelisted milestones, never OAuth URLs,
    account identity, callback parameters, or MFA challenge IDs.
21. Delete-all-chats always requires a successful Live row plus explicit confirmation, stays RAM-only, and is never replayed after restart.
22. Delete-all-chats never changes 2FA/password job state, output, history, Usage, payment data, or SQLite records.
23. Logout-all-sessions always requires a successful Live row plus explicit confirmation, stays RAM-only, is never retried or replayed after restart, and never logs in again after revocation.
24. Logout-all-sessions blocks same-account conflicting work but never changes another account's queue or state.
25. Passkey preparation requires a verified Live row, is RAM-only and one-use, never stores a private key, and never claims WebAuthn completion.
26. Bulk passkey jobs are RAM-only, use one handoff per account, never persist credentials or enrollment URLs, and never claim WebAuthn completion.
27. The dashboard can select multiple successful Live rows for logout. Each selected account reuses the confirmed single-account route exactly once, with bounded client concurrency, no automatic retry, and independent failures.

## 26. Agent change-impact map

| If changing | Read first | Closest verification |
|---|---|---|
| Startup, host, token, route, response headers | server.py, macos_integration.py | filtered export API and macOS tests |
| Queue, worker health, retry, recovery | jobs.py, JobRepository | test_worker_resilience.py |
| Password queue, HTTP mutation, recovery | password_jobs.py, password_service.py, password_phase.py | test_password_feature.py |
| Recheck or per-row Change 2FA | jobs.py, app.js | test_job_recheck.py, filtered export API tests |
| Delete all chats | session_phase.py, service.py, jobs.py, server.py, app.js | test_delete_all_chats.py, test_delete_chats_ui.js |
| Logout all sessions | session_phase.py, service.py, jobs.py, server.py, app.js | test_logout_all_sessions.py, test_logout_sessions_ui.js |
| Add passkey | passkey_service.py, service.py, jobs.py, server.py, app.js | test_passkey_feature.py, test_passkey_ui.js |
| Bulk add passkey | passkey_jobs.py, passkey_service.py, server.py, passkey-ui.js | test_passkey_jobs.py, test_passkey_batch_ui.js |
| Login, OAuth, password, TOTP challenge | service.py, session_phase.py, request_phase.py, Sentinel modules | service tests plus dependency check |
| Disable, enroll, activate | mfa_phase.py, totp_helper.py, service.py, checkpoint path | rotation and history tests; inspect partial-state risk |
| Plan, Usage, saved payment methods, or inspection switches | jobs.py, service.py, server.py, session_phase.py, app.js, usage-ui.js, payment-ui.js | test_inspection_options.py, test_usage_feature.py, test_usage_refresh.py, test_payment_methods_feature.py, test_inspection_options_ui.js, test_usage_ui.js, test_payment_ui.js |
| SQLite schema or transactions | engine.py, schema.py, active repository methods | all Python tests and migration scenario |
| Output, raw, filter, history | jobs.py, server.py, app.js | filtered export and history tests |
| Realtime rendering | jobs.py subscribe/broadcast, server events, realtime-ui.js, app.js | test_realtime_ui.js |
| macOS bundle | build-macos.sh, spec, macos_integration.py, MenuBarApp.swift | macOS tests and frozen dependency check |
| UI layout only | index.html, dashboard.css, app.js render functions | JavaScript syntax, Node tests, browser viewport inspection |

Recommended read order for a new Agent:

1. This document.
2. server.py.
3. jobs.py, especially snapshot(), start(), _run(), retry/recheck/enqueue_change_2fa(), clear/output, and SSE methods.
4. service.py; for password work, password_jobs.py, password_service.py, then password_phase.py.
5. app.js and password-ui.js, then realtime-ui.js, usage-ui.js, and payment-ui.js.
6. session_phase.py pure-request functions and request_phase.py.
7. mfa_phase.py.
8. active JobRepository and SettingsRepository methods, then schema and engine.
9. launch and packaging files only when the task touches distribution.

## 27. Test map and verification

Python suite includes the original runtime regression tests plus the dedicated
password feature tests.

| Test file | Main contract |
|---|---|
| tests/test_filtered_export.py | Filter semantics, raw values, output selection |
| tests/test_filtered_export_api.py | Auth, routes, headers, status mapping, protected data |
| tests/test_job_recheck.py | Recheck remains non-mutating |
| tests/test_macos_integration.py | Menu helper command and launch conditions |
| tests/test_password_feature.py | Settings secrecy, HTTP contract, mutation checkpoint, ambiguity recovery, atomic promotion, separate API |
| tests/test_twofa_history.py | Atomic verified history, ordering, backfill, queue independence |
| tests/test_usage_feature.py | Usage parser, service fail-soft behavior, persistence |
| tests/test_usage_refresh.py | Eligibility, non-mutation, persistence, safe errors |
| tests/test_delete_all_chats.py | HTTP contract, confirmation/auth, RAM-only lock, non-interference, safe errors |
| tests/test_payment_methods_feature.py | Safe payment parsing/fetching, fail-soft service behavior, persistence |
| tests/test_inspection_options.py | Default/snapshot/recovery semantics and zero upstream calls when disabled |
| tests/test_worker_resilience.py | Worker recovery, health, persistence failure containment |
| tests/test_usage_ui.js | Usage formatting and filter/export metadata |
| tests/test_payment_ui.js | Safe payment formatting, table wiring, loading/empty/unavailable states |
| tests/test_inspection_options_ui.js | Compact switches, Settings payload, and explicit disabled states |
| tests/test_realtime_ui.js | Event batching and output refresh trigger |
| tests/test_event_connection_lifecycle.js | Foreground-only 2FA SSE, lazy password SSE, pagehide cleanup |
| tests/test_password_ui.js | Separate password DOM/API wiring and sensitive-state cleanup |
| tests/test_password_history_ui.js | Email filtering, credential-field parsing, and filtered-copy selection |
| tests/test_delete_chats_ui.js | Eligible row action, destructive confirmation copy, payload, and pending lock |

Run from repository root:

~~~bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
node tests/test_usage_ui.js
node tests/test_payment_ui.js
node tests/test_delete_chats_ui.js
node tests/test_inspection_options_ui.js
node tests/test_realtime_ui.js
node tests/test_password_ui.js
node tests/test_password_history_ui.js
.venv/bin/python 'change 2fa community/server.py' --check-runtime-dependencies
.venv/bin/python -m compileall -q \
  'change 2fa community' session_phase.py request_phase.py password_phase.py mfa_phase.py db
node --check 'change 2fa community/static/app.js'
node --check 'change 2fa community/static/password-ui.js'
node --check 'change 2fa community/static/password-history-ui.js'
node --check 'change 2fa community/static/realtime-ui.js'
node --check 'change 2fa community/static/usage-ui.js'
node --check 'change 2fa community/static/payment-ui.js'
bash -n start-unix.sh build-macos.sh packaging/macos/make-icon.sh
git diff --check
~~~

For documentation changes, also:

- compare every documented route with decorators in server.py;
- confirm every named file and symbol exists;
- parse every Mermaid block with Mermaid v11;
- scan the documentation for accidental credentials;
- inspect the final diff.

## 28. Fast onboarding questions

An Agent understands the whole app only when it can answer:

- Which browser is opened, and which remote browser is never used?
- What happens before FastAPI starts accepting requests?
- Why can bootstrap return a token without an auth header?
- Which client state is safe snapshot data and which state contains credentials?
- How do source and frozen macOS startup differ?
- How does a line become a queued job and then a worker task?
- What selects check(), rotate(), or verify()?
- Where is a new secret first persisted?
- Why does rotated_pending_verify prevent a second rotation?
- What exact fields make verified output eligible?
- How do raw, filtered export, verified output, and history differ?
- Which operations are guaranteed non-mutating?
- Why can a password job with mutation_started=true never resend automatically?
- Where is pending_password promoted, and what proves the promotion is safe?
- What is removed by Delete, Clear failed, and Clear all?
- How does the UI recover realtime state after reconnect?
- Which database tables are active, shared-side-effect, or legacy?
- What can happen between disable_in_house and activate_enrollment?
- Which tests protect the module an Agent is about to change?

If any answer is unclear, trace the named function before editing.

## 29. Separate password-change workflow

The password feature is not a mode inside TwoFAJobManager. It owns:

- job_type `password_community`;
- PasswordJobManager workers, queue, state, SSE, output, and history;
- `/api/password/*` and `/api/password-settings` routes;
- `password-ui.js` and the separate password workspace;
- `jobs.pending_password` and `password_history`.

At enqueue, PasswordJobManager reads
`password_change.target_password` exactly once and copies it into every new
job's pending_password. A later Settings edit affects only later jobs. Public
job snapshots expose phase booleans but omit current password, pending password,
and TOTP secret. Repository failures are contained per job; worker health
reports only the exception type and persistence-failure count, never SQL values
or credentials.

The common target is editable from both Settings and the password workspace.
The default settings-status response stays non-sensitive; only explicit local
views receive the target, behind `X-Auth-Token` with `Cache-Control: no-store`
and `X-Content-Type-Options: nosniff`. PUT responses, validation errors, audit
rows, snapshots, SSE, toasts, and normal logs do not echo it.

~~~mermaid
sequenceDiagram
  autonumber
  actor User as Operator
  participant UI as password-ui.js
  participant API as Password API
  participant JM as PasswordJobManager
  participant SVC as PasswordService
  participant Auth as auth.openai.com
  participant Login as Pure-request login
  participant DB as SQLite

  User->>UI: Save common target password
  UI->>API: PUT /api/password-settings
  API->>DB: Store local SQLite plaintext KV with masked audit value
  User->>UI: Submit email|current_password|TOTP
  UI->>API: POST /api/password/jobs
  API->>JM: add(lines)
  JM->>DB: Persist current + pending password
  JM->>SVC: change(... mutation_started=false)
  SVC->>Login: Login current password + TOTP
  SVC->>JM: Request durable mutation_started checkpoint
  JM->>DB: Persist mutation_started before mutation
  SVC->>Auth: Start NextAuth password-reset re-auth intent
  SVC->>Auth: Re-auth current password + optional TOTP
  SVC->>Auth: POST password/reset exactly once
  SVC->>Login: Fresh login target password + same TOTP
  Login-->>SVC: accessToken proves new credential
  SVC-->>JM: login_verified=true
  JM->>DB: Atomically promote pending password + insert history
  JM-->>UI: Safe SSE success and refresh verified output
~~~

### Current pure-request auth-web contract

password_phase.py mirrors the current official auth.openai.com web client, not a
documented public OpenAI API:

1. inject cookies returned by get_session_pure_request();
2. fetch ChatGPT NextAuth CSRF state;
3. POST `/api/auth/signin/openai` with the authenticated user's `login_hint`,
   callback `https://chatgpt.com/`, and authorization parameters
   `reauth=password`, `max_age=0`, and `post_login_password_reset=true`;
4. follow the returned authorize URL once and require its validated landing to
   be `/log-in/password` (do not reopen that document a second time);
5. create a Sentinel token for `password_verify`;
6. POST `/api/accounts/password/verify` with the current password;
7. if requested, issue and verify the existing TOTP challenge;
8. require page type `reset_password_new_password` or its matching continue
   route, then require the final document landing to be exactly
   `/reset-password/new-password`;
9. create a Sentinel token for `password_reset`;
10. POST `/api/accounts/password/reset` with `{"password": target}` once;
11. discard that transport session and verify through a completely fresh login.

If any route or state gate differs, the adapter raises a definitive rejection
before the reset POST. Login diagnostics are reduced to fixed milestone labels;
OAuth queries, callback values, identities, and MFA challenge IDs are discarded.

A 4xx reset response other than 408 is treated as a definitive rejection.
Timeouts, transport failures, redirects, HTTP 408, and 5xx responses are
ambiguous after the POST and therefore enter login reconciliation; they never
authorize an automatic second reset request.

Known 400 policy codes are translated through a fixed whitelist
(`password_too_weak`, `password_already_used`, and
`password_contains_user_info`). Upstream response messages are never copied
into logs, snapshots, or API errors because they may contain sensitive input.

The endpoint is private web-client behavior and may change upstream. Do not
silently guess a replacement contract. Update the adapter and its contract test
from current official bundles or a controlled network capture.

### Non-idempotent mutation and recovery

The durable mutation_started checkpoint is written before the reset POST. From
that point onward, timeout, cancellation, crash, or an ambiguous response cannot
authorize an automatic resend.

Recovery order is fixed:

1. fresh login with pending_password;
2. if it succeeds, complete verified success;
3. otherwise try the old password;
4. if old succeeds, mark password_not_applied;
5. if neither succeeds, mark password_uncertain and block retry.

Only an explicit human Retry after a definitive rejection/not-applied result
clears mutation_started and authorizes one new attempt. An uncertain job remains
non-retryable.

### Atomic completion and credential-bearing views

JobRepository.complete_password_success() runs one transaction that:

1. requires a non-null pending_password;
2. sets jobs.password=pending_password;
3. clears pending_password;
4. marks success with login_verified state;
5. inserts password_history from the promoted job row.

`/api/password/output` and `/api/password/history` are token-protected,
credential-bearing, and use no-store/nosniff. Their line format remains:

~~~text
email|VERIFIED_NEW_PASSWORD|CURRENT_TOTP_SECRET
~~~
