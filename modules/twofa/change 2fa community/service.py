"""Core orchestration for Shoptaikhoan Tool."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


LogFn = Callable[[str], None]
CheckpointFn = Callable[[str], Awaitable[None]]
LoginFn = Callable[..., Awaitable[dict[str, Any]]]
RotateFn = Callable[..., Awaitable[dict[str, Any]]]
EntitlementFn = Callable[..., Awaitable[dict[str, Any]]]
UsageFn = Callable[..., Awaitable[dict[str, Any]]]
PaymentMethodsFn = Callable[..., Awaitable[list[dict[str, Any]]]]
DeleteChatsFn = Callable[..., Awaitable[None]]


class TwoFAFlowError(RuntimeError):
    """A fail-fast error safe to display in the local control plane."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "technical_error",
        account_state: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.account_state = account_state


@dataclass(frozen=True, slots=True)
class RotationResult:
    secret: str
    login_verified: bool
    account_state: str = "live"
    plan: str | None = None
    plan_source: str | None = None
    usage: dict[str, Any] | None = None
    payment_methods: list[dict[str, Any]] | None = None
    billing_date: str | None = None


class TwoFAService:
    """Run the smallest safe 2FA rotation flow using the existing core APIs."""

    def __init__(
        self,
        *,
        login_fn: LoginFn | None = None,
        rotate_fn: RotateFn | None = None,
        entitlement_fn: EntitlementFn | None = None,
        usage_fn: UsageFn | None = None,
        payment_methods_fn: PaymentMethodsFn | None = None,
        delete_chats_fn: DeleteChatsFn | None = None,
        logout_sessions_fn: Callable[..., Awaitable[None]] | None = None,
        login_attempts: int = 3,
        retry_delay: float = 3.0,
    ) -> None:
        self._login_fn = login_fn
        self._rotate_fn = rotate_fn
        self._entitlement_fn = entitlement_fn
        self._usage_fn = usage_fn
        self._payment_methods_fn = payment_methods_fn
        self._delete_chats_fn = delete_chats_fn
        self._logout_sessions_fn = logout_sessions_fn
        self._login_attempts = login_attempts
        self._retry_delay = retry_delay

    @staticmethod
    def _resolve_dependencies() -> tuple[LoginFn, RotateFn]:
        from mfa_phase import rotate_2fa
        from session_phase import get_session_pure_request

        return get_session_pure_request, rotate_2fa

    async def _login(
        self,
        *,
        email: str,
        password: str,
        secret: str,
        timeout: float,
        log: LogFn,
    ) -> dict[str, Any]:
        from session_phase import (
            classify_account_check_error,
            is_fatal_login_error,
        )

        default_login, _ = self._resolve_dependencies()
        login_fn = self._login_fn or default_login
        last_error: BaseException | None = None
        for attempt in range(1, self._login_attempts + 1):
            try:
                session = await asyncio.wait_for(
                    login_fn(
                        email=email,
                        password=password,
                        secret=secret,
                        proxy=None,
                        log=log,
                    ),
                    timeout=timeout,
                )
                token = session.get("accessToken")
                if not isinstance(token, str) or not token.strip():
                    raise TwoFAFlowError("Đăng nhập không trả về access token")
                return session
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if is_fatal_login_error(exc) or attempt >= self._login_attempts:
                    break
                log(f"[login] lần {attempt}/{self._login_attempts} chưa thành công — thử lại...")
                await asyncio.sleep(self._retry_delay)
        detail = str(last_error).strip() if last_error else "unknown login error"
        account_state = (
            "die"
            if classify_account_check_error(last_error) == "deactivated"
            else "unknown"
        )
        error_kind = (
            "account_die"
            if account_state == "die"
            else "invalid_credentials"
            if last_error is not None and is_fatal_login_error(last_error)
            else "technical_error"
        )
        label = "Tài khoản die" if account_state == "die" else "Đăng nhập thất bại"
        raise TwoFAFlowError(
            f"{label}: {detail[:220]}",
            error_kind=error_kind,
            account_state=account_state,
        ) from last_error

    @staticmethod
    def _session_plan(session: dict[str, Any]) -> str | None:
        top = session.get("accountPlan")
        if isinstance(top, str) and top.strip():
            return top.strip().casefold()
        account = session.get("account")
        nested = account.get("planType") if isinstance(account, dict) else None
        return nested.strip().casefold() if isinstance(nested, str) and nested.strip() else None

    @staticmethod
    def _session_account_id(session: dict[str, Any]) -> str | None:
        account = session.get("account")
        candidates = [session.get("accountId"), session.get("account_id")]
        if isinstance(account, dict):
            candidates.extend((account.get("id"), account.get("accountId")))
        return next(
            (
                candidate.strip()
                for candidate in candidates
                if isinstance(candidate, str) and candidate.strip()
            ),
            None,
        )

    async def _check_plan(
        self,
        *,
        session: dict[str, Any],
        timeout: float,
        log: LogFn,
    ) -> tuple[str | None, str | None, str | None]:
        from session_phase import fetch_account_entitlement

        fallback = self._session_plan(session)
        entitlement_fn = self._entitlement_fn or fetch_account_entitlement
        try:
            payload = await asyncio.wait_for(
                entitlement_fn(
                    access_token=str(session["accessToken"]),
                    cookies=session.get("__cookies"),
                    proxy=None,
                    timeout=min(timeout, 20.0),
                ),
                timeout=min(timeout, 25.0),
            )
            plan = str(payload.get("plan") or "").strip().casefold()
            if not plan:
                plan = "plus" if payload.get("is_plus") is True else fallback or "free"
            billing_date = payload.get("expires")
            if not isinstance(billing_date, str):
                billing_date = None
            log(f"[account] Tài khoản live · gói {plan.upper()}")
            return plan, "entitlement", billing_date
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if fallback:
                log(f"[account] Entitlement chưa đọc được; dùng session plan {fallback.upper()}")
                return fallback, "session", None
            log(f"[account] Chưa xác định được gói: {type(exc).__name__}")
            return None, None, None

    async def _check_usage(
        self,
        *,
        session: dict[str, Any],
        timeout: float,
        log: LogFn,
    ) -> dict[str, Any] | None:
        from session_phase import fetch_codex_weekly_usage

        usage_fn = self._usage_fn or fetch_codex_weekly_usage
        try:
            payload = await asyncio.wait_for(
                usage_fn(
                    access_token=str(session["accessToken"]),
                    account_id=self._session_account_id(session),
                    cookies=session.get("__cookies"),
                    proxy=None,
                    timeout=min(timeout, 20.0),
                ),
                timeout=min(timeout, 25.0),
            )
            if not isinstance(payload, dict):
                raise TypeError("usage summary must be an object")
            used = payload.get("used_percent")
            remaining = payload.get("remaining_percent")
            if not isinstance(used, (int, float)) or isinstance(used, bool):
                raise ValueError("usage percent is unavailable")
            if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
                raise ValueError("usage remaining percent is unavailable")
            reset_after = payload.get("reset_after_seconds")
            reset_label = ""
            if isinstance(reset_after, (int, float)) and not isinstance(reset_after, bool):
                seconds = max(0, int(reset_after))
                days, remainder = divmod(seconds, 86400)
                hours = remainder // 3600
                if days:
                    reset_label = f" · reset sau {days} ngày {hours} giờ"
                elif hours:
                    reset_label = f" · reset sau {hours} giờ"
                else:
                    reset_label = f" · reset sau {max(1, remainder // 60)} phút"
            state_label = " · ĐÃ CHẠM GIỚI HẠN" if payload.get("limit_reached") is True else ""
            log(
                f"[usage] Đã dùng {float(used):g}% · còn {float(remaining):g}%"
                f"{reset_label}{state_label}"
            )
            return payload
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Deliberately log only the exception type: HTTP errors can contain
            # bearer tokens, cookies or response fragments.
            log(f"[usage] Chưa đọc được Usage: {type(exc).__name__}")
            return None

    async def _check_payment_methods(
        self,
        *,
        session: dict[str, Any],
        timeout: float,
        log: LogFn,
    ) -> list[dict[str, Any]] | None:
        from session_phase import fetch_payment_methods, _sanitize_payment_methods

        payment_methods_fn = self._payment_methods_fn or fetch_payment_methods
        try:
            payload = await asyncio.wait_for(
                payment_methods_fn(
                    access_token=str(session["accessToken"]),
                    account_id=self._session_account_id(session),
                    cookies=session.get("__cookies"),
                    proxy=None,
                    timeout=min(timeout, 20.0),
                ),
                timeout=min(timeout, 25.0),
            )
            methods = _sanitize_payment_methods(payload)
            if methods:
                log(f"[payment] Đã đọc {len(methods)} phương thức thanh toán")
            else:
                log("[payment] Tài khoản không có phương thức thanh toán đã lưu")
            return methods
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Upstream payment errors may carry account or billing fragments.
            log(f"[payment] Chưa đọc được phương thức thanh toán: {type(exc).__name__}")
            return None

    async def _inspect_account(
        self,
        *,
        session: dict[str, Any],
        timeout: float,
        log: LogFn,
        read_usage: bool = True,
        read_payment_methods: bool = True,
    ) -> tuple[
        str | None,
        str | None,
        str | None,
        dict[str, Any] | None,
        list[dict[str, Any]] | None,
    ]:
        names = ["plan"]
        inspections = [self._check_plan(session=session, timeout=timeout, log=log)]
        if read_usage:
            names.append("usage")
            inspections.append(
                self._check_usage(session=session, timeout=timeout, log=log)
            )
        else:
            log("[usage] Đã tắt theo cấu hình")
        if read_payment_methods:
            names.append("payment_methods")
            inspections.append(
                self._check_payment_methods(session=session, timeout=timeout, log=log)
            )
        else:
            log("[payment] Đã tắt theo cấu hình")

        inspected = dict(zip(names, await asyncio.gather(*inspections), strict=True))
        plan_result = inspected["plan"]
        usage = inspected.get("usage")
        payment_methods = inspected.get("payment_methods")
        plan, plan_source, billing_date = plan_result
        return plan, plan_source, billing_date, usage, payment_methods

    async def refresh_usage(
        self,
        *,
        email: str,
        password: str,
        secret: str,
        timeout: float,
        log: LogFn,
    ) -> dict[str, Any]:
        """Re-authenticate and retry Usage without changing account or 2FA state."""
        log("[usage] Đang đăng nhập lại để đọc Usage...")
        try:
            session = await self._login(
                email=email,
                password=password,
                secret=secret,
                timeout=timeout,
                log=log,
            )
        except asyncio.CancelledError:
            raise
        except TwoFAFlowError as exc:
            raise TwoFAFlowError(
                "Không thể đăng nhập lại để đọc Usage",
                error_kind=exc.error_kind,
                account_state=exc.account_state,
            ) from exc
        usage = await self._check_usage(session=session, timeout=timeout, log=log)
        if usage is None:
            raise TwoFAFlowError(
                "Chưa đọc được Usage; vui lòng thử lại sau",
                account_state="live",
            )
        log("[usage] Đọc lại Usage thành công")
        return usage

    async def prepare_passkey(
        self, *, email: str, password: str, secret: str, timeout: float,
    ) -> str:
        from passkey_service import prepare_passkey_url

        try:
            session = await self._login(
                email=email, password=password, secret=secret, timeout=timeout,
                log=lambda _message: None,
            )
            return await asyncio.wait_for(
                prepare_passkey_url(session_data=session, timeout=min(timeout, 20.0)),
                timeout=min(timeout, 25.0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TwoFAFlowError(
                "Không chuẩn bị được passkey. Tài khoản có thể chưa hỗ trợ hoặc phiên đã hết hạn; "
                "hãy thử thêm trong Settings > Security của ChatGPT."
            ) from exc

    async def logout_all_sessions(
        self, *, email: str, password: str, secret: str, timeout: float, log: LogFn,
    ) -> None:
        from session_phase import logout_all_sessions

        try:
            session = await self._login(
                email=email, password=password, secret=secret, timeout=timeout,
                log=lambda _message: None,
            )
            await asyncio.wait_for(
                (self._logout_sessions_fn or logout_all_sessions)(
                    access_token=session["accessToken"], cookies=session.get("__cookies"),
                    timeout=min(timeout, 20.0),
                ), timeout=min(timeout, 25.0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A lost response may follow a successful revocation. Never auto-replay.
            raise TwoFAFlowError(
                "Logout chưa xác nhận; kiểm tra phiên đăng nhập trước khi thử lại."
            ) from exc
        # Do not log in again: that would create a session after revocation.

    async def delete_all_chats(
        self,
        *,
        email: str,
        password: str,
        secret: str,
        timeout: float,
        log: LogFn,
    ) -> None:
        """Re-authenticate and delete all chats without touching other account data."""
        from session_phase import delete_all_chats

        log("[chat] Đang đăng nhập lại để xóa toàn bộ dữ liệu chat...")
        try:
            session = await self._login(
                email=email,
                password=password,
                secret=secret,
                timeout=timeout,
                log=log,
            )
            delete_fn = self._delete_chats_fn or delete_all_chats
            await asyncio.wait_for(
                delete_fn(
                    access_token=str(session["accessToken"]),
                    account_id=self._session_account_id(session),
                    cookies=session.get("__cookies"),
                    proxy=None,
                    timeout=min(timeout, 20.0),
                ),
                timeout=min(timeout, 25.0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"[chat] Xóa dữ liệu chat thất bại: {type(exc).__name__}")
            raise TwoFAFlowError(
                "Không thể xóa dữ liệu chat; vui lòng thử lại",
                account_state="live",
            ) from exc
        log("[chat] Đã xóa toàn bộ dữ liệu chat")

    async def check(
        self,
        *,
        email: str,
        password: str,
        secret: str,
        timeout: float,
        log: LogFn,
        read_usage: bool = True,
        read_payment_methods: bool = True,
    ) -> RotationResult:
        """Authenticate and classify the account without changing its TOTP secret."""
        log("[1/2] Đang xác thực tài khoản — chế độ chỉ kiểm tra...")
        session = await self._login(
            email=email,
            password=password,
            secret=secret,
            timeout=timeout,
            log=log,
        )
        plan, plan_source, billing_date, usage, payment_methods = await self._inspect_account(
            session=session,
            timeout=timeout,
            log=log,
            read_usage=read_usage,
            read_payment_methods=read_payment_methods,
        )
        log("[done] Đã kiểm tra tài khoản; không thay đổi 2FA")
        return RotationResult(
            secret=secret,
            login_verified=True,
            account_state="live",
            plan=plan,
            plan_source=plan_source,
            usage=usage,
            payment_methods=payment_methods,
            billing_date=billing_date,
        )

    async def rotate(
        self,
        *,
        email: str,
        password: str,
        old_secret: str,
        timeout: float,
        checkpoint: CheckpointFn,
        log: LogFn,
        read_usage: bool = True,
        read_payment_methods: bool = True,
    ) -> RotationResult:
        """Rotate once and persist the new secret before fresh-login verification."""
        log("[1/3] Đang xác thực tài khoản với 2FA hiện tại...")
        session = await self._login(
            email=email,
            password=password,
            secret=old_secret,
            timeout=timeout,
            log=log,
        )
        access_token = str(session["accessToken"])
        plan, plan_source, billing_date, usage, payment_methods = await self._inspect_account(
            session=session,
            timeout=timeout,
            log=log,
            read_usage=read_usage,
            read_payment_methods=read_payment_methods,
        )

        log("[2/3] Đang thay thế khóa TOTP...")
        _, default_rotate = self._resolve_dependencies()
        rotate_fn = self._rotate_fn or default_rotate
        try:
            payload = await asyncio.wait_for(
                rotate_fn(
                    access_token=access_token,
                    cookies=session.get("__cookies"),
                    proxy=None,
                    log=log,
                ),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise TwoFAFlowError(f"Đổi 2FA thất bại: {detail[:220]}") from exc

        new_secret = str(payload.get("secret") or "").strip()
        if payload.get("activated") is not True or not new_secret:
            raise TwoFAFlowError("Secret mới chưa được kích hoạt")

        await checkpoint(new_secret)
        log("[checkpoint] Secret mới đã được lưu an toàn")
        await self._verify_login(
            email=email,
            password=password,
            new_secret=new_secret,
            timeout=timeout,
            log=log,
        )
        return RotationResult(
            secret=new_secret,
            login_verified=True,
            account_state="live",
            plan=plan,
            plan_source=plan_source,
            usage=usage,
            payment_methods=payment_methods,
            billing_date=billing_date,
        )

    async def _verify_login(
        self,
        *,
        email: str,
        password: str,
        new_secret: str,
        timeout: float,
        log: LogFn,
    ) -> dict[str, Any]:
        log("[3/3] Đang đăng nhập lại bằng 2FA mới...")
        session = await self._login(
            email=email,
            password=password,
            secret=new_secret,
            timeout=timeout,
            log=log,
        )
        log("[done] 2FA mới đã được xác minh thành công")
        return session

    async def verify(
        self,
        *,
        email: str,
        password: str,
        new_secret: str,
        timeout: float,
        log: LogFn,
        read_usage: bool = True,
        read_payment_methods: bool = True,
    ) -> RotationResult:
        """Verify an already-checkpointed secret without rotating again."""
        session = await self._verify_login(
            email=email,
            password=password,
            new_secret=new_secret,
            timeout=timeout,
            log=log,
        )
        plan, plan_source, billing_date, usage, payment_methods = await self._inspect_account(
            session=session,
            timeout=timeout,
            log=log,
            read_usage=read_usage,
            read_payment_methods=read_payment_methods,
        )
        return RotationResult(
            secret=new_secret,
            login_verified=True,
            account_state="live",
            plan=plan,
            plan_source=plan_source,
            usage=usage,
            payment_methods=payment_methods,
            billing_date=billing_date,
        )
