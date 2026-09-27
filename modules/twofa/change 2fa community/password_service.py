"""Safe orchestration for password mutation and crash/ambiguity recovery."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from password_phase import (
    PasswordMutationRejected,
    PasswordMutationUncertain,
    change_password_pure_request,
)


LogFn = Callable[[str], None]
CheckpointFn = Callable[[str], Awaitable[None]]
LoginFn = Callable[..., Awaitable[dict[str, Any]]]
MutationFn = Callable[..., Awaitable[None]]


class PasswordChangeError(RuntimeError):
    """A credential-safe error for the local password control plane."""

    def __init__(self, message: str, *, error_kind: str = "technical_error") -> None:
        super().__init__(message)
        self.error_kind = error_kind


@dataclass(frozen=True, slots=True)
class PasswordChangeResult:
    login_verified: bool


class PasswordService:
    """Mutate at most once, then resolve the actual state with fresh logins."""

    def __init__(
        self,
        *,
        login_fn: LoginFn | None = None,
        mutate_fn: MutationFn | None = None,
    ) -> None:
        self._login_fn = login_fn
        self._mutate_fn = mutate_fn

    @staticmethod
    def _default_login() -> LoginFn:
        from session_phase import get_session_pure_request

        return get_session_pure_request

    @staticmethod
    def _safe_login_logger(log: LogFn) -> LogFn:
        """Expose login milestones without OAuth URLs, identities, or IDs."""
        milestones = (
            ("password login flow", "[login] Đã vào luồng đăng nhập bằng mật khẩu"),
            ("password verified", "[login] Mật khẩu đăng nhập đã được xác minh"),
            ("mfa challenge detected", "[login] Tài khoản yêu cầu xác minh 2FA"),
            ("issuing mfa challenge", "[login] Đã khởi tạo bước xác minh 2FA"),
            ("verifying totp", "[login] Đang xác minh mã TOTP"),
            ("mfa verified", "[login] Mã TOTP đã được xác minh"),
            ("consume_callback verified", "[login] Callback đăng nhập đã được xác nhận"),
            ("get /api/auth/session", "[login] Đã đọc phiên ChatGPT"),
            ("✓ done", "[login] Đăng nhập pure-request thành công"),
        )

        def safe_log(message: str) -> None:
            normalized = str(message or "").casefold()
            for marker, replacement in milestones:
                if marker in normalized:
                    log(replacement)
                    return

        return safe_log

    async def _login(
        self,
        *,
        email: str,
        password: str,
        secret: str,
        timeout: float,
        log: LogFn,
    ) -> dict[str, Any]:
        login_fn = self._login_fn or self._default_login()
        session = await asyncio.wait_for(
            login_fn(
                email=email,
                password=password,
                secret=secret,
                proxy=None,
                log=self._safe_login_logger(log),
            ),
            timeout=timeout,
        )
        if not isinstance(session, dict) or not session.get("accessToken"):
            raise PasswordChangeError("Đăng nhập không trả về access token")
        return session

    async def _reconcile(
        self,
        *,
        email: str,
        current_password: str,
        target_password: str,
        secret: str,
        timeout: float,
        log: LogFn,
    ) -> PasswordChangeResult:
        log("[verify] Đăng nhập bằng mật khẩu mới để xác nhận trạng thái...")
        try:
            await self._login(
                email=email,
                password=target_password,
                secret=secret,
                timeout=timeout,
                log=log,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        else:
            log("[done] Mật khẩu mới đã được xác minh bằng phiên đăng nhập mới")
            return PasswordChangeResult(login_verified=True)

        log("[verify] Mật khẩu mới chưa đăng nhập được; kiểm tra mật khẩu cũ...")
        try:
            await self._login(
                email=email,
                password=current_password,
                secret=secret,
                timeout=timeout,
                log=log,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PasswordChangeError(
                "Không xác định được mật khẩu nào đang có hiệu lực; không tự gửi lại yêu cầu",
                error_kind="password_uncertain",
            ) from exc
        raise PasswordChangeError(
            "Mật khẩu cũ vẫn đăng nhập được; lần đổi trước chưa được áp dụng",
            error_kind="password_not_applied",
        )

    async def change(
        self,
        *,
        email: str,
        current_password: str,
        target_password: str,
        secret: str,
        timeout: float,
        mutation_started: bool,
        checkpoint: CheckpointFn,
        log: LogFn,
    ) -> PasswordChangeResult:
        if mutation_started:
            log("[recovery] Có mutation checkpoint; chỉ đối soát, không gửi lại")
            return await self._reconcile(
                email=email,
                current_password=current_password,
                target_password=target_password,
                secret=secret,
                timeout=timeout,
                log=log,
            )

        log("[1/3] Đăng nhập bằng mật khẩu và 2FA hiện tại...")
        try:
            session = await self._login(
                email=email,
                password=current_password,
                secret=secret,
                timeout=timeout,
                log=log,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PasswordChangeError(
                "Không thể đăng nhập bằng thông tin hiện tại",
                error_kind="invalid_credentials",
            ) from exc

        # This durable checkpoint must complete before the non-idempotent POST.
        await checkpoint("mutation_started")
        mutate_fn = self._mutate_fn or change_password_pure_request
        try:
            await asyncio.wait_for(
                mutate_fn(
                    session_data=session,
                    current_password=current_password,
                    new_password=target_password,
                    secret=secret,
                    log=log,
                ),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except PasswordMutationRejected as exc:
            raise PasswordChangeError(
                str(exc),
                error_kind="password_rejected",
            ) from exc
        except (PasswordMutationUncertain, asyncio.TimeoutError, Exception):
            # Once checkpointed, any transport/runtime failure is reconciled by
            # login state.  The mutation is never repeated automatically.
            log("[recovery] Phản hồi mutation không chắc chắn; đối soát bằng đăng nhập")

        return await self._reconcile(
            email=email,
            current_password=current_password,
            target_password=target_password,
            secret=secret,
            timeout=timeout,
            log=log,
        )
