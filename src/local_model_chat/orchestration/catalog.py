"""Cached runtime facts and compatible route selection; refresh performs no inference."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import threading
import os
from pathlib import Path
import re
import subprocess

from ..inference.adapters.ollama import ensure_local_offering, local_offering_id
from ..inference.discovery import discover_local_tools
from ..inference.registry import ProviderRegistry
from ..config import MODEL_PRESETS


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    runtime: str
    context_window: int = 16384
    output_tokens: int = 2048
    effort: str | None = None
    reason: str = ""
    offering_id: str = ""


class CapabilityCatalogService:
    KEY = "orchestration.catalog.v1"
    _refresh_lock = threading.Lock()

    def __init__(self, store, client):
        self.store, self.client = store, client
        self.registry = ProviderRegistry(store)

    def cached(self):
        try:
            return json.loads(self.store.get_setting(self.KEY, "{}"))
        except (ValueError, TypeError):
            return {}

    def route_records(self):
        try:
            value = json.loads(self.store.get_setting('orchestration.route_records.v1', '{}'))
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def route_key(route):
        return route.offering_id or f'{route.provider}:{route.model}'

    def record_outcome(self, route, status, *, spec=None, error='', verified=False):
        from ..inference.fallback import classify_provider_failure
        from datetime import timedelta
        with self._refresh_lock:
            records = self.route_records()
            key = self.route_key(route)
            record = records.get(key, {'passed': 0, 'failed': 0, 'consecutive_failures': 0})
            record.update(last_status=status, checked_at=datetime.now(timezone.utc).isoformat(),
                          last_intent=(spec or {}).get('intent'), source='host request result')
            intent = (spec or {}).get('intent')
            if status == 'completed':
                record['passed'] += 1
                record['consecutive_failures'] = 0
                record.pop('cooldown_until', None)
                if intent:
                    record.setdefault('intent_failures', {})[intent] = 0
                    record.setdefault('intent_cooldowns', {}).pop(intent, None)
                if verified and spec:
                    samples = record.setdefault('verified_samples', {})
                    import hashlib
                    samples[hashlib.sha256(spec['objective'].encode()).hexdigest()[:20]] = spec['intent']
                    record['verified_samples'] = dict(list(samples.items())[-30:])
            else:
                record['failed'] += 1
                record['consecutive_failures'] += 1
                action = classify_provider_failure(status, error)
                if intent and action.category == 'execution':
                    failures = record.setdefault('intent_failures', {})
                    failures[intent] = failures.get(intent, 0) + 1
                    if failures[intent] >= 3:
                        record.setdefault('intent_cooldowns', {})[intent] = (
                            datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
                    seconds = 0
                else:
                    seconds = action.cooldown_seconds or (120 if action.category in {'auth', 'model'} else
                        60 if record['consecutive_failures'] >= 3 else 0)
                record.update(last_error=str(error)[:500], failure_category=action.category)
                if seconds:
                    record['cooldown_until'] = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            records[key] = record
            self.store.set_setting('orchestration.route_records.v1', json.dumps(records))

    @staticmethod
    def codex_models():
        """Use the CLI's cached advertised models; never read account identity/credentials."""
        path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'models_cache.json'
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            models = []
            for model in payload.get('models', []):
                levels = [item['effort'] for item in model.get('supported_reasoning_levels', []) if isinstance(item, dict) and 'effort' in item]
                if not model.get('slug') or model.get('visibility') == 'hide':
                    continue
                models.append({'model': model['slug'], 'effort_levels': levels,
                    'context_window': model.get('context_window'), 'source': 'Codex CLI models_cache.json',
                    'checked_at': payload.get('fetched_at'), 'default_effort': model.get('default_reasoning_level')})
            return models
        except (OSError, ValueError, TypeError):
            return []

    @classmethod
    def planner_default(cls):
        # Catalog order is the provider's preference. User excluded the extreme families.
        return next((m for m in cls.codex_models() if 'medium' in m['effort_levels']
                     and not any(name in m['model'].lower() for name in ('astra', 'fable'))), None)

    @staticmethod
    def runtime_models(provider, binary, *, refresh=False):
        """Parse advertised model facts, discarding headers, credentials and options."""
        command = [str(binary), '--pure', 'models', provider, '--verbose']
        if refresh:
            command.append('--refresh')
        process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode:
            raise ValueError(provider + ' model discovery failed')
        rows = []
        decoder = json.JSONDecoder()
        for match in re.finditer(r'(?m)^([^\s{}]+/[^\s{}]+)\r?\n', process.stdout[:4_000_000]):
            try:
                model, _ = decoder.raw_decode(process.stdout[match.end():].lstrip())
                rows.append({'model': match[1], 'capabilities': model.get('capabilities', {}),
                    'cost': model.get('cost', {}), 'limits': model.get('limit', {}),
                    'effort_levels': list(model.get('variants', {})), 'source': provider + ' models --verbose',
                    'checked_at': datetime.now(timezone.utc).isoformat()})
            except (ValueError, TypeError, AttributeError):
                continue
        return rows

    def refresh(self, *, discover_runtimes=True):
        # Serialize refreshes from startup, settings and CLI; readers use the last snapshot.
        with self._refresh_lock:
            previous = self.cached()
            snapshot = dict(previous, revision=datetime.now(timezone.utc).isoformat(), stale=False, error="")
            models = []
            try:
                tags = self.client.tags()
                installed = {str(tag.get("name")) for tag in tags}
                for tag in tags:
                    model = str(tag.get("name", ""))
                    if not model or any(word in model.lower() for word in ("embed", "vector")):
                        continue
                    preset = MODEL_PRESETS.get(model, MODEL_PRESETS["molicoder:latest"])
                    offering_id = ensure_local_offering(self.registry, model,
                        context_window=preset.context_window, max_output_tokens=preset.max_output_tokens)
                    offering = self.registry.get_offering(offering_id)
                    if offering.status != "disabled":
                        self.registry.set_offering_state(offering_id, status="eligible", health_state="healthy",
                            actor="catalog-refresh", reason="Installed local model; structured protocol checked at invocation")
                    previous_model = next((m for m in previous.get('models', [])
                        if m.get('digest') == tag.get('digest') and m.get('model') == model), {})
                    metadata = previous_model.get('advertised', {})
                    if discover_runtimes and not metadata and hasattr(self.client, 'show'):
                        try:
                            shown = self.client._request('/api/show', {'model': model}, timeout=2) if hasattr(self.client, '_request') else self.client.show(model)
                            info = shown.get('model_info', {})
                            windows = [v for k, v in info.items() if k.endswith('.context_length') and type(v) is int and v > 0]
                            metadata = {'context_window': max(windows) if windows else None,
                                'capabilities': shown.get('capabilities', []), 'source': 'Ollama /api/show',
                                'checked_at': datetime.now(timezone.utc).isoformat()}
                        except Exception:
                            pass
                    window = min(preset.context_window, metadata.get('context_window') or preset.context_window)
                    models.append({"model": model, "digest": tag.get("digest", model),
                        "offering_id": offering_id, "context_window": window, 'advertised': metadata,
                        'context_source': 'Application inference ceiling, bounded by advertised context when known',
                        "output_tokens": min(preset.max_output_tokens, 2400), "effort_levels": [],
                        "effort_source": "not advertised", "tools": "host JSON protocol; native support unknown",
                        "quality": "qualification required; attempts checked against host evidence"})
                for offering in self.registry.list_offerings():
                    if offering.provider_id == "ollama-local" and offering.model_id not in installed and offering.status != "disabled":
                        self.registry.set_offering_state(offering.id, status="unavailable", health_state="unavailable",
                            actor="catalog-refresh", reason="Model absent from successful discovery")
                snapshot["models"] = models
            except Exception as exc:
                snapshot.update(stale=True, error=str(exc)[:500])
            if discover_runtimes:
                snapshot["runtimes"] = [asdict(t) for t in discover_local_tools()]
                from ..molicoder.providers import create_provider
                snapshot['runtime_models'] = dict(previous.get('runtime_models', {}))
                for runtime in ('opencode', 'kilo'):
                    if not any(t['tool_id'] == runtime and t['installed'] for t in snapshot['runtimes']):
                        continue
                    binary = create_provider(runtime).discover_binary()
                    if binary:
                        try:
                            snapshot['runtime_models'][runtime] = self.runtime_models(runtime, binary, refresh=True)
                        except Exception:
                            snapshot['runtime_models'][runtime + '_stale'] = True
            snapshot['configured_offerings'] = [asdict(o) for o in self.registry.list_offerings()
                if o.provider_id != 'ollama-local']
            snapshot['cli_models'] = {'codex': self.codex_models()}
            self.store.set_setting(self.KEY, json.dumps(snapshot))
            return snapshot

    def routes(self, project, preferred_model, *, allow_external=True, spec=None):
        facts = self.cached()
        routes, excluded, digests = [], [], set()
        # Selection uses installation and enabled state; no alphabetic alias retry on identical weights.
        try:
            qualification = json.loads(self.store.get_setting("orchestration.qualification.v1", "{}"))
            if not isinstance(qualification, dict):
                qualification = {}
        except (ValueError, TypeError):
            qualification = {}
        models = sorted(facts.get("models", []), key=lambda m: (m["model"] != preferred_model,
            -qualification.get(m["model"], {}).get("passed", 0), m["model"]))
        for item in models:
            record = qualification.get(item["model"], {})
            if item["model"] != preferred_model and not record.get("enabled_for_fallback", False):
                excluded.append({"model": item["model"], "reason": "Not qualified for automatic fallback"})
                continue
            offering = self.registry.get_offering(item["offering_id"])
            if offering is None or offering.status in {"disabled", "unavailable", "deprecated"}:
                excluded.append({"model": item["model"], "reason": "Offering disabled or unavailable"})
                continue
            if item["digest"] in digests:
                continue
            digests.add(item["digest"])
            routes.append(Route("ollama", item["model"], "internal", item["context_window"], item["output_tokens"],
                reason="Installed local candidate using host tools and verified structured output"))
        if allow_external and project and self.store.get_setting(f"repository.external_fallback.{project.id}", "true") == "true":
            from ..inference.onboarding import is_api_offering
            from ..inference.eligibility import EligibilityRequest, evaluate_eligibility
            from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
            for offering in self.registry.list_offerings():
                if not is_api_offering(offering.id):
                    continue
                permission = AuthorizationPolicy(self.store).check(ActionRequest(project.id, 'inference_route', offering.id,
                    ActionRisk.FINANCIAL if offering.offering_class in {'metered', 'trial'} else ActionRisk.EXTERNAL))
                eligible = evaluate_eligibility(offering, EligibilityRequest('private', (), unattended=True,
                    paid_allowed=permission.allowed), self.registry)
                if not permission.allowed or not eligible.eligible:
                    excluded.append({'offering': offering.id, 'reason': permission.reason if not permission.allowed else '; '.join(eligible.reasons)})
                    continue
                levels = str(offering.capabilities.get('reasoning_effort_levels', '')).split(',')
                effort = ('medium' if 'medium' in levels and offering.capabilities.get('reasoning_effort_parameter') == 'reasoning_effort' else None)
                window = offering.capabilities.get('context_window')
                output = offering.capabilities.get('max_output_tokens')
                if type(window) is int and type(output) is int:
                    routes.append(Route(offering.provider_id, offering.model_id, 'api', window, min(output, 2400),
                        effort, 'Enabled authorized API offering with known limits; policy and budget checked at dispatch', offering.id))
            preferences = self.store.get_project_preferences(project.id)
            for provider in dict.fromkeys([preferences.provider, *preferences.trusted_providers]):
                if provider in preferences.trusted_providers and provider in {"codex", "claude", "copilot", "opencode", "kilo"}:
                    model = preferences.model_overrides.get(provider, '')
                    if not model and provider in {'opencode', 'kilo'}:
                        from ..molicoder.providers import create_provider
                        model = create_provider(provider).model
                    default = self.planner_default() if provider == 'codex' and not model else None
                    routes.append(Route(provider, model or (default['model'] if default else ''), provider,
                        32000, 2400, 'medium' if default else None,
                        reason="Project-authorized coding runtime; advertised normal reasoning effort when available"))
        records = self.route_records()
        eligible_routes = []
        for route in routes:
            record = records.get(self.route_key(route), {})
            until = record.get('cooldown_until')
            intent_until = record.get('intent_cooldowns', {}).get((spec or {}).get('intent'))
            # Interpret older semantic-failure cooldowns by their recorded family,
            # so upgrading cannot disable General chat after a code-answer failure.
            if until and record.get('failure_category') == 'execution' and record.get('last_intent'):
                if (spec or {}).get('intent') == record['last_intent']:
                    intent_until = max(intent_until or '', until)
                until = None
            if until and until > datetime.now(timezone.utc).isoformat():
                excluded.append({'provider': route.provider, 'model': route.model, 'reason': 'Temporary failure cooldown', 'until': until})
            elif intent_until and intent_until > datetime.now(timezone.utc).isoformat():
                excluded.append({'provider': route.provider, 'model': route.model,
                    'reason': 'Temporary task-family failure cooldown', 'intent': spec['intent'], 'until': intent_until})
            else:
                eligible_routes.append(route)
        routes = eligible_routes
        # Reserve one of three slots for a trusted fallback even when many local aliases exist.
        locals_ = [r for r in routes if r.provider == "ollama"]
        external = [r for r in routes if r.provider != "ollama"]
        external.sort(key=lambda r: (r.provider not in {'opencode', 'kilo'}, r.runtime != 'api'))
        if spec and (spec['complexity'] in {'medium', 'high'} or spec['intent'] == 'build'):
            external.sort(key=lambda r: r.provider not in {'codex', 'claude', 'copilot'})
            # Reasoning-heavy work goes to a capable configured route; local promotion
            # requires explicit task-family qualification rather than a model-name guess.
            qualified = [r for r in locals_ if spec['intent'] in qualification.get(r.model, {}).get('qualified_intents', [])]
            return (external[:2] + qualified[:1]), excluded
        if external:
            if external[0].provider in {'codex', 'claude', 'copilot'}:
                return (locals_[:2] + external[:max(1, 3 - len(locals_[:2]))])[:3], excluded
            chosen = locals_[:1]
            if external:
                chosen.append(external[0])
            strong = next((r for r in external[1:] if r.provider in {'codex', 'claude', 'copilot'}), None)
            chosen.extend([strong] if strong else external[1:2])
            return chosen[:3], excluded
        return locals_[:3], excluded
