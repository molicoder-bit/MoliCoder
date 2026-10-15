"""Claims about OS protections the application actually provides today."""

from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlatformSecurity:
    system: str
    credential_backend: str | None
    strict_agent_sandbox: bool
    metered_api_allowed: bool
    reason: str


def platform_security(system: str | None = None) -> PlatformSecurity:
    system = system or platform.system()
    if system == "Windows":
        return PlatformSecurity(
            system, "Windows DPAPI", False, True,
            "User-scoped DPAPI is available for provider keys; opaque CLI native tools "
            "are not confined by a verified MoliCoder sandbox.",
        )
    return PlatformSecurity(
        system, None, False, False,
        "No verified encrypted provider-key backend or strict CLI sandbox on this platform.",
    )
