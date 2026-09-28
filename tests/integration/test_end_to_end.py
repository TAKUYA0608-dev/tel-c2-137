# TEL-C2-137 — Integration: pre → inner workflow (linear) → post, and the real outer invoke path

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph
from src.nodes.citation_anchor_retrieve_node import CitationAnchorRetrieveNode
from src.nodes.evidence_sequence_interpret_node import EvidenceSequenceInterpretNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.time_reference_normalise_node import TimeReferenceNormaliseNode
from src.nodes.timeline_worksheet_compose_node import TimelineWorksheetComposeNode

_SUCCESS = AgentStatus.SUCCESS.value
_CONTEXT = {
    "channel": "evidence_console",
    "attested_feeds": ["alarm_export", "analyst_note_export", "topology_extract",
                       "incident_record", "evidence_repository"],
}

# A completed, de-identified complex-fault investigation packet with the full spread of gap kinds.
_PACKET = {
    "investigation_id": "INC-2026-0731-01",
    "packet_version_ref": "pkt-v3",
    "worksheet_schema_version": "wks-1.2",
    "evidence_owner": "noc-evidence-lead",
    "observations": [
        {"observation_id": "alarm-1", "kind": "alarm", "event_ref": "ring-loss",
         "time_reference": {"value": "2026-07-31T03:12:00+09:00", "kind": "absolute",
                            "clock_source": "ntp"},
         "statement": "LOS alarm raised on the aggregation ring.",
         "site_ref": "AGG-RING-EAST", "traffic_volume": 4200,
         "source": "alarm_export:row-1024"},
        {"observation_id": "note-1", "kind": "note", "event_ref": "ring-loss",
         "time_reference": {"value": "2026-07-31T03:41:00+09:00", "kind": "absolute",
                            "clock_source": "ntp"},
         "statement": "Analyst recorded the ring loss starting at 03:41 per the console clock.",
         "source": "analyst_note_export:span-7"},
        {"observation_id": "topo-1", "kind": "topology",
         "time_reference": {"value": "2026-07-31T04:05:00", "kind": "absolute",
                            "clock_source": "unknown"},
         "statement": "Protection path recorded as active on the east ring.",
         "source": "topology_extract:path-11"},
        {"observation_id": "chk-1", "kind": "checklist",
         "time_reference": {"value": "T+15m", "kind": "relative", "clock_source": "manual"},
         "statement": "代替実施: ring bypass applied in place of the documented protection switch.",
         "checklist_ref": "CHK-03", "supporting_source": "evidence_repository:CHK-03",
         "source": "analyst_note_export:span-9"},
        {"observation_id": "traf-1", "kind": "traffic", "event_ref": "throughput-drop",
         "time_reference": {"value": "2026-07-31T05:00:00+09:00", "kind": "absolute",
                            "clock_source": "gps"},
         "statement": "Throughput extract recorded for the east ring window.",
         "traffic_volume": 180, "source": "alarm_export:row-1099"},
        {"observation_id": "hand-1", "kind": "handoff",
         "time_reference": {"value": "2026-07-31T06:00:00+09:00", "kind": "absolute",
                            "clock_source": "ntp"},
         "statement": "Investigation handed to the following shift.",
         "source": "incident_record:hand-1"},
    ],
    "evidence_checklist": [
        {"clause_ref": "CHK-03", "requirement": "Confirm the protection switch completed"},
        {"clause_ref": "CHK-07", "requirement": "Record the restoration timestamp"},
    ],
}


def _run(user_input: str, context: dict | None = None) -> dict:
    state: dict = {"user_input": user_input,
                   "input_context": _CONTEXT if context is None else context,
                   "node_history": [], "error_log": []}
    state.update(PreProcessNode().execute(state) or {})
    for node in (TimeReferenceNormaliseNode(), EvidenceSequenceInterpretNode(),
                 CitationAnchorRetrieveNode(), TimelineWorksheetComposeNode()):
        state.update(node.execute(state) or {})
    state.update(PostProcessNode().execute(state) or {})
    return state


