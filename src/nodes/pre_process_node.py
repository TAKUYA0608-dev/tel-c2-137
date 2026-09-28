"""TEL-C2-137 — pre_process node: `InvestigationPacketIngest` (S-1) + `NetworkDataMinimise` (S-2).

**S-1 (ingest / validation).** Accepts a completed complex-fault investigation packet as structured JSON
(`observations[]` plus `investigation_id` / `packet_version_ref` / `worksheet_schema_version` /
`evidence_checklist[]`) or NL text, normalises it (NFKC + control strip), enforces the size cap, and
extracts the analysis slots. Per the SoT, **injection detection is deliberately NOT attributed to S-1** —
the boundary reject below is the S-2 hard gate, and per-note instruction-shaped text is handled by the
independent pre-LLM containment layer in `evidence_sequence_interpret`.

**S-2 (pre-LLM minimisation).** Every downstream node sees a *minimised* packet, built
**whitelist-by-construction**: only the named fields below are copied, so an arbitrary extra caller field
(a subscriber MSISDN, an exact site coordinate, an internal note naming a crew member) is never carried
into State, a citation, or the output.

- **identifiers** (`investigation_id`, `packet_version_ref`, `observation_id`, `event_ref`,
  `handoff_owner`, `checklist_ref`, `site_ref`, `evidence_owner`) → **unconditionally** tokenized to an
  opaque `<kind>:<sha8>` surrogate. No syntactic passthrough: a bare name (`Alice`, `John.Smith`,
  `TaroYamada`) is opaque like any other value.
- **commercial traffic magnitudes** → reduced to a band (`low`/`mid`/`high`); the exact value is dropped.
- **analyst free text** → hygiened (credential / My-Number / email / phone redaction) and length-capped. It
  is retained only so the bounded rules can scan it; the worksheet carries the *span location*, never the
  excerpt (S-4 no-persist).
- **time reference values** → constrained to a timestamp charset; anything else is blanked, so free text
  cannot ride in through the timestamp field.
- **provenance** (`source`, `supporting_source`) → resolved **exactly once, here**, and only when the
  label names an authorized system of record **with a reference part**. Free text, an unauthorized
  namespace, a bare namespace, or a value merely shaped like a surrogate (`src:<hex>`) → `None` → S-3
  blocks the worksheet as `CITATION_INCOMPLETE`. See `service.CITATION_BASIS` for what the resulting
  citation asserts (a caller *declaration*, not verification that the record exists).

**Degraded contract (SDK 1.0.0).** Injection markers / oversize / empty never set `status=ERROR`. They
return `status=SUCCESS + error_code` (`INJECTION_REJECTED` / `INPUT_TOO_LONG` / `INPUT_REJECTED`) and
**discard the offending body**, so `main` / `post_process` still run (disclaimer + S-3 + S-4). The `@final`
framework hook is not invoked by the local stub framework, so `execute()` re-checks the same S-2 conditions.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import (
    OBSERVATION_KINDS,
    hygiene_text,
    resolve_provenance,
    safe_identifier,
    traffic_band,
)
from src.utils.audit import emit_trace_event

_MAX_INPUT = 200_000  # an investigation packet carries many observations → larger cap than a chat prompt
# S-2 boundary reject: classic prompt-injection preambles aimed at the request envelope. Softer directive
# phrasing inside an analyst note is NOT rejected here (the note is evidence) — it is contained downstream.
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "system prompt",
    "you are now",
    "###system",
    "<|im_start|>",
)
_REJECT_CODES = frozenset({"INJECTION_REJECTED", "INPUT_TOO_LONG"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Timestamp charset — digits and separators only. A name cannot match, so free text cannot ride in through
# the time field; a non-matching value is blanked and the entry becomes `timestamp_missing`.
_TIME_VALUE = re.compile(r"^[0-9TZtz:+\-. /]{1,40}$")
# Schema/version labels pass through only on an exact, strictly structured match that a personal name can
# never satisfy (a digit run is mandatory); anything else is reported as "unknown".
_SCHEMA_VERSION = re.compile(r"^(?:[A-Za-z]{1,8}-)?[Vv]?\d+(?:\.\d+){0,3}$")
_MAX_REQUIREMENT = 200


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _opt_id(value: Any, kind: str) -> str | None:
    """Tokenize an optional caller identifier; absent stays absent."""
    text = str(value or "").strip()
    return safe_identifier(text, kind) if text else None


def _safe_version(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SCHEMA_VERSION.match(text) else "unknown"


def _time_reference(raw: Any) -> dict[str, Any]:
    """Project a caller time reference onto the closed schema, blanking non-timestamp free text."""
    if not isinstance(raw, dict):
        raw = {"value": raw}
    value = str(raw.get("value") or "").strip()
    kind = str(raw.get("kind") or "").strip().lower()
    clock = str(raw.get("clock_source") or "").strip().lower()
    return {
        "value": value if _TIME_VALUE.match(value) else "",
        "kind": kind,
        "clock_source": clock,
    }


class PreProcessNode(FunctionNode):
    """Validate the investigation packet, minimise it (S-2), and resolve provenance exactly once."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject_code(self, raw: str) -> str | None:
        if len(raw) > _MAX_INPUT:
            return "INPUT_TOO_LONG"
        if any(marker in _nfkc(raw).lower() for marker in _INJECTION_MARKERS):
            return "INJECTION_REJECTED"
        return None

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2 boundary checks: size cap + prompt-injection markers on the request envelope.

        SDK 1.0.0 contract: MUST NOT raise, and MUST NOT set `status=ERROR` (that would short-circuit the
        pipeline past `post_process`). A rejection is surfaced as a degraded `SUCCESS + error_code`; the
        offending body is discarded by `execute()`.
        """
        raw = state.get("user_input", "") or ""
        code = self._reject_code(raw)
        if code:
            out = dict(state)
            out["error_code"] = code
            return out
        return dict(state)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("user_input", "") or ""
        input_context = state.get("input_context", {})  # read-only [C1] — platform/gateway supplied
        enriched = json.dumps(
            {"source": "ComplexFaultEvidenceTimelineAgent", "channel": (input_context or {}).get("channel", "unknown")},
            ensure_ascii=False,
        )

        # Degrade on rejection: the S-2 hook may already have set error_code (real SDK); re-detect here
        # because the local stub framework does not invoke the hook. Discard the offending body entirely.
        prior = state.get("error_code")
        code = prior if prior in _REJECT_CODES else self._reject_code(raw)
        if code:
            emit_trace_event("investigation_packet_ingest.rejected", {"reason": code}, state)
            return {
                "validated_input": "{}",
                "input_format": "rejected",
                "enriched_context": enriched,
                "user_input": "",
                "error_code": code,
                "status": AgentStatus.SUCCESS.value,
            }

        if not raw.strip():
            emit_trace_event("investigation_packet_ingest.rejected", {"reason": "empty_input"}, state)
            return {
                "validated_input": "{}",
                "input_format": "empty",
                "enriched_context": enriched,
                "error_code": "INPUT_REJECTED",
                "status": AgentStatus.SUCCESS.value,
            }

        packet, fmt = self._parse(_CONTROL.sub("", _nfkc(raw)))
        cited = sum(1 for o in packet["observations"] if o["source_ref"])
        emit_trace_event(
            "network_data_minimise.applied",
            {
                "input_format": fmt,
                "observation_count": len(packet["observations"]),
                "resolved_source_ref_count": cited,
                "checklist_clause_count": len(packet["evidence_checklist"]),
            },
            state,
        )
        return {
            "validated_input": json.dumps(packet, ensure_ascii=False),
            "input_format": fmt,
            "enriched_context": enriched,
            "status": AgentStatus.SUCCESS.value,
        }

    # ── whitelist-by-construction projection ────────────────────────────────────────────────────
    def _parse(self, text: str) -> tuple[dict[str, Any], str]:
        empty: dict[str, Any] = {
            "investigation_id": None,
            "packet_version_ref": None,
            "worksheet_schema_version": "unknown",
            "evidence_owner_ref": None,
            "observations": [],
            "evidence_checklist": [],
        }
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return empty, "text"
        if isinstance(obj, list):  # bare observations array
            return {**empty, "observations": self._observations(obj)}, "json"
        if not isinstance(obj, dict):
            return empty, "text"
        return {
            "investigation_id": _opt_id(obj.get("investigation_id"), "inv"),
            "packet_version_ref": _opt_id(obj.get("packet_version_ref"), "pkt"),
            "worksheet_schema_version": _safe_version(obj.get("worksheet_schema_version")),
            "evidence_owner_ref": _opt_id(obj.get("evidence_owner"), "owner"),
            "observations": self._observations(obj.get("observations")),
            "evidence_checklist": self._checklist(obj.get("evidence_checklist")),
        }, "json"

    @staticmethod
    def _observations(raw: Any) -> list[dict[str, Any]]:
        rows = raw if isinstance(raw, list) else []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id = str(row.get("observation_id") or row.get("id") or "").strip()
            if not raw_id:
                continue  # an observation without an identifier cannot be placed on a timeline
            kind = str(row.get("kind") or "").strip().lower()
            statement = hygiene_text(str(row.get("statement") or row.get("note") or ""))
            out.append(
                {
                    "observation_ref": safe_identifier(raw_id, "obs"),
                    "kind": kind if kind in OBSERVATION_KINDS else "unknown",
                    "event_ref": _opt_id(row.get("event_ref"), "evt"),
                    "time_reference": _time_reference(row.get("time_reference")),
                    "statement": statement,
                    "handoff_owner_ref": _opt_id(row.get("handoff_owner"), "owner"),
                    "checklist_ref": _opt_id(row.get("checklist_ref"), "chk"),
                    "site_ref": _opt_id(row.get("site_ref") or row.get("site"), "site"),
                    "traffic_band": (
                        traffic_band(row.get("traffic_volume")) if row.get("traffic_volume") is not None else "unknown"
                    ),
                    # Provenance — resolved exactly once, here (authorized namespace + reference part).
                    "source_ref": resolve_provenance(row.get("source")),
                    "supporting_source_ref": resolve_provenance(row.get("supporting_source")),
                }
            )
        return out

    @staticmethod
    def _checklist(raw: Any) -> list[dict[str, Any]]:
        rows = raw if isinstance(raw, list) else []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            clause = str(row.get("clause_ref") or "").strip()
            if not clause:
                continue
            out.append(
                {
                    "clause_ref": safe_identifier(clause, "chk"),
                    "requirement": hygiene_text(str(row.get("requirement") or ""))[:_MAX_REQUIREMENT],
                }
            )
        return out
