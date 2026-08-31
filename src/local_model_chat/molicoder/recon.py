from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..attachments import AttachmentError, is_allowed, load_attachment
from .projectstate import detect_project_kind, read_git_state
from .tools import is_sensitive_path


_IGNORED_DIRS = {".git", ".molicoder", ".venv", "node_modules", "build", "dist", "coverage"}
_STOP_WORDS = {
    "add", "and", "change", "create", "fix", "for", "from", "into", "make", "support",
    "the", "this", "to", "update", "with",
}


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    path: str
    line_start: int
    line_end: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class ReconPacket:
    objective: str
    project_type: str
    architecture: tuple[str, ...]
    relevant_files: tuple[str, ...]
    relevant_symbols: tuple[str, ...]
    tests: tuple[str, ...]
    constraints: tuple[str, ...]
    risks: tuple[str, ...]
    open_questions: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]

    @classmethod
    def empty(cls, objective: str, project_type: str = "unknown") -> "ReconPacket":
        return cls(objective, project_type, (), (), (), (), (), (), (), ())

    def to_dict(self) -> dict:
        return asdict(self)

    def to_prompt(self) -> str:
        payload = self.to_dict()
        payload["evidence_refs"] = [
            {
                "reference": f"{ref.path}:{ref.line_start}-{ref.line_end}",
                "excerpt": ref.excerpt,
            }
            for ref in self.evidence_refs
        ]
        return json.dumps(payload, indent=2, ensure_ascii=False)


class Reconnaissance:
    def __init__(self, root: str | Path, *, max_files: int = 5000, max_evidence: int = 30) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"project root does not exist: {self.root}")
        self.max_files = max_files
        self.max_evidence = max_evidence

    def _files(self) -> list[Path]:
        files: list[Path] = []
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if any(part in _IGNORED_DIRS for part in relative.parts):
                continue
            if path.is_file() and is_allowed(path) and not is_sensitive_path(relative):
                files.append(path)
                if len(files) >= self.max_files:
                    break
        return files

    def build(self, objective: str) -> ReconPacket:
        terms = {
            term.lower()
            for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", objective)
            if term.lower() not in _STOP_WORDS
        }
        files = self._files()
        relative_names = {path: path.relative_to(self.root).as_posix() for path in files}
        tests = tuple(
            name for path, name in relative_names.items()
            if "test" in path.name.lower() or "tests" in path.parts
        )
        scored: list[tuple[int, str, Path]] = []
        evidence: list[EvidenceRef] = []
        symbols: set[str] = set()
        for path, relative in relative_names.items():
            lowered = relative.lower()
            score = sum(3 for term in terms if term in lowered)
            try:
                text = load_attachment(path).text
            except AttachmentError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                matched = [term for term in terms if term in line.lower()]
                if matched:
                    score += len(matched)
                    if len(evidence) < self.max_evidence:
                        evidence.append(EvidenceRef(relative, number, number, line.strip()[:300]))
                definition = re.search(r"\b(?:def|class|function|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
                if definition and any(term in definition.group(1).lower() for term in terms):
                    symbols.add(definition.group(1))
            if score:
                scored.append((score, relative, path))
        scored.sort(key=lambda item: (-item[0], item[1]))
        relevant = [item[1] for item in scored[:20]]
        for manifest in ("pyproject.toml", "package.json", "pubspec.yaml", "pytest.ini"):
            if (self.root / manifest).is_file() and manifest not in relevant:
                relevant.append(manifest)
        architecture = tuple(
            sorted(
                {path.relative_to(self.root).parts[0] for path in files if path.relative_to(self.root).parts}
            )[:30]
        )
        constraints_path = self.root / ".molicoder" / "instructions.md"
        constraints: tuple[str, ...] = ()
        if constraints_path.is_file():
            constraints = tuple(
                line.strip() for line in constraints_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        git = read_git_state(self.root)
        risks = []
        if git.available and git.dirty:
            risks.append("Working tree contains uncommitted changes.")
        project_type = detect_project_kind(self.root)
        if project_type == "unknown":
            risks.append("Project type and validation commands are unknown.")
        return ReconPacket(
            objective=objective,
            project_type=project_type,
            architecture=architecture,
            relevant_files=tuple(relevant),
            relevant_symbols=tuple(sorted(symbols)),
            tests=tests,
            constraints=constraints,
            risks=tuple(risks),
            open_questions=(),
            evidence_refs=tuple(evidence),
        )
