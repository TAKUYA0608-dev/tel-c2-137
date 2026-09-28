"""TEL-C2-137 — inner domain workflow graph (Cat 2).

Instantiated by EvidenceTimelineWorkflowGraphNode.get_subgraph() in graph.py. Linear topology with
per-node skip guards (the portable Cat 2 form; conditional edges don't propagate across the subgraph
boundary) — the proposal's workflow steps 3–6:

    START → time_reference_normalise → evidence_sequence_interpret → citation_anchor_retrieve
          → timeline_worksheet_compose → END

On rejected / 0-observation input, time_reference_normalise sets entry_count=0 (+ error_code);
evidence_sequence_interpret and citation_anchor_retrieve no-op and timeline_worksheet_compose emits the
out-of-scope safe answer — no fabricated chronology.
"""

from __future__ import annotations
from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState

from src.nodes.citation_anchor_retrieve_node import CitationAnchorRetrieveNode
from src.nodes.evidence_sequence_interpret_node import EvidenceSequenceInterpretNode
from src.nodes.time_reference_normalise_node import TimeReferenceNormaliseNode
from src.nodes.timeline_worksheet_compose_node import TimelineWorksheetComposeNode
from src.schemas.state import State


class ComplexFaultEvidenceTimelineWorkflow(BaseGraph):
    """Inner graph: normalise → interpret → cite → compose."""

    @property
    def name(self) -> str:
        return "ComplexFaultEvidenceTimelineWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract.
        self._nodes["time_reference_normalise"] = TimeReferenceNormaliseNode()
        self._nodes["evidence_sequence_interpret"] = EvidenceSequenceInterpretNode()
        self._nodes["citation_anchor_retrieve"] = CitationAnchorRetrieveNode()
        self._nodes["timeline_worksheet_compose"] = TimelineWorksheetComposeNode()

    def add_edges(self) -> None:
        # Static linear backbone; the 0-entry / rejected skip is handled by per-node guards.
        self._sg.add_edge(START, "time_reference_normalise")
        self._sg.add_edge("time_reference_normalise", "evidence_sequence_interpret")
        self._sg.add_edge("evidence_sequence_interpret", "citation_anchor_retrieve")
        self._sg.add_edge("citation_anchor_retrieve", "timeline_worksheet_compose")
        self._sg.add_edge("timeline_worksheet_compose", END)

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph ABC. Linear topology → not wired to a conditional edge."""
        if state.get("error_code") or state.get("entry_count", 0) == 0:
            return "timeline_worksheet_compose"
        return "evidence_sequence_interpret"

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            "output": state.get("result"),
            "status": state.get("status"),
            "entry_count": state.get("entry_count", 0),
            "gap_count": state.get("gap_count", 0),
            "human_review_flag": state.get("human_review_flag", False),
            "error_code": state.get("error_code"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
