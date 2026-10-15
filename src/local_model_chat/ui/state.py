from dataclasses import dataclass, field

@dataclass
class UiWorkspaceState:
    mode: str = 'chat'
    drafts: dict = field(default_factory=dict)