class TestEndToEnd:
    def test_worksheet_with_ordered_entries_and_citations(self):
        state = _run(json.dumps(_PACKET, ensure_ascii=False))
        assert state["status"] == _SUCCESS and state["audit_logged"] is True
        env = json.loads(state["formatted_output"])
        assert env["status_kind"] == "evidence_timeline_worksheet"
        assert [e["seq"] for e in env["ordered_entries"]] == [1, 2, 3, 4, 5, 6]
        assert env["citations"] and env["citation_complete"] is True
        assert env["human_review"]["required"] is True
        assert "needs-review" in env["disclaimer"]

    def test_all_gap_kinds_present_and_within_the_closed_taxonomy(self):
        env = json.loads(_run(json.dumps(_PACKET, ensure_ascii=False))["formatted_output"])
        kinds = {e["gap_kind"] for e in env["ordered_entries"]}
        assert {"source_disagreement", "clock_provenance_uncertain", "substituted_action",
                "handoff_owner_unstated", "ordered_confirmed"} <= kinds
        from src.services.service import EVIDENCE_TIMELINE_TAXONOMY
        assert kinds <= set(EVIDENCE_TIMELINE_TAXONOMY)

    def test_time_range_is_unbounded_when_a_relative_reference_exists(self):
        env = json.loads(_run(json.dumps(_PACKET, ensure_ascii=False))["formatted_output"])
        rng = env["evidence_time_range"]
        assert rng["bounded"] is False and rng["entries_without_time"] == 1
        assert rng["start_utc"] and rng["end_utc"]

    def test_identifiers_and_site_never_leak(self):
        state = _run(json.dumps(_PACKET, ensure_ascii=False))
        for raw in ("INC-2026-0731-01", "AGG-RING-EAST", "noc-evidence-lead", "alarm-1", "CHK-03"):
            assert raw not in state["formatted_output"]
            assert raw not in state["validated_input"]

    def test_exact_traffic_value_never_leaks(self):
        state = _run(json.dumps(_PACKET, ensure_ascii=False))
        assert "4200" not in state["formatted_output"]
        env = json.loads(state["formatted_output"])
        assert any(e["traffic_band"] == "high" for e in env["ordered_entries"])

    def test_gap_items_carry_an_owner_and_citations(self):
        env = json.loads(_run(json.dumps(_PACKET, ensure_ascii=False))["formatted_output"])
        assert env["gap_items"]
        for gap in env["gap_items"]:
            assert gap["cited_source_ref"]
            assert gap["owner_ref"] is not None
            assert gap["bounded_rule_satisfied"] is True

    def test_out_of_scope_safe(self):
        env = json.loads(_run("複合障害の証跡タイムラインを教えて")["formatted_output"])
        assert env["status_kind"] == "out_of_scope"
        assert env["citations"] == [] and "needs-review" in env["disclaimer"]

    def test_empty_degrades_but_audits(self):
        state = _run("   ")
        assert state["status"] == _SUCCESS and state["audit_logged"] is True
        assert json.loads(state["formatted_output"])["status_kind"] == "out_of_scope"

    def test_packet_with_unauthorized_sources_is_withheld(self):
        packet = json.loads(json.dumps(_PACKET))
        for row in packet["observations"]:
            row["source"] = "random_blog:row-1"
            row.pop("supporting_source", None)
        state = _run(json.dumps(packet, ensure_ascii=False))
        env = json.loads(state["formatted_output"])
        assert env["status_kind"] == "needs_review" and env["ordered_entries"] == []
        assert state["error_code"] == "CITATION_INCOMPLETE" and state["status"] == _SUCCESS

    def test_deployed_call_path_produces_a_worksheet(self):
        """★ The way `src/api/server.py` actually calls the agent: no `input_context`.

        The previous revision gated citations on an `input_context` attestation that nothing populates
        on that path, so this packet returned needs_review with an empty worksheet — the agent was inert
        in production. This asserts the deployed call path stays functional.
        """
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        out = Graph().invoke(json.dumps(_PACKET, ensure_ascii=False), ctx=ctx)
        assert out["status"] == _SUCCESS
        env = json.loads(out["output"])
        assert env["status_kind"] == "evidence_timeline_worksheet"
        assert len(env["ordered_entries"]) == 6
        assert env["citation_basis"] == "caller_declared_authorized_source"

    def test_real_invoke_end_to_end(self):
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        out = Graph().invoke(json.dumps(_PACKET, ensure_ascii=False), ctx=ctx, input_context=_CONTEXT)
        assert out["status"] == _SUCCESS
        assert "PostProcessNode" in out["node_history"]
        env = json.loads(out["output"])
        assert env["status_kind"] == "evidence_timeline_worksheet"
        assert len(env["ordered_entries"]) == 6
        assert env["citation_complete"] is True
