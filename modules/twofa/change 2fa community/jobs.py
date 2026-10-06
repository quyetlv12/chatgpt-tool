"""Batch jobs and SQLite persistence for Shoptaikhoan Tool."""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from service import TwoFAService


JOB_TYPE = "twofa_community"
TERMINAL = {"success", "error", "cancelled"}
EXPORT_FILTERS = {"all", "running", "success", "usage-low", "usage-full", "free-no-usage", "error"}


def _safe_payment_methods(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    try:
        from session_phase import _sanitize_payment_methods

        return _sanitize_payment_methods(value)
    except Exception:
        return None


def _safe_billing_date(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


class JobPersistenceError(RuntimeError):
    """Raised after a repository failure has been recorded safely."""


@dataclass(slots=True)
class TwoFAJob:
    id: str
    email: str
    password: str
    secret: str
    mode: str = "change_2fa"
    status: str = "queued"
    error: str | None = None
    error_kind: str | None = None
    account_state: str = "unknown"
    plan: str | None = None
    plan_source: str | None = None
    usage: dict[str, Any] | None = None
    payment_methods: list[dict[str, Any]] | None = None
    billing_date: str | None = None
    usage_enabled: bool = True
    payment_methods_enabled: bool = True
    usage_refreshing: bool = False
    chat_deleting: bool = False
    sessions_logging_out: bool = False
    passkey_preparing: bool = False
    rotated_pending_verify: bool = False
    login_verified: bool = False
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    logs: list[str] = field(default_factory=list)

    @property
    def retryable(self) -> bool:
        return self.error_kind not in {"account_die", "invalid_credentials"}

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "mode": self.mode,
            "status": self.status,
            "error": self.error,
            "error_kind": self.error_kind,
            "account_state": self.account_state,
            "plan": self.plan,
            "plan_source": self.plan_source,
            "usage": self.usage,
            "payment_methods": _safe_payment_methods(self.payment_methods),
            "billing_date": _safe_billing_date(self.billing_date),
            "usage_enabled": self.usage_enabled,
            "payment_methods_enabled": self.payment_methods_enabled,
            "usage_refreshing": self.usage_refreshing,
            "chat_deleting": self.chat_deleting,
            "sessions_logging_out": self.sessions_logging_out,
            "passkey_preparing": self.passkey_preparing,
            "retryable": self.retryable,
            "rotated_pending_verify": self.rotated_pending_verify,
            "login_verified": self.login_verified,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_tail": self.logs[-3:],
        }


class TwoFAJobManager:
    DEFAULTS = {
        "twofa.max_concurrent": 3,
        "twofa.job_timeout": 180,
        "twofa.auto_retry": False,
        "twofa.auto_retry_max": 1,
        "twofa.auto_retry_delay": 3,
        "twofa.change_enabled": False,
        "twofa.read_usage": True,
        "twofa.read_payment_methods": True,
        "twofa.input_draft": "",
    }

    def __init__(self, job_repo, settings_repo, service: TwoFAService | None = None) -> None:
        self.job_repo = job_repo
        self.settings_repo = settings_repo
        self.service = service or TwoFAService()
        self.jobs: dict[str, TwoFAJob] = {}
        self.order: list[str] = []
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._retire_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._stopping = False
        self._planned_worker_exits: set[asyncio.Task] = set()
        self._clear_lock = asyncio.Lock()
        self._clear_in_progress = False
        self._worker_restarts = 0
        self._persistence_failures = 0
        self._last_worker_error: str | None = None
        self._last_persistence_error: str | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self.settings = dict(self.DEFAULTS)
        self._load_settings()
        self._recover()
        self._backfill_twofa_history()

    @staticmethod
    def _legacy_error_metadata(error: Any) -> tuple[str | None, str]:
        if not isinstance(error, str) or not error.strip():
            return None, "unknown"
        from session_phase import classify_account_check_error, is_fatal_login_error

        if classify_account_check_error(error) == "deactivated":
            return "account_die", "die"
        if is_fatal_login_error(error):
            return "invalid_credentials", "unknown"
        return "technical_error", "unknown"

    def _load_settings(self) -> None:
        stored = self.settings_repo.list("twofa")
        self.settings.update({key: value for key, value in stored.items() if key in self.DEFAULTS})

    @staticmethod
    def parse_combo(line: str) -> tuple[str, str, str]:
        parts = [part.strip() for part in line.strip().split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError("Định dạng phải là email|password|2FA_cũ")
        email, password, secret = parts
        if "@" not in email:
            raise ValueError("Email không hợp lệ")
        return email.casefold(), password, secret.replace(" ", "").upper()

    def _recover(self) -> None:
        for row in self.job_repo.list_all():
            if row.get("job_type") != JOB_TYPE:
                continue
            state = self._decode_state(row.get("account_check"))
            status = str(row.get("status") or "error")
            if status in {"running", "queued"}:
                status = "queued"
            legacy_kind, legacy_account_state = self._legacy_error_metadata(row.get("error"))
            job = TwoFAJob(
                id=str(row["id"]),
                email=str(row["email"]),
                password=str(row.get("password") or ""),
                secret=str(row.get("secret") or ""),
                mode=str(state.get("mode") or "change_2fa"),
                status=status,
                error=row.get("error"),
                error_kind=state.get("error_kind") or legacy_kind,
                account_state=str(state.get("account_state") or legacy_account_state),
                plan=state.get("plan"),
                plan_source=state.get("plan_source"),
                usage=state.get("usage") if isinstance(state.get("usage"), dict) else None,
                payment_methods=_safe_payment_methods(state.get("payment_methods")),
                billing_date=_safe_billing_date(state.get("billing_date")),
                usage_enabled=state.get("usage_enabled") is not False,
                payment_methods_enabled=state.get("payment_methods_enabled") is not False,
                rotated_pending_verify=bool(state.get("rotated_pending_verify")),
                login_verified=bool(state.get("login_verified")),
                retry_count=int(state.get("retry_count") or 0),
                created_at=float(row.get("created_at") or time.time()),
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
                logs=[str(item.get("line") or "") for item in self.job_repo.get_logs(str(row["id"]))],
            )
            self.jobs[job.id] = job
            self.order.append(job.id)

    def _backfill_twofa_history(self) -> None:
        """Preserve successful legacy rotations created before history v14."""
        record = getattr(self.job_repo, "record_twofa_history", None)
        if not callable(record):
            return
        for job in self.jobs.values():
            if not (
                job.mode == "change_2fa"
                and job.status == "success"
                and job.login_verified
                and job.finished_at is not None
            ):
                continue
            self._persist(
                lambda job=job: record(
                    job_id=job.id,
                    email=job.email,
                    password=job.password,
                    secret=job.secret,
                    changed_at=float(job.finished_at),
                ),
                "2FA history backfill",
            )

    @staticmethod
    def _decode_state(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw:
            try:
                value = json.loads(raw)
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _state(self, job: TwoFAJob) -> str:
        return json.dumps({
            "rotated_pending_verify": job.rotated_pending_verify,
            "login_verified": job.login_verified,
            "retry_count": job.retry_count,
            "mode": job.mode,
            "error_kind": job.error_kind,
            "account_state": job.account_state,
            "plan": job.plan,
            "plan_source": job.plan_source,
            "usage": job.usage,
            "payment_methods": _safe_payment_methods(job.payment_methods),
            "billing_date": _safe_billing_date(job.billing_date),
            "usage_enabled": job.usage_enabled,
            "payment_methods_enabled": job.payment_methods_enabled,
        }, ensure_ascii=False)

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._started:
            if loop is not self._loop:
                raise RuntimeError("Job manager đã chạy trên event loop khác")
            self._reconcile_workers()
            return
        self._loop = loop
        self._stopping = False
        self._started = True
        self._reconcile_workers()
        for job in self.jobs.values():
            if job.status == "queued":
                self._queue.put_nowait(job.id)

    def _active_workers(self) -> list[asyncio.Task]:
        self._workers[:] = [task for task in self._workers if not task.done()]
        return self._workers

    def _assert_event_loop_thread(self) -> None:
        if not self._started:
            return
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is not self._loop:
            raise RuntimeError("Job manager mutation phải chạy trên event-loop thread")

    def _reconcile_workers(self) -> None:
        if not self._started or self._stopping:
            return
        self._spawn_workers(int(self.settings["twofa.max_concurrent"]))

    def _spawn_workers(self, target: int) -> None:
        workers = self._active_workers()
        while len(workers) < target:
            task = asyncio.create_task(self._worker())
            task.add_done_callback(self._worker_done)
            workers.append(task)

    def _worker_done(self, task: asyncio.Task) -> None:
        planned = task in self._planned_worker_exits
        self._planned_worker_exits.discard(task)
        if task in self._workers:
            self._workers.remove(task)
        if self._stopping or not self._started or planned:
            self._broadcast_health()
            return

        self._worker_restarts += 1
        if task.cancelled():
            self._last_worker_error = "CancelledError"
        else:
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = None
            self._last_worker_error = type(error).__name__ if error else "WorkerStopped"
        self._broadcast_health()
        self._reconcile_workers()
        self._broadcast_health()

    async def _claim_retirement(self) -> bool:
        async with self._retire_lock:
            workers = self._active_workers()
            target = int(self.settings["twofa.max_concurrent"])
            current = asyncio.current_task()
            if len(workers) <= target or current not in workers:
                return False
            self._planned_worker_exits.add(current)
            return True

    async def _resize_workers(self, previous: int, target: int) -> None:
        workers = self._active_workers()
        if target > len(workers):
            self._spawn_workers(target)
            return
        for _ in range(max(0, previous - target)):
            self._queue.put_nowait(None)

    async def shutdown(self) -> None:
        if not self._started and not self._workers:
            return
        self._assert_event_loop_thread()
        self._stopping = True
        self._started = False
        workers = list(self._workers)
        running = list(self._tasks.values())
        self._planned_worker_exits.update(workers)
        for task in running + workers:
            task.cancel()
        await asyncio.gather(*running, *workers, return_exceptions=True)
        self._workers.clear()
        self._tasks.clear()
        self._planned_worker_exits.clear()
        self._stopping = False

    def worker_health(self) -> dict[str, Any]:
        active = len(self._active_workers())
        configured = int(self.settings["twofa.max_concurrent"])
        return {
            "started": self._started,
            "configured": configured,
            "active": active,
            "busy": sum(not task.done() for task in self._tasks.values()),
            "queued": sum(job.status == "queued" for job in self.jobs.values()),
            "restarts": self._worker_restarts,
            "persistence_failures": self._persistence_failures,
            "last_worker_error": self._last_worker_error,
            "last_persistence_error": self._last_persistence_error,
            "degraded": self._started and active < configured,
        }

    def _record_persistence_failure(self, error: Exception) -> None:
        self._persistence_failures += 1
        self._last_persistence_error = type(error).__name__
        self._broadcast_health()

    def _persist(self, action: Callable[[], Any], status: str) -> Any:
        try:
            return action()
        except Exception as exc:
            self._record_persistence_failure(exc)
            raise JobPersistenceError(
                f"Không lưu được trạng thái {status} vào cơ sở dữ liệu"
            ) from exc

    def _persist_terminal(self, job: TwoFAJob, status: str) -> bool:
        try:
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id,
                    status,
                    error=job.error,
                    secret=job.secret,
                    password=job.password,
                    account_check=self._state(job),
                ),
                status,
            )
            return True
        except JobPersistenceError as exc:
            job.status = "error"
            job.error = str(exc)
            job.error_kind = "technical_error"
            job.finished_at = time.time()
            try:
                self._persist(
                    lambda: self.job_repo.update_status(
                        job.id,
                        "error",
                        error=job.error,
                        secret=job.secret,
                        password=job.password,
                        account_check=self._state(job),
                    ),
                    "error fallback",
                )
            except JobPersistenceError:
                pass
            return False

    def _append_job_log(self, job: TwoFAJob, message: str) -> None:
        safe = str(message)
        for sensitive in (job.password, job.secret):
            if sensitive:
                safe = safe.replace(sensitive, "***")
        stamped = f"{time.strftime('%H:%M:%S')}  {safe[:500]}"
        job.logs.append(stamped)
        job.logs[:] = job.logs[-300:]
        self._persist(
            lambda: self.job_repo.append_log(job.id, stamped),
            "log",
        )
        self._broadcast(job)

    def add(self, lines: list[str], mode: str = "change_2fa") -> list[dict[str, Any]]:
        self._assert_event_loop_thread()
        if self._clear_in_progress:
            raise ValueError("Đang dọn danh sách; vui lòng thử lại sau")
        if mode not in {"check_only", "change_2fa"}:
            raise ValueError("Chế độ phải là check_only hoặc change_2fa")
        for line in lines:
            self.assert_not_logging_out(line.split("|", 1)[0])
        created: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in lines:
            email, password, secret = self.parse_combo(line)
            if email in seen:
                continue
            seen.add(email)
            job = TwoFAJob(
                id=uuid.uuid4().hex,
                email=email,
                password=password,
                secret=secret,
                mode=mode,
                usage_enabled=bool(self.settings["twofa.read_usage"]),
                payment_methods_enabled=bool(
                    self.settings["twofa.read_payment_methods"]
                ),
            )
            self._persist(
                lambda: self.job_repo.create({
                    "id": job.id,
                    "email": email,
                    "combo": "[redacted]",
                    "mail_mode": "none",
                    "status": "queued",
                    "password": password,
                    "secret": secret,
                    "account_check": self._state(job),
                    "created_at": job.created_at,
                    "job_type": JOB_TYPE,
                }),
                "queued",
            )
            self.jobs[job.id] = job
            self.order.append(job.id)
            self._reconcile_workers()
            self._queue.put_nowait(job.id)
            created.append(job.snapshot())
            self._broadcast(job)
        return created

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id is None:
                    if await self._claim_retirement():
                        return
                    continue
                job = self.jobs.get(job_id)
                if job and job.status == "queued":
                    task = asyncio.create_task(self._run(job))
                    self._tasks[job.id] = task
                    await task
            finally:
                if job_id is not None:
                    self._tasks.pop(job_id, None)
                self._queue.task_done()

    async def _run(self, job: TwoFAJob) -> None:
        job.status = "running"
        job.error = None
        job.started_at = time.time()

        def log(message: str) -> None:
            self._append_job_log(job, message)

        async def checkpoint(new_secret: str) -> None:
            job.secret = new_secret
            job.rotated_pending_verify = True
            job.login_verified = False
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id,
                    "running",
                    secret=new_secret,
                    password=job.password,
                    account_check=self._state(job),
                ),
                "running checkpoint",
            )
            self._broadcast(job)

        try:
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id, "running", account_check=self._state(job)
                ),
                "running",
            )
            self._broadcast(job)
            timeout = float(self.settings["twofa.job_timeout"])
            if job.mode == "check_only":
                result = await self.service.check(
                    email=job.email,
                    password=job.password,
                    secret=job.secret,
                    timeout=timeout,
                    log=log,
                    read_usage=job.usage_enabled,
                    read_payment_methods=job.payment_methods_enabled,
                )
            elif job.rotated_pending_verify:
                result = await self.service.verify(
                    email=job.email,
                    password=job.password,
                    new_secret=job.secret,
                    timeout=timeout,
                    log=log,
                    read_usage=job.usage_enabled,
                    read_payment_methods=job.payment_methods_enabled,
                )
            else:
                result = await self.service.rotate(
                    email=job.email,
                    password=job.password,
                    old_secret=job.secret,
                    timeout=timeout,
                    checkpoint=checkpoint,
                    log=log,
                    read_usage=job.usage_enabled,
                    read_payment_methods=job.payment_methods_enabled,
                )
            job.secret = result.secret
            job.login_verified = result.login_verified
            job.account_state = result.account_state
            job.plan = result.plan
            job.plan_source = result.plan_source
            job.usage = result.usage
            job.payment_methods = _safe_payment_methods(
                getattr(result, "payment_methods", None)
            )
            job.billing_date = _safe_billing_date(
                getattr(result, "billing_date", None)
            )
            job.error_kind = None
            job.rotated_pending_verify = False
            job.status = "success"
            job.finished_at = time.time()
            complete_twofa = getattr(self.job_repo, "complete_twofa_success", None)
            if job.mode == "change_2fa" and job.login_verified and callable(complete_twofa):
                self._persist(
                    lambda: complete_twofa(
                        job_id=job.id,
                        email=job.email,
                        password=job.password,
                        secret=job.secret,
                        account_check=self._state(job),
                        changed_at=float(job.finished_at),
                    ),
                    "success + 2FA history",
                )
            else:
                self._persist(
                    lambda: self.job_repo.update_status(
                        job.id,
                        "success",
                        secret=job.secret,
                        password=job.password,
                        account_check=self._state(job),
                    ),
                    "success",
                )
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "Đã dừng bởi người dùng"
            job.finished_at = time.time()
            self._persist_terminal(job, "cancelled")
        except Exception as exc:
            job.status = "error"
            job.error = (str(exc).strip() or type(exc).__name__)[:240]
            job.error_kind = str(getattr(exc, "error_kind", "technical_error"))
            job.account_state = str(getattr(exc, "account_state", job.account_state))
            job.finished_at = time.time()
            self._persist_terminal(job, "error")
            if self._should_auto_retry(job):
                delay = float(self.settings["twofa.auto_retry_delay"])
                asyncio.create_task(self._delayed_retry(job.id, delay))
        finally:
            self._broadcast(job)

    def _should_auto_retry(self, job: TwoFAJob) -> bool:
        return (
            job.retryable
            and bool(self.settings["twofa.auto_retry"])
            and job.retry_count < int(self.settings["twofa.auto_retry_max"])
        )

    async def _delayed_retry(self, job_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        if job_id in self.jobs and self.jobs[job_id].status == "error":
            self.retry(job_id)

    def retry(self, job_id: str) -> dict[str, Any]:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        self.assert_not_logging_out(job.email)
        if job.usage_refreshing:
            raise ValueError("Usage của tài khoản đang được đọc lại")
        if job.chat_deleting:
            raise ValueError("Dữ liệu chat của tài khoản đang được xóa")
        if job.status not in TERMINAL:
            raise ValueError("Job đang chạy hoặc đang chờ")
        if not job.retryable:
            label = "tài khoản die" if job.error_kind == "account_die" else "sai thông tin đăng nhập/2FA"
            raise ValueError(f"Không retry tự động: {label}")
        previous = (job.retry_count, job.status, job.error, job.error_kind, job.finished_at)
        job.retry_count += 1
        job.status = "queued"
        job.error = None
        job.error_kind = None
        job.finished_at = None
        try:
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id,
                    "queued",
                    secret=job.secret,
                    password=job.password,
                    account_check=self._state(job),
                ),
                "queued",
            )
        except JobPersistenceError:
            job.retry_count, job.status, job.error, job.error_kind, job.finished_at = previous
            raise
        self._reconcile_workers()
        self._queue.put_nowait(job.id)
        self._broadcast(job)
        return job.snapshot()

    def recheck(self, job_id: str) -> dict[str, Any]:
        """Queue a completed account for a non-mutating account check."""
        self._assert_event_loop_thread()
        job = self._require(job_id)
        self.assert_not_logging_out(job.email)
        if job.usage_refreshing:
            raise ValueError("Usage của tài khoản đang được đọc lại")
        if job.chat_deleting:
            raise ValueError("Dữ liệu chat của tài khoản đang được xóa")
        if job.status != "success":
            raise ValueError("Chỉ có thể check lại tài khoản đã hoàn tất thành công")

        fields = (
            "mode",
            "status",
            "error",
            "error_kind",
            "account_state",
            "plan",
            "plan_source",
            "usage",
            "payment_methods",
            "billing_date",
            "usage_enabled",
            "payment_methods_enabled",
            "rotated_pending_verify",
            "login_verified",
            "retry_count",
            "started_at",
            "finished_at",
        )
        previous = {field: getattr(job, field) for field in fields}
        job.mode = "check_only"
        job.status = "queued"
        job.error = None
        job.error_kind = None
        job.account_state = "unknown"
        job.plan = None
        job.plan_source = None
        job.usage = None
        job.payment_methods = None
        job.billing_date = None
        job.usage_enabled = bool(self.settings["twofa.read_usage"])
        job.payment_methods_enabled = bool(
            self.settings["twofa.read_payment_methods"]
        )
        job.rotated_pending_verify = False
        job.login_verified = False
        job.retry_count = 0
        job.started_at = None
        job.finished_at = None
        try:
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id,
                    "queued",
                    secret=job.secret,
                    password=job.password,
                    account_check=self._state(job),
                ),
                "queued recheck",
            )
        except JobPersistenceError:
            for field, value in previous.items():
                setattr(job, field, value)
            raise

        self._reconcile_workers()
        self._queue.put_nowait(job.id)
        self._broadcast(job)
        return job.snapshot()

    def enqueue_change_2fa(self, job_id: str) -> dict[str, Any]:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        self.assert_not_logging_out(job.email)
        if job.usage_refreshing:
            raise ValueError("Usage của tài khoản đang được đọc lại")
        if job.chat_deleting:
            raise ValueError("Dữ liệu chat của tài khoản đang được xóa")
        if job.status != "success":
            raise ValueError("Chỉ có thể đổi 2FA cho tài khoản đã check thành công")
        if job.account_state != "live":
            raise ValueError("Tài khoản chưa được xác nhận Live")
        if job.mode != "check_only":
            raise ValueError("Tài khoản không còn ở chế độ Chỉ check")
        previous = (
            job.mode,
            job.status,
            job.error,
            job.error_kind,
            job.usage_enabled,
            job.payment_methods_enabled,
            job.rotated_pending_verify,
            job.login_verified,
            job.retry_count,
            job.started_at,
            job.finished_at,
        )
        job.mode = "change_2fa"
        job.status = "queued"
        job.error = None
        job.error_kind = None
        job.usage_enabled = bool(self.settings["twofa.read_usage"])
        job.payment_methods_enabled = bool(
            self.settings["twofa.read_payment_methods"]
        )
        job.rotated_pending_verify = False
        job.login_verified = False
        job.retry_count = 0
        job.started_at = None
        job.finished_at = None
        try:
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id,
                    "queued",
                    secret=job.secret,
                    password=job.password,
                    account_check=self._state(job),
                ),
                "queued",
            )
        except JobPersistenceError:
            (
                job.mode,
                job.status,
                job.error,
                job.error_kind,
                job.usage_enabled,
                job.payment_methods_enabled,
                job.rotated_pending_verify,
                job.login_verified,
                job.retry_count,
                job.started_at,
                job.finished_at,
            ) = previous
            raise
        self._reconcile_workers()
        self._queue.put_nowait(job.id)
        self._broadcast(job)
        return job.snapshot()

    async def refresh_usage(self, job_id: str) -> dict[str, Any]:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        self.assert_not_logging_out(job.email)
        if not job.usage_enabled:
            raise ValueError("Đọc Usage đã tắt cho job này")
        if job.status != "success" or job.account_state != "live":
            raise ValueError("Chỉ đọc lại Usage cho tài khoản Live đã check thành công")
        if job.usage is not None:
            raise ValueError("Tài khoản đã có dữ liệu Usage")
        if job.usage_refreshing:
            raise ValueError("Usage của tài khoản đang được đọc lại")
        if job.chat_deleting:
            raise ValueError("Dữ liệu chat của tài khoản đang được xóa")

        job.usage_refreshing = True
        self._broadcast(job)
        try:
            usage = await self.service.refresh_usage(
                email=job.email,
                password=job.password,
                secret=job.secret,
                timeout=float(self.settings["twofa.job_timeout"]),
                log=lambda message: self._append_job_log(job, message),
            )
            job.usage = usage
            try:
                self._persist(
                    lambda: self.job_repo.update_status(
                        job.id,
                        job.status,
                        account_check=self._state(job),
                    ),
                    "usage",
                )
            except JobPersistenceError:
                job.usage = None
                raise
        finally:
            job.usage_refreshing = False
            self._broadcast(job)
        return job.snapshot()

    def assert_not_logging_out(self, email: str) -> None:
        # Keep the existing guard entry point used by both local workspaces.
        if any(job.passkey_preparing and job.email.strip().casefold() == email.strip().casefold()
               for job in self.jobs.values()):
            raise ValueError("Tài khoản đang chuẩn bị passkey; vui lòng chờ")
        if any(job.sessions_logging_out and job.email.strip().casefold() == email.strip().casefold()
               for job in self.jobs.values()):
            raise ValueError("Tài khoản đang logout all sessions; vui lòng chờ")

    async def prepare_passkey(self, job_id: str) -> str:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        if self._clear_in_progress or job.status != "success" or job.account_state != "live" or not job.login_verified:
            raise ValueError("Chỉ thêm passkey cho tài khoản Live đã xác minh, không đang dọn danh sách")
        self.assert_not_logging_out(job.email)
        if any(other.email.strip().casefold() == job.email.strip().casefold() and (
            other.status not in TERMINAL or other.usage_refreshing or other.chat_deleting
        ) for other in self.jobs.values()):
            raise ValueError("Tài khoản còn thao tác đang chạy; vui lòng chờ trước khi thêm passkey")
        job.passkey_preparing = True
        self._broadcast(job)
        try:
            return await self.service.prepare_passkey(
                email=job.email, password=job.password, secret=job.secret,
                timeout=float(self.settings["twofa.job_timeout"]),
            )
        finally:
            job.passkey_preparing = False
            self._broadcast(job)

    async def logout_all_sessions(self, job_id: str) -> dict[str, Any]:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        if self._clear_in_progress:
            raise ValueError("Đang dọn danh sách; vui lòng chờ")
        if job.status != "success" or job.account_state != "live":
            raise ValueError("Chỉ logout cho tài khoản Live đã check thành công")
        self.assert_not_logging_out(job.email)
        if any(other.email.strip().casefold() == job.email.strip().casefold() and (
            other.status not in TERMINAL or other.usage_refreshing or other.chat_deleting
        ) for other in self.jobs.values()):
            raise ValueError("Tài khoản còn thao tác đang chạy; vui lòng chờ trước khi logout")
        job.sessions_logging_out = True
        self._broadcast(job)
        try:
            await self.service.logout_all_sessions(
                email=job.email, password=job.password, secret=job.secret,
                timeout=float(self.settings["twofa.job_timeout"]), log=lambda _message: None,
            )
        finally:
            job.sessions_logging_out = False
            self._broadcast(job)
        return job.snapshot()

    async def delete_all_chats(self, job_id: str) -> dict[str, Any]:
        """Run one confirmed destructive account action without persisting it."""
        self._assert_event_loop_thread()
        job = self._require(job_id)
        self.assert_not_logging_out(job.email)
        if job.status != "success" or job.account_state != "live":
            raise ValueError("Chỉ xóa chat cho tài khoản Live đã check thành công")
        if job.usage_refreshing:
            raise ValueError("Usage của tài khoản đang được đọc lại")
        if job.chat_deleting:
            raise ValueError("Dữ liệu chat của tài khoản đang được xóa")

        job.chat_deleting = True
        self._broadcast(job)
        try:
            await self.service.delete_all_chats(
                email=job.email,
                password=job.password,
                secret=job.secret,
                timeout=float(self.settings["twofa.job_timeout"]),
                log=lambda _message: None,
            )
        finally:
            job.chat_deleting = False
            self._broadcast(job)
        return job.snapshot()

    def stop(self, job_id: str) -> dict[str, Any]:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
        elif job.status == "queued":
            previous = (job.status, job.error, job.error_kind, job.finished_at)
            job.status = "cancelled"
            job.error = "Đã dừng bởi người dùng"
            job.error_kind = None
            job.finished_at = time.time()
            try:
                self._persist(
                    lambda: self.job_repo.update_status(
                        job.id,
                        "cancelled",
                        error=job.error,
                        account_check=self._state(job),
                    ),
                    "cancelled",
                )
            except JobPersistenceError:
                job.status, job.error, job.error_kind, job.finished_at = previous
                raise
            self._broadcast(job)
        return job.snapshot()

    def stop_all(self) -> None:
        self._assert_event_loop_thread()
        for job in list(self.jobs.values()):
            if job.status in {"queued", "running"}:
                self.stop(job.id)

    def delete(self, job_id: str) -> None:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        self.assert_not_logging_out(job.email)
        if job.usage_refreshing:
            raise ValueError("Không thể xóa khi đang đọc lại Usage")
        if job.chat_deleting:
            raise ValueError("Không thể xóa job khi đang xóa dữ liệu chat")
        if job.status not in TERMINAL:
            raise ValueError("Không thể xóa job đang chạy")
        self.job_repo.delete(job_id)
        self.jobs.pop(job_id, None)
        self.order = [item for item in self.order if item != job_id]
        self._broadcast_raw({"type": "removed", "id": job_id})

    @staticmethod
    def _is_clearable_failure(job: TwoFAJob) -> bool:
        return job.status in {"error", "cancelled"} and job.account_state != "live"

    def clear_failed(self) -> int:
        self._assert_event_loop_thread()
        removable_ids = [
            job_id
            for job_id in self.order
            if (job := self.jobs.get(job_id)) is not None
            and self._is_clearable_failure(job)
        ]
        for job_id in removable_ids:
            self.delete(job_id)
        return len(removable_ids)

    def clear(self) -> int:
        self._assert_event_loop_thread()
        if any(
            job.status not in TERMINAL or job.usage_refreshing or job.chat_deleting or job.sessions_logging_out or job.passkey_preparing
            for job in self.jobs.values()
        ):
            raise ValueError("Hãy dừng toàn bộ job trước khi dọn danh sách")
        count = self.job_repo.delete_all(JOB_TYPE)
        self.jobs.clear()
        self.order.clear()
        self._broadcast_raw({"type": "snapshot", "jobs": []})
        return count

    async def clear_async(self) -> int:
        """Clear Community jobs without blocking FastAPI's event loop.

        SQLite cleanup can cascade through a large ``job_logs`` table. Keep
        manager state and SSE publication on the application loop, but run the
        blocking repository transaction in a worker thread so health/SSE and
        the password workspace remain responsive while cleanup is in progress.
        """
        self._assert_event_loop_thread()
        async with self._clear_lock:
            if self._clear_in_progress:
                raise ValueError("Đang dọn danh sách; vui lòng thử lại sau")
            if any(
                job.status not in TERMINAL or job.usage_refreshing or job.chat_deleting or job.sessions_logging_out or job.passkey_preparing
                for job in self.jobs.values()
            ):
                raise ValueError("Hãy dừng toàn bộ job trước khi dọn danh sách")
            if not self.jobs:
                return 0

            self._clear_in_progress = True
            delete_task = asyncio.create_task(
                asyncio.to_thread(self.job_repo.delete_all, JOB_TYPE)
            )
            try:
                count = await asyncio.shield(delete_task)
            except asyncio.CancelledError:
                # Do not leave memory and SQLite out of sync if the HTTP task
                # is cancelled while the non-cancellable thread is finishing.
                count = await delete_task
                self.jobs.clear()
                self.order.clear()
                self._broadcast_raw({"type": "snapshot", "jobs": []})
                raise
            finally:
                self._clear_in_progress = False

            self.jobs.clear()
            self.order.clear()
            self._broadcast_raw({"type": "snapshot", "jobs": []})
            return int(count)

    def output(self) -> list[str]:
        completed = sorted(
            (
                job
                for job in self.jobs.values()
                if (
                    job.mode == "change_2fa"
                    and job.status == "success"
                    and job.login_verified
                )
            ),
            key=lambda job: (
                float(job.finished_at) if job.finished_at is not None else float("inf"),
                float(job.created_at),
                job.id,
            ),
        )
        return [
            "|".join((job.email, job.password, job.secret))
            for job in completed
        ]

    def twofa_history(self) -> list[dict[str, Any]]:
        """Return sensitive raw history only for the protected history API."""
        list_history = getattr(self.job_repo, "list_twofa_history", None)
        if not callable(list_history):
            return []
        return [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "email": row["email"],
                "changed_at": row["changed_at"],
                "raw": "|".join((row["email"], row["password"], row["secret"])),
            }
            for row in list_history()
        ]

    @staticmethod
    def _matches_export_filter(job: TwoFAJob, view: str) -> bool:
        if view == "all":
            return True
        if view == "running":
            return job.status in {"queued", "running"}
        if view == "success":
            return job.status == "success"
        if view == "error":
            return job.status in {"error", "cancelled"}
        if view == "usage-low":
            usage = job.usage if isinstance(job.usage, dict) else {}
            used = usage.get("used_percent")
            return (
                isinstance(used, (int, float))
                and not isinstance(used, bool)
                and 0 <= float(used) < 50
            )
        if view == "usage-full":
            usage = job.usage if isinstance(job.usage, dict) else {}
            used = usage.get("used_percent")
            return (
                isinstance(used, (int, float))
                and not isinstance(used, bool)
                and float(used) == 100
            )
        if view == "free-no-usage":
            if not job.usage_enabled:
                return False
            usage = job.usage if isinstance(job.usage, dict) else {}
            used = usage.get("used_percent")
            has_valid_usage = (
                isinstance(used, (int, float))
                and not isinstance(used, bool)
                and 0 <= float(used) <= 100
            )
            return (
                str(job.plan or "").casefold() in {"free", "plus"}
                and job.status == "success"
                and job.account_state == "live"
                and not has_valid_usage
            )
        return False

    def export_filtered(self, view: str) -> list[str]:
        if view not in EXPORT_FILTERS:
            raise ValueError("Bộ lọc xuất dữ liệu không hợp lệ")
        return [
            "|".join((job.email, job.password, job.secret))
            for job_id in self.order
            if (job := self.jobs.get(job_id)) is not None
            and self._matches_export_filter(job, view)
        ]

    def raw_combo(self, job_id: str) -> str:
        job = self._require(job_id)
        return "|".join((job.email, job.password, job.secret))

    def live_journal_entries(self) -> list[dict[str, Any]]:
        """Capture current verified Live rows without enqueueing any account work."""
        entries = []
        for job_id in self.order:
            job = self.jobs[job_id]
            if not (job.status == "success" and job.account_state == "live"
                    and job.login_verified and not job.rotated_pending_verify):
                continue
            snapshot = copy.deepcopy(job.snapshot())
            # A journal is a past observation, not a log or an in-flight action.
            snapshot.pop("log_tail", None)
            for key in ("usage_refreshing", "chat_deleting", "sessions_logging_out", "passkey_preparing"):
                snapshot[key] = False
            entries.append({"email": job.email, "password": job.password, "secret": job.secret, "snapshot": snapshot})
        return entries

    def snapshots(self) -> list[dict[str, Any]]:
        return [self.jobs[job_id].snapshot() for job_id in self.order if job_id in self.jobs]

    def logs(self, job_id: str) -> list[str]:
        return list(self._require(job_id).logs)

    async def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        self._assert_event_loop_thread()
        previous = int(self.settings["twofa.max_concurrent"])
        for key, value in values.items():
            if key not in self.DEFAULTS:
                raise ValueError(f"Setting không hỗ trợ: {key}")
            self.settings_repo.set(key, value)
            self.settings[key] = value
        target = int(self.settings["twofa.max_concurrent"])
        if target != previous:
            await self._resize_workers(previous, target)
        self._broadcast_health()
        return dict(self.settings)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _broadcast(self, job: TwoFAJob) -> None:
        self._broadcast_raw({"type": "job", "job": job.snapshot()})

    def _broadcast_health(self) -> None:
        self._broadcast_raw({"type": "worker_health", "worker_health": self.worker_health()})

    def _broadcast_raw(self, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(payload)

    def _require(self, job_id: str) -> TwoFAJob:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job
