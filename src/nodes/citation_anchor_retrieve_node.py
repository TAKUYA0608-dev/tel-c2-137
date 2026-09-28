"""TEL-C2-137 — inner workflow step 3 (SoT §4 step 5): `citation_anchor_retrieve`.

Deterministic anchoring only — **retrieval, never fabrication**. Each entry is anchored to the
S-1-resolved `source_ref` plus the *location* of the recorded statement (offset + length); the raw excerpt
is never carried (S-4 no-persist). An entry whose provenance did not resolve gets **no** span, which is
what makes the S-3 gate fail-closed rather than cosmetic.

Two-sided gap kinds must carry both sides so a contradiction / substitution / exemption is never presented
one-sided: `source_disagreement` → this observation **and** its counterpart; `substituted_action` /
`exemption_claimed` → this observation **and** the supporting record (checklist clause / exemption record).

`citation presence ≠ entailment`: a span proves the reference exists and is attached, not that its content
supports the assigned `gap_kind`. That judgement belongs to the named accountable network-evidence owner.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import EvidenceTimelineService
from src.utils.audit import emit_trace_event


class CitationAnchorRetrieveNode(FunctionNode):
    """Attach deterministic evidence spans (both sides for the two-sided gap kinds)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_code") or state.get("entry_count", 0) == 0:
            emit_trace_event(
                "citation_anchor_retrieve.skipped", {"reason": state.get("error_code") or "no_entries"}, state
            )
            return {}

        entries = json.loads(state.get("interpreted_entries") or "[]")
        by_ref = {e["observation_ref"]: e for e in entries}
        cited = []
        unsatisfied = 0
        for entry in entries:
            spans = EvidenceTimelineService.anchor(entry, by_ref)
            satisfied = EvidenceTimelineService.spans_satisfy_bounded_rule(entry["gap_kind"], spans)
            if not satisfied:
                unsatisfied += 1
            cited.append({**entry, "evidence_spans": spans, "bounded_rule_satisfied": satisfied})

        emit_trace_event(
            "citation_anchor_retrieve.complete",
            {
                "entries": len(cited),
                "spans": sum(len(e["evidence_spans"]) for e in cited),
                "entries_without_sufficient_spans": unsatisfied,
            },
            state,
        )
        return {"cited_entries": json.dumps(cited, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}
