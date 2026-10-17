"""Planning selection and actionable preflight failures, without inference."""
from .providers import ProviderUnavailable


class ProviderSetupRequired(ProviderUnavailable):
    def __init__(self, providers, detail):
        self.providers = tuple(providers)
        super().__init__(detail)


def unavailable_message(name, status):
    if not status.installed:
        return f'{name.title()} is not installed. Use Set up / sign in to install it, then retry Generate plan.'
    if not status.authenticated:
        return f'{name.title()} is not signed in. Use Set up / sign in to connect your account and verify model access, then retry Generate plan.'
    return f'{name.title()} sign-in could not be verified. Use Set up / sign in to test the connection before retrying.'


def select_planner(store, project, client, create_provider, *, input_tokens=0):
    from ..orchestration.catalog import CapabilityCatalogService
    routes, excluded = CapabilityCatalogService(store, client).planning_routes(project, input_tokens=input_tokens)
    ready, unverified, failures, checked = [], [], [], {}
    for route in routes:
        candidate = create_provider(route.provider, model=route.model, project_root=project.path)
        if route.effort:
            candidate.effort = route.effort
        if route.provider not in checked:
            checked[route.provider] = candidate.probe()
        status = checked[route.provider]
        if status.installed and status.authenticated and status.auth_verified:
            ready.append((route, candidate))
        elif status.installed and status.authenticated and not status.auth_verified:
            # Some CLIs cannot report auth independently of loading unrelated
            # user config. Their bounded planning call is the authoritative
            # access check; never confuse unknown with a known signed-out state.
            unverified.append((route, candidate))
        elif route.provider not in [name for name, _ in failures]:
            failures.append((route.provider, unavailable_message(route.provider, status)))
    ready.extend(unverified)
    if not ready:
        trusted = store.get_project_preferences(project.id).trusted_providers
        raise ProviderSetupRequired([name for name, _ in failures] or [p for p in trusted if p in {'codex', 'claude', 'copilot'}],
            '\n\n'.join(message for _, message in failures) or
            'No allowed model has sufficient reasoning evidence for planning. Set up a frontier provider '
            '(Codex or Claude), refresh the model catalog, or remove an unsuitable model override. '
            'Automatic planning excludes unqualified models and the Astra/Fable families.')
    return ready, failures
