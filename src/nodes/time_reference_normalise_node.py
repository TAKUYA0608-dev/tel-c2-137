"""TEL-C2-137 — inner workflow step 1 (SoT §4 step 3): `time_reference_normalise`.

Deterministic time normalisation — the *Tool-equivalent* half of the template. Parses absolute timestamps,
unifies JST/UTC onto a single epoch, applies the seeded clock-skew tolerance and derives a sortable key.

**Relative, derived and unparseable references are never promoted to a confirmed instant** — they are
flagged only (proposal risk #2: fabricating a chronology). Sets `entry_count`; **0 usable observations
(rejected input, non-JSON text, or every row missing an observation identifier) routes to the out-of-scope
safe answer** — the agent never invents a timeline for evidence it did not receive.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import EvidenceTimelineService
from src.utils.audit import emit_trace_event


class TimeReferenceNormaliseNode(FunctionNode):
    """Normalise every recorded time reference onto a comparable epoch + sortable key."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # The packet arrives already validated, minimised and provenance-resolved by pre_process (S-1/S-2):
        # each `source_ref` is a grounded citation `src:<sha8>` or None (a forged surrogate was dropped at
        # S-1). Provenance is never re-resolved here — it is resolved exactly once, upstream.
        packet = json.loads(state.get("validated_input") or state.get("user_input") or "{}")
        if not isinstance(packet, dict):
            packet = {}
        canonical = json.dumps(packet, ensure_ascii=False)
        observations = packet.get("observations") if isinstance(packet.get("observations"), list) else []

        if state.get("error_code") or not observations:
            emit_trace_event(
                "time_reference_normalise.skipped", {"reason": state.get("error_code") or "no_observations"}, state
            )
            return {
                "validated_input": canonical,
                "normalised_entries": "[]",
                "entry_count": 0,
                "error_code": state.get("error_code") or "NO_EVIDENCE",
                "status": AgentStatus.SUCCESS.value,
            }

        entries = EvidenceTimelineService.normalise(observations)
        if not entries:
            emit_trace_event("time_reference_normalise.skipped", {"reason": "all_unreferenced"}, state)
            return {
                "validated_input": canonical,
                "normalised_entries": "[]",
                "entry_count": 0,
                "error_code": "NO_EVIDENCE",
                "status": AgentStatus.SUCCESS.value,
            }

        parse_distribution: dict[str, int] = {}
        for entry in entries:
            parse_distribution[entry["parse_status"]] = parse_distribution.get(entry["parse_status"], 0) + 1
        emit_trace_event(
            "time_reference_normalise.complete",
            {
                "supplied": len(observations),
                "normalised": len(entries),
                "parse_status_distribution": parse_distribution,
            },
            state,
        )
        return {
            "validated_input": canonical,
            "normalised_entries": json.dumps(entries, ensure_ascii=False),
            "entry_count": len(entries),
            "status": AgentStatus.SUCCESS.value,
        }
