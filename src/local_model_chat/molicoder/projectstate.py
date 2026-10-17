from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ProjectState:
    root: Path
    kind: str
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GitState:
    branch: str
    dirty: bool
    available: bool


_VALIDATION: dict[str, list[dict[str, Any]]] = {
    "python": [{"name": "pytest", "argv": ["{python}", "-m", "pytest"]}],
    "node": [
        {"name": "test", "argv": ["npm", "test", "--", "--runInBand"]},
        {"name": "build", "argv": ["npm", "run", "build"]},
    ],
    "flutter": [
        {"name": "analyze", "argv": ["flutter", "analyze"]},
        {"name": "test", "argv": ["flutter", "test"]},
    ],
    "unknown": [],
}

_CREATABLE_KINDS = {"python", "node", "flutter", "unknown"}
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _run_checked(argv: list[str], *, cwd: Path, timeout: float = 180.0) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"required program is not installed: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"project setup timed out while running: {' '.join(argv[:3])}") from exc
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()[-2000:]
        raise ValueError(detail or f"project setup failed: {' '.join(argv[:3])}")


def _write_new(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8", newline="")


def _setup_python_environment(root: Path) -> None:
    python = shutil.which("python")
    if not python:
        raise ValueError("Python is required to create a Python project but was not found in PATH")
    environment = root / ".venv"
    if not (environment / "Scripts" / "python.exe").is_file():
        _run_checked([python, "-m", "venv", str(environment)], cwd=root)
    environment_python = environment / "Scripts" / "python.exe"
    _run_checked(
        [str(environment_python), "-m", "pip", "install", "--disable-pip-version-check", "pytest==9.1.1"],
        cwd=root,
    )


def bootstrap_project(parent: str | Path, name: str, kind: str) -> ProjectState:
    """Create a build-ready local project without model-selected shell commands."""
    parent_path = Path(parent).resolve()
    clean_name = name.strip()
    normalized_kind = kind.strip().lower()
    if not parent_path.is_dir():
        raise ValueError(f"parent folder does not exist: {parent_path}")
    if not _PROJECT_NAME.fullmatch(clean_name):
        raise ValueError("project name must use only letters, numbers, dots, dashes, or underscores")
    if normalized_kind not in _CREATABLE_KINDS:
        raise ValueError(f"unsupported project type: {kind}")
    root = (parent_path / clean_name).resolve()
    if root.parent != parent_path:
        raise ValueError("project must be created directly inside the selected parent folder")
    if root.exists() and not root.is_dir():
        raise ValueError(f"project path is not a folder: {root}")
    if root.is_dir():
        resumable_entries = {
            "python": {".molicoder", ".venv", ".gitignore", "README.md", "pyproject.toml"},
            "node": {".molicoder", ".gitignore", "README.md", "package.json"},
            "flutter": {".molicoder"},
            "unknown": {".molicoder", "README.md"},
        }[normalized_kind]
        unrelated = [entry.name for entry in root.iterdir() if entry.name not in resumable_entries]
        if unrelated:
            raise ValueError(
                "automatic project creation requires an empty folder; found: "
                + ", ".join(sorted(unrelated)[:8])
            )
    else:
        root.mkdir()

    if normalized_kind == "python":
        module_name = re.sub(r"[^a-z0-9_]", "_", clean_name.lower().replace("-", "_"))
        _write_new(
            root / "pyproject.toml",
            "[project]\n"
            f'name = "{clean_name.lower().replace("_", "-")}"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n\n'
            "[project.optional-dependencies]\n"
            'test = ["pytest==9.1.1"]\n\n'
            "[tool.pytest.ini_options]\n"
            'pythonpath = ["src"]\n'
            'testpaths = ["tests"]\n',
        )
        _write_new(root / ".gitignore", ".venv/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n")
        package = root / "src" / module_name
        tests = root / "tests"
        package.mkdir(parents=True, exist_ok=True)
        tests.mkdir(exist_ok=True)
        _write_new(package / "__init__.py", '"""Application package."""\n')
        _write_new(
            tests / "test_smoke.py",
            f"def test_package_imports():\n    import {module_name}\n    assert {module_name} is not None\n",
        )
        _setup_python_environment(root)
    elif normalized_kind == "node":
        package_name = clean_name.lower().replace("_", "-")
        _write_new(
            root / "package.json",
            json.dumps(
                {
                    "name": package_name,
                    "version": "0.1.0",
                    "private": True,
                    "type": "module",
                    "scripts": {"test": "node --test", "build": "node --check app.js"},
                },
                indent=2,
            )
            + "\n",
        )
        _write_new(root / ".gitignore", "node_modules/\ndist/\n")
        _write_new(
            root / "index.html",
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{clean_name}</title></head><body><main id=\"app\"></main>"
            '<script type="module" src="./app.js"></script></body></html>\n',
        )
        _write_new(root / "app.js", 'export function start() { return "ready"; }\n')
        _write_new(
            root / "app.test.js",
            'import test from "node:test";\nimport assert from "node:assert/strict";\n'
            'import { start } from "./app.js";\ntest("starter is ready", () => assert.equal(start(), "ready"));\n',
        )
    elif normalized_kind == "flutter":
        flutter_name = re.sub(r"[^a-z0-9_]", "_", clean_name.lower().replace("-", "_"))
        _run_checked(
            ["flutter", "create", "--empty", "--project-name", flutter_name, "."],
            cwd=root,
            timeout=300.0,
        )

    _write_new(root / "README.md", f"# {clean_name}\n")
    state = ensure_project_state(root)
    if state.kind != normalized_kind:
        raise ValueError(f"created project was detected as {state.kind}, expected {normalized_kind}")
    if normalized_kind == "node":
        config = dict(state.config)
        config["validation"] = [
            {"name": "test", "argv": ["npm", "test"]},
            {"name": "build", "argv": ["npm", "run", "build"]},
        ]
        (root / ".molicoder" / "project.json").write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8", newline=""
        )
        state = ProjectState(root, normalized_kind, config)
    _run_checked(["git", "init", "-b", "main"], cwd=root)
    _run_checked(["git", "add", "-A"], cwd=root)
    _run_checked(
        [
            "git", "-c", "user.name=MoliCoder", "-c", "user.email=molicoder@localhost",
            "commit", "-m", "Initialize project with MoliCoder",
        ],
        cwd=root,
    )
    return state


