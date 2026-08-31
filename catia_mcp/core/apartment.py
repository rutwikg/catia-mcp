"""A single-threaded COM apartment for every CATIA call.

Why this exists
---------------
CATIA's automation server is a single-threaded-apartment (STA) out-of-process
COM server. Two things go wrong if you ignore that:

1. ``pythoncom.CoInitialize()`` is per-thread. An MCP server runs handlers on
   whatever worker thread the event loop hands out, so a proxy obtained on
   thread A is frequently used from thread B. That either raises
   ``CO_E_NOTINITIALIZED`` or silently marshals through the global interface
   table with surprising lifetime rules.

2. CATIA rejects incoming calls with ``RPC_E_CALL_REJECTED`` whenever a modal
   dialog is open or a command is mid-flight. A native COM client installs an
   ``IMessageFilter`` to retry; Python cannot easily, so we retry explicitly.

Both problems are solved here: one dedicated daemon thread owns the apartment,
every CATIA interaction is submitted to it as a callable, and transient
rejections are retried with bounded exponential backoff.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from catia_mcp.core import errors

logger = logging.getLogger("catia_mcp.apartment")

T = TypeVar("T")

_SHUTDOWN = object()


class ComApartment:
    """Runs callables on one dedicated STA thread, with busy-retry."""

    def __init__(
        self,
        *,
        retry_attempts: int = 8,
        retry_initial_delay: float = 0.15,
        retry_max_delay: float = 2.0,
        default_timeout: float = 300.0,
    ) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._lock = threading.Lock()
        self.retry_attempts = retry_attempts
        self.retry_initial_delay = retry_initial_delay
        self.retry_max_delay = retry_max_delay
        self.default_timeout = default_timeout
        # Populated by the worker thread once COM is up; used by callers that
        # need to know whether we are on a machine that can talk COM at all.
        self.com_available = False
        self.com_import_error: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._run, name="catia-com-apartment", daemon=True
            )
            self._thread.start()
        self._ready.wait(timeout=30.0)
        if self._start_error is not None:
            raise errors.PlatformError(str(self._start_error))

    def shutdown(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return
        self._queue.put(_SHUTDOWN)
        thread.join(timeout=10.0)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_apartment_thread(self) -> bool:
        return threading.current_thread() is self._thread

    # ── submission ───────────────────────────────────────────────────────────

    def call(
        self,
        fn: Callable[..., T],
        *args: Any,
        timeout: float | None = None,
        retry: bool = True,
        **kwargs: Any,
    ) -> T:
        """Run ``fn`` on the apartment thread and return its result.

        Re-entrant: if we are already on the apartment thread (a tool helper
        calling another tool helper) the callable runs inline rather than
        deadlocking on its own queue.
        """
        if self.is_apartment_thread:
            return self._invoke(fn, args, kwargs, retry)

        if not self.alive:
            self.start()

        future: Future = Future()
        self._queue.put((future, fn, args, kwargs, retry))
        return future.result(timeout=timeout if timeout is not None else self.default_timeout)

    # ── worker ───────────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import pythoncom  # noqa: PLC0415  (deliberately deferred: Windows only)

            # STA. CATIA's automation objects are apartment-threaded; asking for
            # MTA here would force every call through a marshalling proxy.
            pythoncom.CoInitialize()
            self.com_available = True
        except Exception as exc:  # pragma: no cover - platform dependent
            self.com_available = False
            self.com_import_error = str(exc)
            self._start_error = exc
            self._ready.set()
            return

        self._ready.set()
        logger.debug("COM apartment thread started")

        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN:
                    break
                future, fn, args, kwargs, retry = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(self._invoke(fn, args, kwargs, retry))
                except BaseException as exc:  # noqa: BLE001 - forwarded to caller
                    future.set_exception(exc)
        finally:
            try:
                import pythoncom  # noqa: PLC0415

                pythoncom.CoUninitialize()
            except Exception:  # pragma: no cover
                pass
            logger.debug("COM apartment thread stopped")

    def _invoke(
        self,
        fn: Callable[..., T],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        retry: bool,
    ) -> T:
        if not retry:
            return fn(*args, **kwargs)

        delay = self.retry_initial_delay
        last: BaseException | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - inspected below
                if not errors.is_retryable(exc):
                    raise
                last = exc
                if attempt == self.retry_attempts:
                    break
                logger.info(
                    "CATIA busy (attempt %d/%d), retrying in %.2fs",
                    attempt,
                    self.retry_attempts,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2.0, self.retry_max_delay)

        assert last is not None
        raise errors.CatiaBusyError(
            "CATIA stayed busy across %d attempts (%s)"
            % (self.retry_attempts, errors.com_message(last))
        )


# One apartment per process. The connection object and every tool module share it.
APARTMENT = ComApartment()
