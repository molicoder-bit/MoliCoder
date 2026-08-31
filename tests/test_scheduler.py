from local_model_chat.molicoder.planner.schema import Plan, PlanTask
from local_model_chat.molicoder.scheduler import DagScheduler


def _task(key, depends=(), paths=()):
    return PlanTask(key, key.title(), f"Edit {key}", "low", tuple(depends), tuple(paths))


def test_scheduler_releases_dependencies_only_after_completion() -> None:
    scheduler = DagScheduler(Plan("feature", (_task("a"), _task("b", ("a",)), _task("c"))))

    assert [task.key for task in scheduler.ready()] == ["a", "c"]
    scheduler.start("a")
    assert [task.key for task in scheduler.ready()] == ["c"]
    scheduler.complete("a", ("a.py",))
    assert [task.key for task in scheduler.ready()] == ["b", "c"]


def test_scheduler_serializes_ready_tasks_with_predicted_path_overlap() -> None:
    scheduler = DagScheduler(
        Plan("feature", (_task("a", paths=("shared.py",)), _task("b", paths=("shared.py",)), _task("c", paths=("other.py",))))
    )
    scheduler.start("a")

    assert [task.key for task in scheduler.ready()] == ["c"]
