from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from local_model_chat.inference.budget import BudgetDenied, BudgetManager
from local_model_chat.storage import ChatStore


def test_atomic_reservation_prevents_two_workers_exceeding_project_limit(tmp_path) -> None:
    store = ChatStore(tmp_path / "budget.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    budget = BudgetManager(store)
    budget.set_project_limit(project.id, 0.01, actor="user")
    barrier = Barrier(2)

    def reserve(index: int):
        barrier.wait()
        try:
            return budget.reserve(f"request-{index}", project.id, 0.007)
        except BudgetDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(reserve, (1, 2)))

    assert sum(result is not None for result in results) == 1
    winning_id = next(result for result in results if result is not None)
    assert budget.active_amount(winning_id, "wrong-request", project.id) is None
    budget.release(winning_id)
    assert budget.reserve("replacement", project.id, 0.007)


def test_settled_cost_counts_against_limit_and_unknown_cost_uses_reserved_amount(tmp_path) -> None:
    store = ChatStore(tmp_path / "budget.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    budget = BudgetManager(store)
    budget.set_project_limit(project.id, 1.0, actor="user")
    first = budget.reserve("first", project.id, 0.7)
    budget.settle(first)
    with pytest.raises(BudgetDenied, match="hard budget"):
        budget.reserve("second", project.id, 0.4)
    second = budget.reserve("second", project.id, 0.3)
    budget.settle(second, actual_usd=0.1)
    assert budget.reserve("third", project.id, 0.2)


def test_limit_requires_explicit_user_actor(tmp_path) -> None:
    store = ChatStore(tmp_path / "budget.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    with pytest.raises(ValueError, match="user-set"):
        BudgetManager(store).set_project_limit(project.id, 10, actor="automation")


def test_task_month_and_premium_reserve_limits(tmp_path) -> None:
    store = ChatStore(tmp_path / "budget.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    budget = BudgetManager(store)
    budget.set_project_limit(project.id, 10, actor="user", premium_reserve_usd=3)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    budget.set_scope_limit(project.id, "month", month, 6, actor="user")
    budget.set_scope_limit(project.id, "task", "*", 4, actor="user")

    with pytest.raises(BudgetDenied, match="task scope"):
        budget.reserve("unscoped", project.id, 1)
    budget.reserve("task-a-1", project.id, 3, task_scope="task-a")
    with pytest.raises(BudgetDenied, match="task hard budget"):
        budget.reserve("task-a-2", project.id, 2, task_scope="task-a")
    budget.reserve("task-b-1", project.id, 3, task_scope="task-b")
    with pytest.raises(BudgetDenied, match="month hard budget"):
        budget.reserve("task-c-1", project.id, 1, task_scope="task-c")


def test_premium_reserve_and_exact_approved_exception(tmp_path) -> None:
    store = ChatStore(tmp_path / "budget.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    budget = BudgetManager(store)
    budget.set_project_limit(project.id, 10, actor="user", premium_reserve_usd=3)
    budget.reserve("standard", project.id, 7)
    with pytest.raises(BudgetDenied, match="premium reserve"):
        budget.reserve("more-standard", project.id, 1)
    budget.reserve("premium", project.id, 3, tier="premium")
    with pytest.raises(PermissionError, match="approval"):
        budget.reserve("approved-exception", project.id, 1, allow_exception=True)
    store.record_action_authorization(
        project_id=project.id, action="budget_exception",
        scope=f"request:approved-exception:project:{project.id}", risk="financial",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    assert budget.reserve("approved-exception", project.id, 1, allow_exception=True)
