"""Bounded read-only worker pool for deterministic Portfolio Stage B work."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import Callable, Generic, Iterable, Optional, TypeVar


InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class PortfolioBriefWorkerResult(Generic[OutputT]):
    index: int
    value: OutputT | None
    duration_ms: float
    error: Exception | None = None
    error_class: str | None = None
    sanitized_error_message: str | None = None


@dataclass(frozen=True)
class PortfolioBriefWorkerRun(Generic[OutputT]):
    results: list[PortfolioBriefWorkerResult[OutputT]]
    max_concurrent_workers: int
    duration_ms: float


class PortfolioBriefWorkerPool:
    MAX_WORKERS = 2

    def run(
        self,
        inputs: Iterable[InputT],
        worker: Callable[[InputT], OutputT],
        on_complete: Optional[
            Callable[[PortfolioBriefWorkerResult[OutputT], int, int], None]
        ] = None,
    ) -> PortfolioBriefWorkerRun[OutputT]:
        indexed = list(enumerate(inputs))
        if not indexed:
            return PortfolioBriefWorkerRun([], 0, 0.0)
        lock = Lock()
        active = 0
        max_active = 0
        started = perf_counter()

        def invoke(index: int, item: InputT) -> PortfolioBriefWorkerResult[OutputT]:
            nonlocal active, max_active
            worker_started = perf_counter()
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                return PortfolioBriefWorkerResult(
                    index=index,
                    value=worker(item),
                    duration_ms=(perf_counter() - worker_started) * 1000,
                )
            except Exception as error:  # one brief failure is isolated
                message = str(error).replace("\n", " ").replace("\r", " ").strip()
                if any(marker in message.casefold() for marker in ("authorization", "token", "api_key", "cookie", "secret")):
                    message = "redacted_worker_error"
                return PortfolioBriefWorkerResult(
                    index=index,
                    value=None,
                    duration_ms=(perf_counter() - worker_started) * 1000,
                    error=error,
                    error_class=type(error).__name__,
                    sanitized_error_message=(message[:280] or type(error).__name__),
                )
            finally:
                with lock:
                    active -= 1

        results: list[PortfolioBriefWorkerResult[OutputT]] = []
        with ThreadPoolExecutor(
            max_workers=self.MAX_WORKERS,
            thread_name_prefix="portfolio-preflight",
        ) as executor:
            futures = {
                executor.submit(copy_context().run, invoke, index, item): index
                for index, item in indexed
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                if on_complete is not None:
                    on_complete(result, len(results), len(indexed))
        results.sort(key=lambda item: item.index)
        return PortfolioBriefWorkerRun(
            results=results,
            max_concurrent_workers=max_active,
            duration_ms=(perf_counter() - started) * 1000,
        )
