"""TEL-C2-137 — post_process node: `OutputSanitise` (SoT §4 step 7 — S-3 output gate + S-4 no-persist).

**S-3, fail-closed.** A grounded worksheet is presented only when citation completeness holds:

1. every `ordered_entry` carries at least one span with a resolved `source_ref`;
2. every `gap_item` carries at least one `cited_source_ref`; and
3. every **two-sided** gap kind (`source_disagreement` / `substituted_action` / `exemption_claimed`)
   carries **both** sides' spans.

If any of these fails, the worksheet body is **withheld** and the response degrades to a `needs_review`
envelope with `error_code=CITATION_INCOMPLETE` — still `SUCCESS`, so the disclaimer and the terminal S-4
audit still run.

Also: whole-report redaction (credential / My-Number / email / phone / company-name) as defense-in-depth,
and neutralisation of any residual instruction-shaped marker so a quoted analyst note can never read as an
instruction in the response.

**S-4, no-persist.** The audit event carries the rule version, counts and error codes only — never the raw
packet, the analyst narrative, a timestamp value or any prompt text.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, cast

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import (
    CITATION_BASIS,
    COMPANY_RE,
    CONTAINMENT_MARKERS,
    CREDENTIAL_RE,
    EMAIL_RE,
    MY_NUMBER_RE,
    PHONE_RE,
    REDACTED,
    RULE_VERSION,
    TWO_SIDED_GAP_KINDS,
)
from src.utils.audit import emit_trace_event

_DISCLAIMER = (
    "本ワークシートは提供された完了済み調査パケットの記載事実のみを出典付きで時系列整列した "
    "needs-review の DRAFT 証跡整理資料であり、障害の原因診断・復旧判断・対外報告（電気通信事業法 "
    "第28条 重大事故報告等）ではありません。相対時刻・派生時刻・clock provenance 不明の時刻は確定値に "
    "昇格させていません。**S-3 が機械的に保証するのは citation の completeness（各 entry / gap に "
    "認可済み system of record を名指しした出典参照が付いていること）だけです。出典として名指しされた "
    "レコードが当該システムに実在するか（authority）は本エージェントでは検証できません — 出典は "
    "提出者の申告であり、citation_basis に明示しています。entailment（その出典が付与された分類を "
    "実際に裏付けること）も保証しません。"
    "entailment の判断および各 gap の解消・最終判断は、named accountable network-evidence owner "
    "（NOC 証跡整備責任者）が行ってください。**本エージェントは live network data に接続せず、alarm の "
    "correlate・root cause 診断・remediation 推奨・要員 dispatch・加入者連絡・対外報告の作成/提出は "
    "一切行いません。"
)

_CITATION_INCOMPLETE_MSG = (
    "ワークシートの一部エントリまたは gap について、認可済み system of record を名指しした出典参照が "
    "確認できなかったため、根拠不十分な時系列の提示を差し控えました。source_disagreement / "
    "substituted_action / exemption_claimed は両側の出典 span が揃っている必要があります。"
    "`<認可済みシステム>:<レコード参照>` の形式で出典を付与のうえ再実行してください"
    "（レコードの実在性は本エージェントでは検証しません。citation_basis を参照）。"
)
_NEEDS_REVIEW_NOTE = (
    "Not every ordered entry / gap item carries a source reference naming an authorized system of record; "
    "the evidence timeline worksheet is withheld pending those declared references and authorized "
    "network-evidence owner review. Record existence is not verified by this agent (see citation_basis)."
)

# S-3 defense-in-depth: re-redact secrets / contact info / company names that could have leaked into any
# free-text field, applied to the whole serialized worksheet before it becomes the output envelope.
_REDACTORS = (CREDENTIAL_RE, MY_NUMBER_RE, EMAIL_RE, PHONE_RE, COMPANY_RE)
_NEUTRALISED = "[NEUTRALISED]"
_MARKER_RE = re.compile("|".join(re.escape(m) for m in CONTAINMENT_MARKERS), re.IGNORECASE)


def _sanitise(worksheet: dict[str, Any]) -> dict[str, Any]:
    """Serialize → redact secret/contact/company patterns → neutralise instruction markers → deserialize."""
    text = json.dumps(worksheet, ensure_ascii=False)
    for pattern in _REDACTORS:
        text = pattern.sub(REDACTED, text)
    text = _MARKER_RE.sub(_NEUTRALISED, text)
    return cast(dict[str, Any], json.loads(text))


def _citation_complete(worksheet: dict[str, Any]) -> bool:
    """Fail-closed citation completeness, including the two-sided bounded acceptance rule."""
    for entry in worksheet.get("ordered_entries", []):
        spans = [s for s in entry.get("evidence_span", []) if s.get("source_ref")]
        if not spans:
            return False
        if entry.get("gap_kind") in TWO_SIDED_GAP_KINDS and len(spans) < 2:
            return False
    for gap in worksheet.get("gap_items", []):
        refs = [r for r in gap.get("cited_source_ref", []) if r]
        if not refs:
            return False
        if gap.get("gap_kind") in TWO_SIDED_GAP_KINDS and len(refs) < 2:
            return False
    return bool(worksheet.get("citations"))


class PostProcessNode(FunctionNode):
    """Enforce citation completeness, redact leakage, append the mandatory disclaimer, emit the S-4 audit."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3 preservation check: the mandatory needs-review disclaimer must be in the output envelope.

        SDK 1.0.0 contract: receives the **result dict from `execute()`**; returns the (possibly filtered)
        result. MAY raise to block an output missing the mandatory disclaimer.
        """
        out = result.get("formatted_output", "")
        if out and "needs-review" not in out and "DRAFT" not in out:
            raise ValueError("S-3: mandatory needs-review disclaimer missing from output")
        return dict(result)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        worksheet: dict[str, Any] = _sanitise(json.loads(state.get("result", "{}") or "{}"))

        grounded = worksheet.get("status_kind") == "evidence_timeline_worksheet"
        ordered_entries = worksheet.get("ordered_entries", [])
        gap_items = worksheet.get("gap_items", [])
        citation_complete = (not grounded) or _citation_complete(worksheet)

        # S-3 fail-closed: an ungrounded worksheet (an uncited entry / gap, or a two-sided gap kind missing
        # a side) is never presented. Degrade to a safe needs-review answer (SUCCESS + error_code), withhold
        # the worksheet body, and still run the disclaimer + terminal S-4 audit.
        if grounded and not citation_complete:
            error_code = state.get("error_code") or "CITATION_INCOMPLETE"
            blocked: dict[str, Any] = {
                "status_kind": "needs_review",
                "investigation_id": worksheet.get("investigation_id"),
                "packet_version_ref": worksheet.get("packet_version_ref"),
                "ordered_entries": [],  # incomplete worksheet body withheld
                "gap_items": [],
                "evidence_time_range": {
                    "start_utc": None,
                    "end_utc": None,
                    "bounded": False,
                    "entries_without_time": 0,
                },
                "human_review": {"required": True, "status": "pending_owner_review", "note": _NEEDS_REVIEW_NOTE},
                "citations": [],
                "citation_complete": False,
                "citation_basis": CITATION_BASIS,
                "message": _CITATION_INCOMPLETE_MSG,
                "disclaimer": _DISCLAIMER,
            }
            emit_trace_event(
                "output_sanitise.citation_blocked",
                {
                    "ordered_entry_count": len(ordered_entries),
                    "gap_count": len(gap_items),
                    "rule_version": RULE_VERSION,
                    "error_code": error_code,
                },
                state,
            )
            return {
                "formatted_output": json.dumps(blocked, ensure_ascii=False),
                "disclaimer": _DISCLAIMER,
                "audit_logged": True,
                "error_code": error_code,
                "status": AgentStatus.SUCCESS.value,
            }

        formatted = {
            "status_kind": worksheet.get("status_kind"),
            "investigation_id": worksheet.get("investigation_id"),
            "packet_version_ref": worksheet.get("packet_version_ref"),
            "worksheet_schema_version": worksheet.get("worksheet_schema_version"),
            "rule_version": worksheet.get("rule_version", RULE_VERSION),
            "ordered_entries": ordered_entries,
            "evidence_time_range": worksheet.get(
                "evidence_time_range", {"start_utc": None, "end_utc": None, "bounded": False, "entries_without_time": 0}
            ),
            "gap_items": gap_items,
            "gap_distribution": worksheet.get("gap_distribution", {}),
            "human_review": {
                "required": True,
                "status": "pending_owner_review",
                "note": "Every gap and every downstream judgement is resolved by the named "
                "accountable network-evidence owner; no entry is promoted to "
                "confirmed by this agent.",
            },
            "citations": worksheet.get("citations", []),
            "citation_complete": citation_complete,
            # ★ What a citation asserts. Observations and their `source` labels both arrive in the
            # caller's request body and the template cannot confirm a cited record exists, so the
            # envelope states the basis rather than letting "citation" imply verification. See
            # service.CITATION_BASIS; closing the gap is a platform dependency (SoT §12-B).
            "citation_basis": CITATION_BASIS,
            "message": worksheet.get("message"),
            "disclaimer": _DISCLAIMER,
        }
        emit_trace_event(
            "output_sanitise.complete",
            {
                "status_kind": worksheet.get("status_kind"),
                "ordered_entry_count": len(ordered_entries),
                "gap_count": len(gap_items),
                "citation_complete": citation_complete,
                "human_review_flag": bool(state.get("human_review_flag", False)),
                "rule_version": RULE_VERSION,
                "error_code": state.get("error_code"),
            },
            state,
        )
        return {
            "formatted_output": json.dumps(formatted, ensure_ascii=False),
            "disclaimer": _DISCLAIMER,
            "audit_logged": True,
            "status": AgentStatus.SUCCESS.value,
        }
