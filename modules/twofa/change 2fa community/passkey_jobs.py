"""Ephemeral concurrent queue for explicit bulk passkey handoffs.

Passkey jobs deliberately stay in RAM.  They retain the input credentials only
long enough to re-authenticate and prepare an official OpenAI handoff; no
private key, handoff URL, or job checkpoint is persisted for replay.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from service import TwoFAService


TERMINAL = {"success", "error", "cancelled"}


@dataclass(slots=True)
class PasskeyJob:
    id: str
    email: str
    password: str
    secret: str
    window_index: int = 1
    window_total: int = 1
    status: str = "queued"
    phase: str = "queued"
    error: str | None = None
    error_kind: str | None = None
    handoff_url: str | None = None
    handoff_issued: bool = False
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    logs: list[str] = field(default_factory=list)

    @property
    def retryable(self) -> bool:
        return self.error_kind not in {"invalid_credentials", "account_die"}

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "window_index": self.window_index,
            "window_total": self.window_total,
            "status": self.status,
            "phase": self.phase,
            "error": self.error,
            "error_kind": self.error_kind,
            "handoff_ready": bool(self.handoff_url) and not self.handoff_issued,
            "handoff_issued": self.handoff_issued,
            "retryable": self.retryable,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_tail": self.logs[-3:],
        }


class PasskeyJobManager:
    """Independent, non-persistent worker queue for bulk passkey preparation."""

    MAX_CONCURRENT = 3
    JOB_TIMEOUT = 180.0

    def __init__(
        self,
        service: TwoFAService | None = None,
        *,
        max_concurrent: int = MAX_CONCURRENT,
        job_timeout: float = JOB_TIMEOUT,
    ) -> None:
        self.service = service or TwoFAService()
        self.max_concurrent = max(1, min(int(max_concurrent), 10))
        self.job_timeout = max(30.0, min(float(job_timeout), 600.0))
        self.jobs: dict[str, PasskeyJob] = {}
        self.order: list[str] = []
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._stopping = False
        self._clear_in_progress = False

    @staticmethod
    def parse_combo(line: str) -> tuple[str, str, str]:
        parts = [part.strip() for part in line.strip().split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError("Định dạng phải là email|password|2FA_hiện_tại")
        email, password, secret = parts
        if "@" not in email:
            raise ValueError("Email không hợp lệ")
        return email.casefold(), password, secret.replace(" ", "").upper()

    def _assert_event_loop_thread(self) -> None:
        if not self._started:
            return
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Passkey manager phải chạy trên event-loop thread")

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._started:
            if loop is not self._loop:
                raise RuntimeError("Passkey manager đã chạy trên event loop khác")
            return
        self._loop = loop
        self._started = True
        self._stopping = False
        self._spawn_workers(self.max_concurrent)

    def _spawn_workers(self, target: int) -> None:
        self._workers[:] = [task for task in self._workers if not task.done()]
        while len(self._workers) < target:
            task = asyncio.create_task(self._worker())
            task.add_done_callback(self._worker_done)
            self._workers.append(task)

    def _worker_done(self, task: asyncio.Task) -> None:
        if task in self._workers:
            self._workers.remove(task)
        if self._started and not self._stopping:
            self._spawn_workers(self.max_concurrent)
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
        return {
            "started": self._started,
            "configured": self.max_concurrent,
            "active": sum(not task.done() for task in self._workers),
            "busy": sum(not task.done() for task in self._tasks.values()),
            "queued": sum(job.status == "queued" for job in self.jobs.values()),
        }

    def configure(self, *, max_concurrent: int | None = None, job_timeout: float | None = None) -> None:
        self._assert_event_loop_thread()
        if max_concurrent is not None:
            previous = self.max_concurrent
            self.max_concurrent = max(1, min(int(max_concurrent), 10))
            if self._started:
                self._spawn_workers(self.max_concurrent)
                for _ in range(max(0, previous - self.max_concurrent)):
                    self._queue.put_nowait(None)
        if job_timeout is not None:
            self.job_timeout = max(30.0, min(float(job_timeout), 600.0))
        self._broadcast_health()

    def is_busy(self, email: str) -> bool:
        normalized = email.strip().casefold()
        return any(
            job.email.casefold() == normalized
            and job.status not in TERMINAL
            for job in self.jobs.values()
        )

    def has_job(self, email: str) -> bool:
        normalized = email.strip().casefold()
        return any(job.email.casefold() == normalized for job in self.jobs.values())

    def add(self, lines: list[str]) -> list[dict[str, Any]]:
        self._assert_event_loop_thread()
        if self._clear_in_progress:
            raise ValueError("Đang dọn danh sách passkey; vui lòng thử lại sau")
        parsed: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for line in lines:
            email, password, secret = self.parse_combo(line)
            if email in seen:
                continue
            if self.is_busy(email):
                raise ValueError(f"{email}: đang chuẩn bị passkey; vui lòng chờ")
            seen.add(email)
            parsed.append((email, password, secret))

        created: list[dict[str, Any]] = []
        window_total = len(parsed)
        for window_index, (email, password, secret) in enumerate(parsed, start=1):
            job = PasskeyJob(
                id=uuid.uuid4().hex,
                email=email,
                password=password,
                secret=secret,
                window_index=window_index,
                window_total=window_total,
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
                if job_id is None:
                    return
                job = self.jobs.get(job_id)
                if job and job.status == "queued":
                    task = asyncio.create_task(self._run(job))
                    self._tasks[job.id] = task
                    await task
            finally:
                self._tasks.pop(job_id, None)
                self._queue.task_done()

    def _append_log(self, job: PasskeyJob, message: str) -> None:
        stamped = f"{time.strftime('%H:%M:%S')}  {str(message)[:500]}"
        job.logs.append(stamped)
        job.logs[:] = job.logs[-100:]
        self._broadcast(job)

    def _safe_service_log(self, job: PasskeyJob, message: str) -> None:
        """Keep login diagnostics to fixed, credential-free milestones."""
        if str(message).startswith(("[auth]", "[login]")):
            self._append_log(job, "[auth] Đăng nhập tạm thời chưa thành công; đang thử lại...")

    async def _run(self, job: PasskeyJob) -> None:
        try:
            job.status = "running"
            job.phase = "authenticating"
            job.error = None
            job.error_kind = None
            job.started_at = time.time()
            self._append_log(job, "[1/2] Đang đăng nhập lại tài khoản...")
            url = await self.service.prepare_passkey(
                email=job.email,
                password=job.password,
                secret=job.secret,
                timeout=self.job_timeout,
                log=lambda message: self._safe_service_log(job, message),
            )
            job.handoff_url = url
            job.phase = "handoff_ready"
            job.status = "success"
            job.finished_at = time.time()
            self._append_log(job, "[2/2] Đã chuẩn bị handoff; chờ mở tab OpenAI.")
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.phase = "cancelled"
            job.error = "Đã dừng bởi người dùng"
            job.finished_at = time.time()
        except Exception as exc:
            job.status = "error"
            job.phase = "error"
            job.error_kind = str(getattr(exc, "error_kind", "technical_error"))
            job.error = (str(exc).strip() or type(exc).__name__)[:240]
            job.finished_at = time.time()
            self._append_log(job, "[error] Chuẩn bị handoff thất bại.")
        finally:
            self._broadcast(job)

    def issue_handoff(self, job_id: str, handoffs: Any) -> str:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        if job.status != "success" or not job.handoff_url:
            raise ValueError("Job chưa sẵn sàng mở passkey")
        if job.handoff_issued:
            raise ValueError("Handoff passkey đã được mở hoặc đã dùng")
        token = handoffs.issue(job.handoff_url)
        job.handoff_issued = True
        job.handoff_url = None
        self._broadcast(job)
        return token

    def retry(self, job_id: str) -> dict[str, Any]:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        if job.status not in TERMINAL or not job.retryable:
            raise ValueError("Job không thể chạy lại")
        if self.is_busy(job.email):
            raise ValueError("Tài khoản đang chuẩn bị passkey ở job khác; vui lòng chờ")
        job.status = "queued"
        job.phase = "queued"
        job.error = None
        job.error_kind = None
        job.handoff_url = None
        job.handoff_issued = False
        job.retry_count += 1
        job.started_at = None
        job.finished_at = None
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
            job.status = "cancelled"
            job.phase = "cancelled"
            job.error = "Đã dừng bởi người dùng"
            job.finished_at = time.time()
            self._broadcast(job)
        return job.snapshot()

    def delete(self, job_id: str) -> None:
        self._assert_event_loop_thread()
        job = self._require(job_id)
        if job.status not in TERMINAL:
            raise ValueError("Không thể xóa job đang chạy")
        self.jobs.pop(job_id, None)
        self.order = [item for item in self.order if item != job_id]
        self._broadcast_raw({"type": "removed", "id": job_id})

    async def clear_async(self) -> int:
        self._assert_event_loop_thread()
        if self._clear_in_progress:
            raise ValueError("Đang dọn danh sách passkey; vui lòng thử lại sau")
        if any(job.status not in TERMINAL for job in self.jobs.values()):
            raise ValueError("Hãy dừng toàn bộ passkey job trước khi dọn")
        self._clear_in_progress = True
        try:
            count = len(self.jobs)
            self.jobs.clear()
            self.order.clear()
            self._broadcast_raw({"type": "snapshot", "jobs": []})
            return count
        finally:
            self._clear_in_progress = False

    def output(self) -> list[str]:
        completed = sorted(
            (job for job in self.jobs.values() if job.status == "success" and job.handoff_issued),
            key=lambda job: (float(job.finished_at or float("inf")), job.id),
        )
        return [f"{job.email}|PASSKEY_HANDOFF_ISSUED" for job in completed]

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

    def _broadcast(self, job: PasskeyJob) -> None:
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

    def _require(self, job_id: str) -> PasskeyJob:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job
