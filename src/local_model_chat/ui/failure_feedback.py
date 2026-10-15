"""Plain-language failure summaries with a concrete recovery destination."""


def failure_feedback(reason):
    text = reason.lower()
    if any(word in text for word in ('outside', 'escapes', 'destination', 'parent folder', 'folder name')):
        return 'Target needs correction', 'The requested location was not changed. Review the exact destination or use a local folder action.', 'Edit request'
    if any(word in text for word in ('quota', 'rate limit', 'usage limit', 'budget')):
        return 'Usage limit reached', 'The request stopped. Review the limit or wait before choosing to retry.', 'Usage & limits'
    if any(word in text for word in ('auth', 'sign-in', 'login', 'credential')):
        return 'Account connection needed', 'Connect or sign in to the selected provider, then review the request again.', 'Models & providers'
    if any(word in text for word in ('approval', 'trust')):
        return 'Project permission needed', 'Review the project and provider permission before continuing.', 'Models & providers'
    if any(word in text for word in ('permission', 'access denied', 'denied')):
        return 'Permission denied', 'The requested change was not verified. Choose an accessible target or review project permission.', 'Edit request'
    if any(word in text for word in ('unavailable', 'not installed', 'connection', 'endpoint')):
        return 'Provider unavailable', 'Check the selected provider and connection before retrying.', 'Models & providers'
    if any(word in text for word in ('validation', 'test failed', 'check failed')):
        return 'Validation did not pass', 'Review the preserved changes and failed checks before continuing.', 'Validation'
    return 'Build stopped unexpectedly', 'The request did not produce a verified successful result. Review Activity before retrying.', 'Activity'
