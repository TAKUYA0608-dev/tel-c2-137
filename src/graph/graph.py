"""TEL-C2-137 — outer graph (Cat 2).

AgentBaseGraph 5-node backbone; domain complexity lives in the `main` slot via
EvidenceTimelineWorkflowGraphNode (a GraphNode wrapping the inner ComplexFaultEvidenceTimelineWorkflow).

    START → initialize → pre_process → main(GraphNode) → post_process → finalize → END

The GraphNode is declared **here**, not under `src/nodes/`: PB-6 instantiates every class it finds in
`src/nodes/` with a bare state, which a GraphNode cannot survive (`KeyError: 'session_id'`). There is no
`MainNode` alias for the same reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState

from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import ComplexFaultEvidenceTimelineWorkflow


class EvidenceTimelineWorkflowGraphNode(GraphNode):
    """`main` slot — wraps the inner ComplexFaultEvidenceTimelineWorkflow (composition criterion #9)."""

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False
    # Class-level cache (SDK how-to: compose-agents-graphnode.md) — assigned on the CLASS, not the
    # instance, so it is NOT mutable node-instance state (CoE §9 composition consistency). Set once,
    # shared read-only across invocations.
    _subgraph: ClassVar["ComplexFaultEvidenceTimelineWorkflow | None"] = None

    def get_subgraph(self) -> "ComplexFaultEvidenceTimelineWorkflow":
        # Build/compile the inner workflow once and cache it on the CLASS (per the SDK how-to). Assigning
        # to EvidenceTimelineWorkflowGraphNode._subgraph (not self._subgraph) avoids mutable node-instance
        # state; BaseGraph.invoke()'s _ensure_compiled is idempotent → no per-request DAG compile.
        if self._subgraph is None:
            from src.graph.domain_workflow_graph import ComplexFaultEvidenceTimelineWorkflow

            EvidenceTimelineWorkflowGraphNode._subgraph = ComplexFaultEvidenceTimelineWorkflow(
                config=self._parent_config()
            )
        return cast("ComplexFaultEvidenceTimelineWorkflow", self._subgraph)

    def extract_input(self, state: AgentState) -> str:
        return cast(str, state.get("validated_input", state.get("user_input", "")))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        # error_code is OUTER-first: a pre-stage rejection (INJECTION_REJECTED / INPUT_TOO_LONG) must
        # survive to the terminal S-4 audit. The inner workflow runs on the discarded body and would
        # otherwise overwrite it with NO_EVIDENCE (extract_input passes only validated_input, so the inner
        # state never sees the outer error_code).
        return {
            "result": sub_result.get("output"),
            "entry_count": sub_result.get("entry_count", state.get("entry_count", 0)),
            "gap_count": sub_result.get("gap_count", state.get("gap_count", 0)),
            "human_review_flag": sub_result.get("human_review_flag", state.get("human_review_flag", False)),
            "error_code": state.get("error_code") or sub_result.get("error_code"),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> dict[str, Any]:
        return {}


class Graph(AgentBaseGraph):
    """Outer Cat 2 graph for TEL-C2-137."""

    @property
    def name(self) -> str:
        return "ComplexFaultEvidenceTimelineAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = EvidenceTimelineWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: dict[str, Any]) -> dict[str, Any]:
        """Framework default, plus the guarantee that a success is never empty.

        The Marketplace runner rejects a successful invocation whose output is
        missing — verified on a deployed Pod — and a degraded run
        (SUCCESS + error_code) produces no artefact for the framework default
        to surface. Report the degradation instead: this states what happened,
        it does not invent an answer.

        Only on SUCCESS. A request refused by the framework's S-2 gate (status
        ERROR) must keep publishing nothing — answering a hostile input with a
        notice would undo the refusal, and the runner treats a non-success
        invocation as a failure regardless, so there is nothing to rescue.
        """
        out: dict[str, Any] = super().get_output(state)
        if not out.get("output") and str(state.get("status", "")).lower().endswith("success"):
            code = state.get("error_code") or "NO_CONTENT"
            out["output"] = (
                "This request could not be completed "
                f"(error_code={code}). No content was produced; "
                "see error_code and error_log for the degradation cause."
            )
        return out


# server.py / AgentRegistry expect a module-level alias for the agent class.
ComplexFaultEvidenceTimelineAgent = Graph