def detect_project_kind(root: str | Path) -> str:
    path = Path(root).resolve()
    if (path / "pubspec.yaml").is_file():
        return "flutter"
    if (path / "package.json").is_file():
        return "node"
    if (path / "pyproject.toml").is_file() or (path / "pytest.ini").is_file():
        return "python"
    return "unknown"


def ensure_project_state(root: str | Path) -> ProjectState:
    path = Path(root).resolve()
    if not path.is_dir():
        raise ValueError(f"project folder does not exist: {path}")
    state_dir = path / ".molicoder"
    state_dir.mkdir(parents=True, exist_ok=True)
    for child in ("plans", "tasks", "runs", "logs", "failures", "skills"):
        (state_dir / child).mkdir(exist_ok=True)
    config_path = state_dir / "project.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"invalid MoliCoder project config: {config_path}") from exc
        if not isinstance(config, dict) or not isinstance(config.get("kind"), str):
            raise ValueError(f"invalid MoliCoder project config: {config_path}")
        detected_kind = detect_project_kind(path)
        if config["kind"] == "unknown" and detected_kind != "unknown":
            config["kind"] = detected_kind
            config["validation"] = _VALIDATION[detected_kind]
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="")
    else:
        kind = detect_project_kind(path)
        config = {
            "version": 1,
            "kind": kind,
            "validation": _VALIDATION[kind],
            "constraints": [],
        }
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="")
    instructions = state_dir / "instructions.md"
    if not instructions.exists():
        instructions.write_text(
            "# MoliCoder project instructions\n\nAdd project-specific rules here.\n",
            encoding="utf-8",
            newline="",
        )
    ignore = state_dir / ".gitignore"
    ignored = ("logs/", "plans/", "tasks/", "runs/", "failures/", "worktrees/")
    existing = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
    missing = [entry for entry in ignored if entry not in existing]
    if missing:
        ignore.write_text("\n".join(existing + missing) + "\n", encoding="utf-8", newline="")
    return ProjectState(path, str(config["kind"]), config)


def read_git_state(root: str | Path) -> GitState:
    path = Path(root).resolve()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    probe = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=creationflags,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return GitState("Not a Git repository", False, False)
    branch = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=creationflags,
    ).stdout.strip() or "Detached HEAD"
    dirty = bool(
        subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=creationflags,
        ).stdout.strip()
    )
    return GitState(branch, dirty, True)
