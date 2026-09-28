"""TEL-C2-137 — Agent state (Telecom Complex-Fault Investigation Evidence Timeline Extractor, Cat 2).

ADR-005: State is a flat TypedDict — never a validation/BaseModel instance. Complex fields are stored as
JSON strings (``NotRequired[str]`` + ``# JSON:``); nodes ``json.dumps`` on write / ``json.loads`` on read.

Read-only / advisory: the agent ingests a **completed**, de-identified complex-fault investigation packet
(alarm references, traffic extracts, topology extracts, analyst notes, timestamp references) and produces a
**ComplexFaultEvidenceTimelineWorksheet** — a cited, ``needs-review`` evidence timeline. It never touches
live network data, never correlates alarms, never diagnoses root cause, never recommends remediation,
never dispatches crews, never contacts subscribers, and never drafts or files the 電気通信事業法 第28条
重大事故報告. Every gap and every downstream judgement is resolved by the named accountable
network-evidence owner.

All agent-specific fields are NotRequired (populated progressively; absent at empty-start invoke).
"""

from __future__ import annotations


from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for the complex-fault evidence-timeline extraction workflow."""

    # ── pre_process (InvestigationPacketIngest S-1 + NetworkDataMinimise S-2) ─────────────────
    # JSON: minimised packet {investigation_id, packet_version_ref, observations[], evidence_checklist[]}
    # — identifiers tokenized, subscriber / commercial / site fields dropped or banded.
    validated_input: str
    input_format: str  # "json" | "text" | "empty" | "rejected"
    enriched_context: str  # JSON: {source, channel} (read-only caller context)

    # ── inner workflow (normalise → interpret → cite → compose) ───────────────────────────────
    normalised_entries: str  # JSON: [{observation_ref, sort_key, epoch_utc, parse_status, ...}]
    entry_count: int  # observations normalised (0 → out-of-scope safe answer)
    interpreted_entries: str  # JSON: [+ time_reference_kind, gap_kind, human_review_flag]
    cited_entries: str  # JSON: [+ evidence_spans[] (deterministic anchors, never invented)]
    result: str  # JSON: assembled ComplexFaultEvidenceTimelineWorksheet
    gap_count: int  # gap_items raised for owner resolution
    human_review_flag: bool  # True once any entry hits containment / taxonomy-outside handling

    # ── post_process (OutputSanitise — S-3 output gate + S-4 no-persist audit) ────────────────
    formatted_output: str  # JSON: final response envelope (worksheet + disclaimer)
    disclaimer: str  # mandatory needs-review / not-a-diagnosis disclaimer
    audit_logged: bool  # True once the terminal audit event is emitted

    # ── degraded-path signalling (SUCCESS + error_code, never status=ERROR) ───────────────────
    # INPUT_REJECTED | INJECTION_REJECTED | INPUT_TOO_LONG | NO_EVIDENCE | CITATION_INCOMPLETE
    error_code: str
    error_message: str  # operator-facing detail
