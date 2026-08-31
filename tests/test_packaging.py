from pathlib import Path
import tomllib


def test_frontier_plan_schema_is_declared_as_package_data() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with (project_root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    package_data = config["tool"]["setuptools"]["package-data"]
    assert "molicoder/planner/plan.schema.json" in package_data["local_model_chat"]
