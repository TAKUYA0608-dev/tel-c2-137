"""TEL-C2-137 — inner workflow step 2 (SoT §4 step 4): `evidence_sequence_interpret`.

The **Agent-value** step: bounded interpretation of ambiguous evidence and placement into the *seeded
closed* evidence-timeline taxonomy — derived/relative times, unverified clock provenance, cross-source
disagreement on the same event reference, substituted actions, claimed exemptions, indeterminate ordering
and unstated handoff owners.

**pre-LLM source-text containment — an independent layer, separate from S-2 and from S-3.** The analyst
statement is treated as *quoted data: evidence only, never instructions*. The output schema is restricted
to the approved taxonomy plus cited references, so nothing the note "asks for" can change the shape of the
result. Instruction-shaped or taxonomy-outside text is **not interpreted**: the entry is routed to
`gap_kind=out_of_scope` with `human_review_flag=True` so a human decides what it means.

The node never asserts which side of a contradiction is correct, that a substituted step *was* performed,
or that a claimed exemption *is* valid.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import RULE_VERSION, EvidenceTimelineService
from src.utils.audit import emit_trace_event


class EvidenceSequenceInterpretNode(FunctionNode):
    """Place each normalised entry into the seeded closed taxonomy (bounded, contained, deterministic)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_code") or state.get("entry_count", 0) == 0:
            emit_trace_event(
                "evidence_sequence_interpret.skipped", {"reason": state.get("error_code") or "no_entries"}, state
            )
            return {}

        entries = json.loads(state.get("normalised_entries") or "[]")
        disagreements = EvidenceTimelineService.detect_disagreements(entries)
        indeterminate = EvidenceTimelineService.detect_indeterminate(entries)
        interpreted = [EvidenceTimelineService.classify(e, disagreements, indeterminate) for e in entries]

        distribution: dict[str, int] = {}
        for entry in interpreted:
            distribution[entry["gap_kind"]] = distribution.get(entry["gap_kind"], 0) + 1
        contained = sum(1 for e in interpreted if e["human_review_flag"])
        emit_trace_event(
            "evidence_sequence_interpret.complete",
            {
                "entries": len(interpreted),
                "gap_distribution": distribution,
                "contained_entries": contained,
                "rule_version": RULE_VERSION,
            },
            state,
        )
        return {
            "interpreted_entries": json.dumps(interpreted, ensure_ascii=False),
            "human_review_flag": contained > 0,
            "status": AgentStatus.SUCCESS.value,
        }
