from __future__ import annotations

from typing import Protocol

from ..recon import ReconPacket
from .schema import Plan


class PlannerProvider(Protocol):
    provider: str

    def plan(self, packet: ReconPacket) -> Plan: ...
