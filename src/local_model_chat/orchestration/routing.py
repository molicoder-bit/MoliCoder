"""Evaluate work requirements against model evidence, independently of providers.

The request model describes the work. This policy matches that description to
advertised capabilities and observed results, then ranks suitable options by
cost. A model's name or the mere presence of read_file is not a difficulty test.
"""
from dataclasses import dataclass, field


ALIASES = {
    'file_read': 'read_file', 'project_read': 'read_file', 'filesystem_read': 'read_file',
    'file_write': 'write_file', 'file_edit': 'write_file', 'project_edit': 'write_file',
    'repository_search': 'search_code', 'code_understanding': 'source_reasoning',
    'coding': 'code_generation', 'validation': 'run_check', 'command_execution': 'run_command',
}
HOST_TOOLS = {'read_file', 'list_directory', 'search_code', 'project_status', 'write_file',
    'replace_in_file', 'create_directory', 'run_command', 'run_check'}
COST_ORDER = {'local': 0, 'free': 1, 'subscription': 2, 'metered': 3, 'unknown': 4}
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


@dataclass(frozen=True)
class WorkRequirements:
    capabilities: tuple[str, ...]
    reasoning: int
    source_reasoning: bool
    input_tokens: int = 0

    @classmethod
    def from_evaluation(cls, spec=None, *, input_tokens=0):
        spec = spec or {}
        capabilities = {ALIASES.get(c, c) for c in spec.get('required_capabilities', [])}
        capabilities.update(c['expected'] for c in spec.get('acceptance_checks', [])
            if c['kind'] == 'tool_evidence' and c['expected'] in HOST_TOOLS)
        semantic = 'source_reasoning' in capabilities or any(
            c['kind'] == 'semantic_review' for c in spec.get('acceptance_checks', []))
        reasoning = {'low': 1, 'medium': 2, 'high': 3}.get(spec.get('complexity'), 1)
        work = spec.get('work_type')
        if work in {'analysis', 'implementation'}: reasoning = max(2, reasoning)
        if work == 'planning': reasoning = max(3, reasoning)
        if work == 'analysis' and spec.get('intent') == 'investigation': semantic = True
        if semantic: reasoning = max(2, reasoning)
        if spec.get('intent') == 'build':
            capabilities.add('planning'); reasoning = max(3, reasoning)
        return cls(tuple(sorted(capabilities)), reasoning, semantic, input_tokens)


@dataclass(frozen=True)
class ModelEvidence:
    cost_class: str = 'unknown'
    cost_source: str = 'Price is unknown'
    # Relative descriptions are hints within an opaque subscription, never USD.
    cost_hint: int = 1
    capacity: int = 1
    capacity_source: str = 'Bounded structured-output trial; quality unqualified'
    effort_levels: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    context_window: int = 32000
    estimated_usd: float | None = None
    requires_access: bool = False


@dataclass(frozen=True)
class Suitability:
    eligible: bool
    score: int
    effort: str | None
    reasons: tuple[str, ...]
    sort_key: tuple = field(default_factory=tuple)


def assess(requirements, evidence, *, preferred=False):
    reasons = []
    eligible = True
    if requirements.input_tokens + 2400 > evidence.context_window * .95:
        eligible = False; reasons.append('Insufficient advertised context for the request and output reserve')
    # Host operations are supplied by MoliCoder; native tool support is not a
    # requirement for a model that can propose the validated JSON contract.
    required = set(requirements.capabilities) - HOST_TOOLS - {'tool_evidence', 'host_tools', 'text_generation'}
    supported = set(evidence.capabilities)
    unknown = required - supported - {'reasoning', 'source_reasoning', 'planning', 'code_generation'}
    if unknown:
        eligible = False; reasons.append('Unverified required capabilities: ' + ', '.join(sorted(unknown)))
    if evidence.capacity < requirements.reasoning:
        eligible = False; reasons.append('Insufficient reasoning evidence for this request')
    target = 'low' if requirements.reasoning <= 1 else 'medium' if requirements.reasoning == 2 else 'high'
    levels = [e for e in EFFORTS if e in evidence.effort_levels]
    effort = next((e for e in levels if EFFORTS.index(e) >= EFFORTS.index(target)), levels[-1] if levels else None)
    score = max(0, min(100, 70 + 10 * (evidence.capacity - requirements.reasoning))) if eligible else 0
    reasons.extend([evidence.capacity_source, evidence.cost_source])
    key = (COST_ORDER.get(evidence.cost_class, 4), evidence.estimated_usd if evidence.estimated_usd is not None else 0,
        evidence.cost_hint, not preferred, -score)
    return Suitability(eligible, score, effort, tuple(reasons), key)
