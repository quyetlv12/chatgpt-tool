"""Independent queue and persistence lifecycle for password-change jobs."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from password_service import PasswordChangeError, PasswordService


JOB_TYPE = "password_community"
TERMINAL = {"success", "error", "cancelled"}


class PasswordJobPersistenceError(RuntimeError):
    """Credential-safe wrapper for local repository failures."""


@dataclass(slots=True)
class PasswordJob:
    id: str
    email: str
    password: str
    secret: str
    pending_password: str | None
    status: str = "queued"
    phase: str = "queued"
    error: str | None = None
    error_kind: str | None = None
    mutation_started: bool = False
    login_verified: bool = False
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    logs: list[str] = field(default_factory=list)

    @property
    def retryable(self) -> bool:
        return self.error_kind not in {"invalid_credentials", "password_uncertain"}

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "status": self.status,
            "phase": self.phase,
            "error": self.error,
            "error_kind": self.error_kind,
            "mutation_started": self.mutation_started,
            "login_verified": self.login_verified,
            "retryable": self.retryable,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_tail": self.logs[-3:],
        }


class PasswordJobManager:
    """Own queue, recovery, output, history, and SSE for password jobs only."""

    MAX_CONCURRENT = 3
    JOB_TIMEOUT = 180.0

    def __init__(self, job_repo: Any, settings_repo: Any, service: PasswordService | None = None) -> None:
        self.job_repo = job_repo
        self.settings_repo = settings_repo
        self.service = service or PasswordService()
        self.jobs: dict[str, PasswordJob] = {}
        self.order: list[str] = []
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._clear_lock = asyncio.Lock()
        self._clear_in_progress = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._stopping = False
        self._persistence_failures = 0
        self._last_persistence_error: str | None = None
        self._recover()

    @staticmethod
    def parse_combo(line: str) -> tuple[str, str, str]:
        parts = [part.strip() for part in line.strip().split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError("Định dạng phải là email|password|2FA_hiện_tại")
        email, password, secret = parts
        if "@" not in email:
            raise ValueError("Email không hợp lệ")
        return email.casefold(), password, secret.replace(" ", "").upper()

    @staticmethod
    def _validate_target(value: Any) -> str:
        if not isinstance(value, str) or not (12 <= len(value) <= 128):
            raise ValueError("Chưa cấu hình mật khẩu chung hợp lệ trong Settings")
        if "|" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Mật khẩu chung chứa ký tự không được hỗ trợ")
        return value

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

    def _state(self, job: PasswordJob) -> str:
        return json.dumps({
            "phase": job.phase,
            "error_kind": job.error_kind,
            "mutation_started": job.mutation_started,
            "login_verified": job.login_verified,
            "retry_count": job.retry_count,
        }, ensure_ascii=False)

    def _recover(self) -> None:
        for row in self.job_repo.list_all():
            if row.get("job_type") != JOB_TYPE:
                continue
            state = self._decode_state(row.get("account_check"))
            status = str(row.get("status") or "error")
            if status in {"queued", "running"}:
                status = "queued"
            job = PasswordJob(
                id=str(row["id"]),
                email=str(row["email"]),
                password=str(row.get("password") or ""),
                secret=str(row.get("secret") or ""),
                pending_password=(
                    str(row.get("pending_password"))
                    if row.get("pending_password") is not None
                    else None
                ),
                status=status,
                phase=str(state.get("phase") or ("verified" if status == "success" else "queued")),
                error=row.get("error"),
                error_kind=state.get("error_kind"),
                mutation_started=bool(state.get("mutation_started")),
                login_verified=bool(state.get("login_verified")),
                retry_count=int(state.get("retry_count") or 0),
                created_at=float(row.get("created_at") or time.time()),
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
                logs=[str(item.get("line") or "") for item in self.job_repo.get_logs(str(row["id"]))],
            )
            self.jobs[job.id] = job
            self.order.append(job.id)

    def _assert_event_loop_thread(self) -> None:
        if not self._started:
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is not self._loop:
            raise RuntimeError("Password manager mutation phải chạy trên event-loop thread")

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._started:
            if loop is not self._loop:
                raise RuntimeError("Password manager đã chạy trên event loop khác")
            return
        self._loop = loop
        self._started = True
        self._stopping = False
        self._workers = []
        for _ in range(self.MAX_CONCURRENT):
            self._spawn_worker()
        for job in self.jobs.values():
            if job.status == "queued":
                self._queue.put_nowait(job.id)
        self._broadcast_health()

    def _spawn_worker(self) -> None:
        task = asyncio.create_task(self._worker())
        task.add_done_callback(self._worker_done)
        self._workers.append(task)

    def _worker_done(self, task: asyncio.Task) -> None:
        if task in self._workers:
            self._workers.remove(task)
        if task.cancelled():
            error = None
        else:
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = None
        if error is not None:
            # Deliberately retain only the type. Repository exceptions may wrap
            # SQL values, which must not be broadcast to the dashboard.
            self._broadcast_raw({"type": "worker_error", "error": type(error).__name__})
        if self._started and not self._stopping:
            self._spawn_worker()
            self._broadcast_health()

    async def shutdown(self) -> None:
        self._stopping = True
        self._started = False
        tasks = list(self._tasks.values()) + list(self._workers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._workers.clear()
        self._stopping = False

    def worker_health(self) -> dict[str, Any]:
        active = sum(not task.done() for task in self._workers)
        return {
            "started": self._started,
            "configured": self.MAX_CONCURRENT,
            "active": active,
            "busy": sum(not task.done() for task in self._tasks.values()),
            "queued": sum(job.status == "queued" for job in self.jobs.values()),
            "persistence_failures": self._persistence_failures,
            "last_persistence_error": self._last_persistence_error,
            "degraded": self._started and active < self.MAX_CONCURRENT,
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
            raise PasswordJobPersistenceError(
                f"Không lưu được trạng thái {status} vào cơ sở dữ liệu"
            ) from exc

    def _persist_terminal(self, job: PasswordJob, status: str) -> bool:
        try:
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id,
                    status,
                    error=job.error,
                    password=job.password,
                    pending_password=job.pending_password,
                    secret=job.secret,
                    account_check=self._state(job),
                ),
                status,
            )
            return True
        except PasswordJobPersistenceError as exc:
            preserved_kind = job.error_kind
            job.status = "error"
            job.error = str(exc)
            if preserved_kind not in {
                "invalid_credentials",
                "password_not_applied",
                "password_rejected",
                "password_uncertain",
            }:
                job.error_kind = "technical_error"
            job.finished_at = time.time()
            try:
                self._persist(
                    lambda: self.job_repo.update_status(
                        job.id,
                        "error",
                        error=job.error,
                        password=job.password,
                        pending_password=job.pending_password,
                        secret=job.secret,
                        account_check=self._state(job),
                    ),
                    "error fallback",
                )
            except PasswordJobPersistenceError:
                pass
            return False

    def add(self, lines: list[str]) -> list[dict[str, Any]]:
        self._assert_event_loop_thread()
        if self._clear_in_progress:
            raise ValueError("Đang dọn danh sách password; vui lòng thử lại sau")
        # Snapshot once for the whole enqueue operation. Later Settings edits only
        # affect later jobs and never mutate a credential already in progress.
        target = self._validate_target(
            self.settings_repo.get("password_change.target_password")
        )
        parsed: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for line in lines:
            email, password, secret = self.parse_combo(line)
            if email in seen:
                continue
            if password == target:
                raise ValueError(f"{email}: mật khẩu hiện tại đã trùng mật khẩu chung")
            seen.add(email)
            parsed.append((email, password, secret))

        created: list[dict[str, Any]] = []
        for email, password, secret in parsed:
            job = PasswordJob(
                id=uuid.uuid4().hex,
                email=email,
                password=password,
                secret=secret,
                pending_password=target,
            )
            self._persist(
                lambda: self.job_repo.create({
                    "id": job.id,
                    "email": email,
                    "combo": "[redacted]",
                    "mail_mode": "none",
                    "status": "queued",
                    "password": password,
                    "pending_password": target,
                    "secret": secret,
                    "account_check": self._state(job),
                    "created_at": job.created_at,
                    "job_type": JOB_TYPE,
                }),
                "queued",
            )
            self.jobs[job.id] = job
            self.order.append(job.id)
            self._queue.put_nowait(job.id)
            created.append(job.snapshot())
            self._broadcast(job)
        return created

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                job = self.jobs.get(job_id)
                if job and job.status == "queued":
                    task = asyncio.create_task(self._run(job))
                    self._tasks[job.id] = task
                    await task
            finally:
                self._tasks.pop(job_id, None)
                self._queue.task_done()

    def _append_log(self, job: PasswordJob, message: str) -> None:
        safe = str(message)
        for sensitive in (job.password, job.pending_password, job.secret):
            if sensitive:
                safe = safe.replace(sensitive, "***")
        stamped = f"{time.strftime('%H:%M:%S')}  {safe[:500]}"
        job.logs.append(stamped)
        job.logs[:] = job.logs[-300:]
        try:
            self._persist(
                lambda: self.job_repo.append_log(job.id, stamped),
                "log",
            )
        except PasswordJobPersistenceError:
            # Logs are diagnostic. A log write must never change whether an
            # account mutation proceeds or how its result is reconciled.
            pass
        self._broadcast(job)

    async def _run(self, job: PasswordJob) -> None:
        async def checkpoint(phase: str) -> None:
            previous = (job.phase, job.mutation_started, job.login_verified)
            job.phase = phase
            job.mutation_started = True
            job.login_verified = False
            try:
                self._persist(
                    lambda: self.job_repo.update_status(
                        job.id,
                        "running",
                        password=job.password,
                        pending_password=job.pending_password,
                        secret=job.secret,
                        account_check=self._state(job),
                    ),
                    "mutation checkpoint",
                )
            except PasswordJobPersistenceError:
                job.phase, job.mutation_started, job.login_verified = previous
                raise
            self._broadcast(job)

        try:
            if not job.pending_password:
                raise PasswordChangeError(
                    "Job không còn mật khẩu đích để xử lý",
                    error_kind="technical_error",
                )

            job.status = "running"
            job.phase = "recovery" if job.mutation_started else "authenticating"
            job.error = None
            job.error_kind = None
            job.started_at = time.time()
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id,
                    "running",
                    password=job.password,
                    pending_password=job.pending_password,
                    secret=job.secret,
                    account_check=self._state(job),
                ),
                "running",
            )
            self._broadcast(job)

            result = await self.service.change(
                email=job.email,
                current_password=job.password,
                target_password=job.pending_password,
                secret=job.secret,
                timeout=self.JOB_TIMEOUT,
                mutation_started=job.mutation_started,
                checkpoint=checkpoint,
                log=lambda line: self._append_log(job, line),
            )
            if not result.login_verified:
                raise PasswordChangeError(
                    "Mật khẩu mới chưa được xác minh bằng phiên đăng nhập mới",
                    error_kind="password_uncertain",
                )
            job.login_verified = result.login_verified
            job.phase = "verified"
            job.status = "success"
            job.error = None
            job.error_kind = None
            job.finished_at = time.time()
            complete = getattr(self.job_repo, "complete_password_success", None)
            target = job.pending_password
            if callable(complete):
                self._persist(
                    lambda: complete(
                        job_id=job.id,
                        account_check=self._state(job),
                        changed_at=float(job.finished_at),
                    ),
                    "success + password history",
                )
            else:
                self._persist(
                    lambda: self.job_repo.update_status(
                        job.id,
                        "success",
                        password=target,
                        pending_password=None,
                        secret=job.secret,
                        account_check=self._state(job),
                    ),
                    "success",
                )
            job.password = target
            job.pending_password = None
        except asyncio.CancelledError:
            job.finished_at = time.time()
            if job.mutation_started:
                job.status = "error"
                job.phase = "uncertain"
                job.error_kind = "password_uncertain"
                job.error = "Đã dừng sau mutation checkpoint; cần đối soát, không tự gửi lại"
                self._persist_terminal(job, "error")
            else:
                job.status = "cancelled"
                job.phase = "cancelled"
                job.error = "Đã dừng bởi người dùng"
                self._persist_terminal(job, "cancelled")
        except Exception as exc:
            job.status = "error"
            job.error_kind = str(getattr(exc, "error_kind", "technical_error"))
            job.phase = (
                "uncertain" if job.error_kind == "password_uncertain"
                else "not_applied" if job.error_kind == "password_not_applied"
                else "error"
            )
            job.error = (str(exc).strip() or type(exc).__name__)[:240]
            job.finished_at = time.time()
            self._persist_terminal(job, "error")
        finally:
            self._broadcast(job)

    def retry(self, job_id: str) -> dict[str, Any]:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        if job.status not in TERMINAL:
            raise ValueError("Job đang chạy hoặc đang chờ")
        if not job.retryable:
            raise ValueError("Không thể tự retry khi trạng thái mật khẩu chưa chắc chắn")
        if not job.pending_password:
            raise ValueError("Job đã hoàn tất và không còn mật khẩu đích")
        previous = (
            job.retry_count,
            job.status,
            job.phase,
            job.error,
            job.error_kind,
            job.started_at,
            job.finished_at,
            job.mutation_started,
        )
        # A human retry after a definitive rejection/not-applied result is the
        # only action allowed to authorize another mutation POST.
        if job.error_kind in {"password_not_applied", "password_rejected"}:
            job.mutation_started = False
        job.retry_count += 1
        job.status = "queued"
        job.phase = "queued"
        job.error = None
        job.error_kind = None
        job.started_at = None
        job.finished_at = None
        try:
            self._persist(
                lambda: self.job_repo.update_status(
                    job.id,
                    "queued",
                    password=job.password,
                    pending_password=job.pending_password,
                    secret=job.secret,
                    account_check=self._state(job),
                ),
                "queued retry",
            )
        except PasswordJobPersistenceError:
            (
                job.retry_count,
                job.status,
                job.phase,
                job.error,
                job.error_kind,
                job.started_at,
                job.finished_at,
                job.mutation_started,
            ) = previous
            raise
        self._queue.put_nowait(job.id)
        self._broadcast(job)
        return job.snapshot()

    def stop(self, job_id: str) -> dict[str, Any]:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
        elif job.status == "queued":
            previous = (job.status, job.phase, job.error, job.finished_at)
            job.status = "cancelled"
            job.phase = "cancelled"
            job.error = "Đã dừng bởi người dùng"
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
            except PasswordJobPersistenceError:
                job.status, job.phase, job.error, job.finished_at = previous
                raise
            self._broadcast(job)
        return job.snapshot()

    def delete(self, job_id: str) -> None:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        if job.status not in TERMINAL:
            raise ValueError("Không thể xóa job đang chạy")
        self._persist(lambda: self.job_repo.delete(job_id), "deleted")
        self.jobs.pop(job_id, None)
        self.order = [item for item in self.order if item != job_id]
        self._broadcast_raw({"type": "removed", "id": job_id})

    def clear(self) -> int:
        self._assert_event_loop_thread()
        if any(job.status not in TERMINAL for job in self.jobs.values()):
            raise ValueError("Hãy dừng toàn bộ password job trước khi dọn")
        count = self._persist(
            lambda: self.job_repo.delete_all(JOB_TYPE),
            "cleared",
        )
        self.jobs.clear()
        self.order.clear()
        self._broadcast_raw({"type": "snapshot", "jobs": []})
        return count

    async def clear_async(self) -> int:
        """Clear password jobs without blocking the FastAPI event loop."""
        self._assert_event_loop_thread()
        async with self._clear_lock:
            if self._clear_in_progress:
                raise ValueError("Đang dọn danh sách password; vui lòng thử lại sau")
            if any(job.status not in TERMINAL for job in self.jobs.values()):
                raise ValueError("Hãy dừng toàn bộ password job trước khi dọn")
            if not self.jobs:
                return 0

            self._clear_in_progress = True
            delete_task = asyncio.create_task(
                asyncio.to_thread(self.job_repo.delete_all, JOB_TYPE)
            )
            try:
                try:
                    count = await asyncio.shield(delete_task)
                except Exception as exc:
                    self._record_persistence_failure(exc)
                    raise PasswordJobPersistenceError(
                        "Không lưu được trạng thái cleared vào cơ sở dữ liệu"
                    ) from exc
            except asyncio.CancelledError:
                # Finish the SQLite operation before propagating cancellation;
                # otherwise the next bootstrap could resurrect stale snapshots.
                try:
                    await delete_task
                finally:
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
                job for job in self.jobs.values()
                if job.status == "success" and job.login_verified
            ),
            key=lambda job: (float(job.finished_at or float("inf")), job.id),
        )
        return ["|".join((job.email, job.password, job.secret)) for job in completed]

    def history(self) -> list[dict[str, Any]]:
        list_history = getattr(self.job_repo, "list_password_history", None)
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

    def snapshots(self) -> list[dict[str, Any]]:
        return [self.jobs[job_id].snapshot() for job_id in self.order if job_id in self.jobs]

    def logs(self, job_id: str) -> list[str]:
        return list(self._require(job_id).logs)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _broadcast(self, job: PasswordJob) -> None:
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

    def _require(self, job_id: str) -> PasswordJob:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job
