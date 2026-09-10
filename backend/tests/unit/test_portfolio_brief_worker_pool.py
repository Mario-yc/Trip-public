from __future__ import annotations

import time

from src.services.amap_call_budget import (
    AmapCallBudget,
    amap_call_budget_scope,
    current_amap_call_budget,
)
from src.services.portfolio_brief_worker_pool import PortfolioBriefWorkerPool


def test_four_brief_preflights_overlap_but_never_exceed_two_workers():
    pool = PortfolioBriefWorkerPool()

    result = pool.run(range(4), lambda value: (time.sleep(0.2), value)[1])

    assert [item.value for item in result.results] == [0, 1, 2, 3]
    assert result.max_concurrent_workers == 2
    assert result.duration_ms < 600


def test_worker_failure_is_isolated_and_merge_order_remains_deterministic():
    def worker(value: int) -> int:
        if value == 1:
            raise RuntimeError("one brief failed")
        return value * 10

    result = PortfolioBriefWorkerPool().run([0, 1, 2, 3], worker)

    assert [item.index for item in result.results] == [0, 1, 2, 3]
    assert [item.value for item in result.results] == [0, None, 20, 30]
    assert isinstance(result.results[1].error, RuntimeError)


def test_worker_pool_propagates_the_explicit_amap_budget_context_to_each_thread():
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()

    with amap_call_budget_scope(budget):
        result = PortfolioBriefWorkerPool().run(
            range(4),
            lambda _value: current_amap_call_budget() is budget,
        )

    assert [item.value for item in result.results] == [True, True, True, True]


def test_worker_pool_reports_real_completion_progress_on_coordinator_thread():
    progress: list[tuple[int, int, int, bool]] = []

    result = PortfolioBriefWorkerPool().run(
        [0, 1, 2],
        lambda value: value * 10,
        on_complete=lambda item, completed, total: progress.append(
            (item.index, completed, total, item.error is None)
        ),
    )

    assert len(result.results) == 3
    assert [item[1] for item in progress] == [1, 2, 3]
    assert {item[0] for item in progress} == {0, 1, 2}
    assert {item[2] for item in progress} == {3}
    assert all(item[3] for item in progress)
