"""Describe inference routes without confusing runtime health with model identity."""
from ..config import MODEL_PRESETS


LOCAL_BUILD_MODEL = MODEL_PRESETS['molicoder:latest'].name


def _model_label(model, source='configured'):
    if not model:
        return 'CLI default · model not reported'
    return f'{model} ({source})'


def _build_selection(w):
    if w.build_owner.currentData() == 'kandev':
        return 'Kandev · agent/model managed in Kandev · model not reported'
    from ..file_actions import empty_file_intent
    from pathlib import Path
    project = w.registry.selected()
    if empty_file_intent(w.build_request.toPlainText(), Path(project.path) if project else None):
        return 'Local filesystem · no model · 0 AI calls'
    strategy = w.build_strategy.currentData()
    if strategy == 'LOCAL_OFFLINE':
        return f'Ollama (local) · {LOCAL_BUILD_MODEL}'
    provider = str(w.build_provider.currentData() or 'codex')
    preferences = w.store.get_project_preferences(w.current_project_id) if w.current_project_id else None
    model = preferences.model_overrides.get(provider, '') if preferences else ''
    route = f'{w.build_provider.currentText()} · {_model_label(model)}'
    if strategy == 'HYBRID':
        route += f' · Hybrid: Ollama / {LOCAL_BUILD_MODEL} for eligible local steps'
    return route


def refresh_route_identity(w):
    if not hasattr(w, 'route_identity') or not hasattr(w, 'build_tabs'):
        return
    if w.custom_api_worker is not None:
        offering = w.inference_registry.get_offering(w.custom_api_worker.offering_id)
        route = (f'{offering.provider_id} · {offering.model_id} (configured)'
                 if offering else 'provider/model unavailable')
        text = f'API test running · {route}'
    elif w.view_stack.currentWidget() == w.chat_view:
        if getattr(w.chat_worker, 'local_read', False):
            w.route_identity.setText('Reading local folder · No AI model used')
            w.route_identity.setToolTip(str(w.chat_worker.path))
            return
        model = getattr(w.chat_worker, 'model', w.preset.name)
        text = f'Chat {"running" if w.chat_worker is not None else "selected"} · Ollama (local) · {model}'
        facts = getattr(w, '_model_facts', {}).get(model, {})
        if facts.get('family'):
            text += f" · runtime family: {facts['family']} {facts.get('parameter_size', '')}"
        if w.chat_worker is None:
            from .workspace import last_receipt
            receipt = last_receipt(w)
            if receipt:
                last = ('No AI model used' if receipt.get('model_source') == 'no_inference' else
                        f"{receipt.get('provider', 'Unknown provider')} · {receipt.get('requested_model', 'Model not reported')} ({receipt.get('model_source', 'unknown')})")
                text = f'Last reply: {last}\nNext reply: Ollama (local) · {model}'
    elif w.build_tabs.currentIndex() == 1:
        text = 'Kandev board · agent/model managed in Kandev · model not reported'
    elif w.orchestration_worker is not None:
        route = getattr(w, '_active_build_route', '') or _build_selection(w)
        text = f'Build running · {route}'
    else:
        text = f'Build selected · {_build_selection(w)}'
        if w.current_project_id:
            status = w.store.current_project_run_status(w.current_project_id)
            if status and status.run_id:
                from ..work_state import WorkSnapshot
                snapshot = WorkSnapshot.from_run(w.store, status.run_id)
                call = snapshot.last_completed_call
                if call:
                    identity = f"{str(call.get('provider', 'Unknown')).title()} · {_model_label(call.get('model'), call.get('model_source', 'reported/configured'))}"
                    text = f'Last call: {identity}\nNext Build: {_build_selection(w)}'
    w.route_identity.setText(text)
    w.route_identity.setToolTip(text + '\nSelected/configured models are requests; reported models come from provider results. '
                               'Runtime health is in Performance. Subscription CLI usage can include internal model routing.')


def observe_build_route(w, kind, payload):
    """Follow real handoffs, clearing the previous model at every new call."""
    if kind == 'run_started':
        w._active_build_route = _build_selection(w)
        preferences = w.store.get_project_preferences(w.current_project_id) if w.current_project_id else None
        w._build_route_models = dict(preferences.model_overrides) if preferences else {}
        w._build_route_local = payload.get('strategy', payload.get('mode')) == 'LOCAL_OFFLINE'
        if w._build_route_local:
            w._active_build_route = f'Ollama (local) · {LOCAL_BUILD_MODEL}'
    elif kind == 'local_attempt_started':
        w._active_build_route = f'Ollama (local) · {LOCAL_BUILD_MODEL}'
    elif kind == 'provider_output' and isinstance(payload.get('event'), dict):
        event = payload['event']
        candidates = [event, event.get('result'), event.get('message')]
        model = next((item['model'] for item in reversed(candidates) if isinstance(item, dict) and isinstance(item.get('model'), str)), '')
        if model:
            w._active_build_route = f"Calling: {str(payload.get('provider', 'Unknown')).title()} · {_model_label(model, 'reported')}"
    elif kind in {'provider_started', 'provider_attempt_started', 'provider_takeover', 'provider_finished'}:
        if getattr(w, '_build_route_local', False):
            w._active_build_route = f'Ollama (local) · {LOCAL_BUILD_MODEL}'
        elif payload.get('provider'):
            provider = str(payload['provider'])
            model = getattr(w, '_build_route_models', {}).get(provider, '')
            source = 'configured'
            if kind == 'provider_finished':
                model = payload.get('model') or model
                source = payload.get('model_source', 'reported/configured')
            prefix = 'Last call' if kind == 'provider_finished' else 'Calling'
            w._active_build_route = f'{prefix}: {provider.title()} · {_model_label(model, source)}'
    refresh_route_identity(w)
