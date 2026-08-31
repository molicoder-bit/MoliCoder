from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    keywords: tuple[str, ...]
    content: str


class SkillLibrary:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / ".molicoder" / "skills"
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _name(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise ValueError("skill name must contain only letters, digits, dashes, and underscores")
        return value

    def save(self, name: str, content: str, keywords: tuple[str, ...]) -> Skill:
        safe_name = self._name(name)
        normalized = tuple(sorted({keyword.strip().lower() for keyword in keywords if keyword.strip()}))
        if not content.strip() or not normalized:
            raise ValueError("skills require content and at least one keyword")
        path = self.directory / f"{safe_name}.md"
        path.write_text(
            f"<!-- keywords: {', '.join(normalized)} -->\n\n{content.strip()}\n",
            encoding="utf-8",
            newline="",
        )
        return Skill(safe_name, normalized, content.strip())

    def list(self) -> tuple[Skill, ...]:
        skills: list[Skill] = []
        for path in sorted(self.directory.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            match = re.match(r"<!--\s*keywords:\s*(.*?)\s*-->\s*", text, re.IGNORECASE)
            if not match:
                continue
            keywords = tuple(keyword.strip().lower() for keyword in match.group(1).split(",") if keyword.strip())
            content = text[match.end():].strip()
            if keywords and content:
                skills.append(Skill(path.stem, keywords, content))
        return tuple(skills)

    def match(self, objective: str, *, limit: int = 3) -> tuple[Skill, ...]:
        lowered = objective.lower()
        ranked = [
            (sum(1 for keyword in skill.keywords if keyword in lowered), skill)
            for skill in self.list()
        ]
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        return tuple(skill for score, skill in ranked if score > 0)[:limit]

    def augment(self, objective: str) -> str:
        skills = self.match(objective)
        if not skills:
            return objective
        guidance = "\n\n".join(f"Skill: {skill.name}\n{skill.content}" for skill in skills)
        return f"{objective}\n\nReusable project guidance:\n{guidance}"
