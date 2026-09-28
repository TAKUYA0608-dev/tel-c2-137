"""TEL-C2-137 — inner workflow step 4 (SoT §4 step 6): `timeline_worksheet_compose`.

Composes the **ComplexFaultEvidenceTimelineWorksheet** deliverable: `ordered_entries[seq,
stated_observation_ref, time_reference_kind, evidence_span, gap_kind, handoff_owner_ref]`, the fixed
`evidence_time_range`, and `gap_items[gap_kind, cited_source_ref, owner_ref]`.

The worksheet is fixed to `needs_review`: no entry is ever promoted to *confirmed*, and no downstream
assertion is produced — the agent presents the recorded chronology and its cited gaps and stops. Every gap
carries an `owner_ref` for the named accountable network-evidence owner to resolve. On the
0-entry / rejected branch it emits the out-of-scope safe answer instead of an invented timeline.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import EvidenceTimelineService
from src.utils.audit import emit_trace_event

_OUT_OF_SCOPE = (
    "整列可能な調査パケットの観測記録が入力に見つかりませんでした。"
    "observations 配列に observation_id と time_reference / source を含む JSON をご指定いただくか、"
    "対象の investigation_id と packet_version_ref を明確にしてください。"
    "本エージェントは live network data には接続せず、完了済み調査パケットの記載事実のみを整列します。"
)


class TimelineWorksheetComposeNode(FunctionNode):
    """Compose the cited, needs-review evidence timeline worksheet (or the safe answer on 0 entries)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        entries = json.loads(state.get("cited_entries") or "[]")
        if state.get("error_code") or not entries:
            emit_trace_event(
                "timeline_worksheet_compose.safe", {"reason": state.get("error_code") or "no_entries"}, state
            )
            worksheet: dict[str, Any] = {
                "status_kind": "out_of_scope",
                "message": _OUT_OF_SCOPE,
                "ordered_entries": [],
                "gap_items": [],
                "citations": [],
                "evidence_time_range": {
                    "start_utc": None,
                    "end_utc": None,
                    "bounded": False,
                    "entries_without_time": 0,
                },
                "needs_review": True,
            }
            return {
                "result": json.dumps(worksheet, ensure_ascii=False),
                "gap_count": 0,
                "status": AgentStatus.SUCCESS.value,
            }

        packet = json.loads(state.get("validated_input") or "{}")
        if not isinstance(packet, dict):
            packet = {}
        worksheet = EvidenceTimelineService.compose(entries, packet, packet.get("evidence_owner_ref"))
        emit_trace_event(
            "timeline_worksheet_compose.complete",
            {
                "ordered_entry_count": len(worksheet["ordered_entries"]),
                "gap_count": len(worksheet["gap_items"]),
                "citation_count": len(worksheet["citations"]),
                "time_range_bounded": worksheet["evidence_time_range"]["bounded"],
            },
            state,
        )
        return {
            "result": json.dumps(worksheet, ensure_ascii=False),
            "gap_count": len(worksheet["gap_items"]),
            "status": AgentStatus.SUCCESS.value,
        }
