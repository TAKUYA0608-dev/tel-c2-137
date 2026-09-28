"""TEL-C2-137 — deterministic domain services (no framework imports, no LLM).

`EvidenceTimelineService` normalises the recorded time references of a *completed* complex-fault
investigation packet (absolute parse, JST/UTC unification to an epoch, clock-skew tolerance, sortable key),
places ambiguous evidence into the **seeded closed evidence-timeline taxonomy**, anchors each entry to a
deterministic evidence span, and composes the `ComplexFaultEvidenceTimelineWorksheet`.

Everything here is deterministic and auditable — `datetime` parsing, epoch arithmetic, threshold banding,
a seeded marker vocabulary and keyed composition. There is **no LLM**. Relative / derived / unparseable
time references are never promoted to a confirmed value; they are labelled and handed to the named
accountable network-evidence owner. Seeded thresholds, vocabularies and the taxonomy are overridable by
CoE without touching node logic.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Two SEPARATE concerns — do not conflate them:
#   (1) PRIVACY (safe_identifier): every caller identifier is UNCONDITIONALLY tokenized to a
#       deterministic opaque surrogate `<kind>:<sha8>`, so PII — including a bare name with no spaces or
#       symbols (`Alice` / `John.Smith` / `TaroYamada`) — can never reach a citation or the output. A
#       syntactic "looks like a safe id" allowlist does NOT satisfy this. Tokenizing is a privacy measure;
#       it asserts nothing about whether the value is authorized.
#   (2) PROVENANCE (resolve_provenance): a caller `source` becomes a CITATION only when it names an
#       authorized system of record WITH a reference part. Anything else yields NO citation, and S-3
#       blocks the worksheet as CITATION_INCOMPLETE (fail-closed). "Tokenized" is never sufficient for a
#       citation. What the citation does and does not assert: see CITATION_BASIS below.
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# Authorized evidence registry: the carrier systems of record a network-evidence owner trusts as
# verifiable sources for a completed investigation packet. This is the deploying org's / CoE's SEMANTIC
# registry of authorized systems — not a syntactic character class. ★ Membership is NOT authority: it
# says the caller named a system this deployment recognises, not that the cited record exists there.
# See CITATION_BASIS below — that gap is a platform dependency, not something checked here.
AUTHORIZED_EVIDENCE_SYSTEMS = frozenset(
    {
        "alarm_export",
        "alarm",
        "fault_management",
        "nms",
        "ems",
        "oss",
        "traffic_extract",
        "traffic",
        "pm_export",
        "performance_export",
        "counter_export",
        "topology_extract",
        "topology",
        "inventory",
        "cmdb",
        "netbox",
        "investigation_packet",
        "packet_section",
        "analyst_note_export",
        "note_export",
        "incident_record",
        "itsm",
        "servicenow",
        "ticketing",
        "evidence_repository",
        "system_of_record",
        "sor",
        "authorized_feed",
    }
)

# ── seeded evidence-timeline taxonomy (closed set — no free generation) ──────────────────────────
EVIDENCE_TIMELINE_TAXONOMY: dict[str, str] = {
    "ordered_confirmed": "Time reference parsed, clock provenance trusted, ordering unambiguous",
    "time_reference_derived": "Time is relative or derived — not promoted to a confirmed value",
    "clock_provenance_uncertain": "Absolute time present but the clock source is unstated or untrusted",
    "timestamp_missing": "No usable time reference was recorded for this observation",
    "source_ref_missing": "No authorized system-of-record reference was declared for this observation",
    "source_disagreement": "Two sources state different times for the same event reference",
    "substituted_action": "The note claims an alternative action instead of the checklist step",
    "exemption_claimed": "The note claims a monitoring / procedural exemption",
    "sequence_indeterminate": "Ordering cannot be determined within the clock-skew tolerance",
    "handoff_owner_unstated": "A handoff was recorded without a named receiving owner",
    "out_of_scope": "Content outside the approved taxonomy or shaped as an instruction — routed to human review",
}

# Gap kinds that MUST retain both sides' exact source spans before they may be presented.
TWO_SIDED_GAP_KINDS = frozenset({"source_disagreement", "substituted_action", "exemption_claimed"})

# Gap kinds that are not a gap at all (nothing for the owner to resolve).
_NON_GAP_KINDS = frozenset({"ordered_confirmed"})

# ── closed enums for caller-supplied labels (outside → "unknown") ────────────────────────────────
OBSERVATION_KINDS = frozenset({"alarm", "traffic", "topology", "note", "handoff", "checklist"})
TIME_REFERENCE_INPUT_KINDS = frozenset({"absolute", "relative", "derived"})
CLOCK_SOURCES = frozenset({"ntp", "gps", "nms", "device", "manual", "unknown"})
#: Clock sources whose provenance the carrier considers verifiable.
TRUSTED_CLOCK_SOURCES = frozenset({"ntp", "gps", "nms"})

# ── seeded thresholds (CoE-calibratable) ─────────────────────────────────────────────────────────
#: Two timestamps within this many seconds are treated as the same instant (clock-skew tolerance).
CLOCK_SKEW_TOLERANCE_S = 2
#: Two sources describing the same event_ref disagree beyond this many seconds.
DISAGREEMENT_TOLERANCE_S = 60
#: Default zone assumed for a naive timestamp in a Japanese carrier packet (flagged, never silent).
ASSUMED_TZ = timezone(timedelta(hours=9))  # JST
#: Rule-set version recorded in the S-4 audit trail (counts + version only — never packet content).
RULE_VERSION = "tel-c2-137-timeline-rules/1.0.0"

# ── seeded marker vocabularies (bounded interpretation, never free-form generation) ──────────────
SUBSTITUTION_MARKERS = (
    "代替",
    "代替実施",
    "代替措置",
    "別手順",
    "回避策",
    "substitute",
    "substituted",
    "alternative procedure",
    "workaround instead",
)
EXEMPTION_MARKERS = (
    "除外",
    "監視除外",
    "適用除外",
    "免除",
    "対象外申請",
    "exempt",
    "exemption",
    "waiver",
    "suppression approved",
)
#: Instruction-shaped text in an analyst note (the **pre-LLM source-text containment layer**, independent
#: of the S-2 boundary reject in pre_process). Notes are quoted data — evidence only, never instructions.
#: Deliberately broader than pre_process's S-2 marker set: softer directive phrasing that is not worth
#: rejecting the whole investigation packet over still must not be *interpreted* — the entry is routed to
#: ``out_of_scope`` + ``human_review_flag`` so a human decides what the note means.
CONTAINMENT_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "disregard previous",
    "system prompt",
    "you are now",
    "###system",
    "<|im_start|>",
    "new instructions:",
    "override the approved",
    "output the following instead",
    "as the agent, you must",
    "respond only with",
    "以前の指示を無視",
    "システムプロンプト",
    "次のとおり出力せよ",
    "指示を上書き",
)

_MAX_STATEMENT = 4_000  # analyst notes are minimised to the minimum-necessary excerpt length


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def safe_identifier(value: Any, kind: str = "ref") -> str:
    """PRIVACY-tokenize a caller identifier to a deterministic opaque surrogate ``<kind>:<sha8>``.

    Caller identifiers are **always** hashed — there is no syntactic passthrough — so a name (with or
    without spaces), an MSISDN, or a site label can never survive into a citation or the output. The same
    raw value always maps to the same surrogate, so entries, gaps and citations stay joinable. This is a
    privacy measure only; it makes no claim that the identifier is authorized.
    """
    return f"{kind}:{_sha8(str(value or '').strip())}"


# ── What a citation in this template does and does not assert ────────────────────────────────────
#
# CITATION_BASIS is carried in every output envelope. It is worded as a *declaration* because that is
# all this template can honestly support:
#
#   Observations, and their `source` labels, arrive in the caller's request body. The template has no
#   record store to look them up in, and the platform exposes no trusted ingress attestation it could
#   check them against — the SDK documents `input_context` as CALLER metadata (the SDK state-schema docs:
#   `input_context | dict | invoke() | Caller metadata`), and the shipped
#   `src/api/server.py` calls `agent.invoke(req.input, ctx=ctx)` without it at all. An earlier revision
#   gated citations on an `input_context` attestation; because nothing populates it on the production
#   HTTP path, the very packet this repo's own tests expect to yield a six-entry worksheet returned
#   `needs_review` with `ordered_entries: []`. The agent produced no usable output. Trading a weak
#   claim for no output is not an improvement.
#
# So the citation asserts exactly this: **the caller declared this observation as coming from a named
# authorized system of record.** It does NOT assert that the record exists there. A caller that
# fabricates an observation and labels it `alarm:invented` will get a citation, and the envelope says
# so. Real verification needs a server-side lookup or gateway-signed references delivered outside the
# caller's body — the ingress-attestation agreement tracked in SoT §12-B, not something to simulate.
CITATION_BASIS = "caller_declared_authorized_source"


def normalise_reference(value: Any) -> str | None:
    """Normalise a ``<namespace>:<reference>`` source label, or ``None`` if it is not one.

    The namespace is case-folded (it *names* a system of record) and must be authorized; the reference
    part is preserved verbatim (it *identifies* a record and can be case-significant). A bare namespace
    identifies a system but no record, so it is not a citation.
    """
    text = str(value or "").strip()
    if ":" not in text:
        return None
    namespace, reference = text.split(":", 1)
    namespace = namespace.strip().lower()
    reference = reference.strip()
    if not reference or namespace not in AUTHORIZED_EVIDENCE_SYSTEMS:
        return None
    return f"{namespace}:{reference}"


def resolve_provenance(value: Any) -> str | None:
    """Resolve a **raw** caller source reference into a privacy-hashed citation — or ``None``.

    A citation is emitted only when the label names an authorized system of record in
    :data:`AUTHORIZED_EVIDENCE_SYSTEMS` **and carries a reference part**.

    ★ What this does NOT do — see :data:`CITATION_BASIS`. The check is on the *label*, not the record:
    the observation and its label arrive in the same caller body and no verification surface is exposed
    to the template, so a fabricated reference under an authorized namespace **will** cite. That limit is
    asserted in tests and declared in the envelope rather than papered over.

    ★ Forged-surrogate defence (this part *is* enforced): there is **no format-based passthrough**. A
    caller-supplied ``src:<hex>`` has namespace ``src``, which is not an authorized system of record, so
    it resolves to ``None`` and is dropped here at S-1. Because provenance is resolved exactly once
    (here, in pre_process), the ``src:<sha8>`` values seen downstream are always internally produced and
    are never fed back through this function.

    Anything else — free text, an unauthorized namespace, a bare namespace — returns ``None`` and S-3
    blocks the worksheet as ``CITATION_INCOMPLETE`` (fail-closed).
    """
    reference = normalise_reference(value)
    if reference is None:
        return None  # unauthorized namespace / no reference part / forged surrogate → no citation
    return "src:" + _sha8(reference)


def traffic_band(value: Any) -> str:
    """Reduce a commercial traffic magnitude to a coarse band (S-2 minimisation — exact value dropped)."""
    try:
        magnitude = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if magnitude >= 1_000:
        return "high"
    if magnitude >= 100:
        return "mid"
    if magnitude > 0:
        return "low"
    return "none"


def _contains(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


class EvidenceTimelineService:
    """Deterministic time normalisation, bounded taxonomy placement, citation anchoring, composition."""

    # ── Step 3 · TimeReferenceNormalise (deterministic, Tool-equivalent) ─────────────────────────
    @staticmethod
    def parse_time_reference(reference: Any) -> dict[str, Any]:
        """Parse one recorded time reference into an epoch + parse status. Never infers a missing time.

        Returns ``{epoch_utc, iso_utc, parse_status, declared_kind, clock_source}``. ``parse_status`` is one
        of ``absolute`` / ``naive_assumed_jst`` / ``relative`` / ``derived`` / ``unparseable`` / ``missing``.
        A relative ("15 minutes after the first alarm") or derived reference is **never** converted into a
        confirmed instant — it is reported as-is for the owner to fix.
        """
        if not isinstance(reference, dict):
            reference = {}
        declared = str(reference.get("kind") or "").strip().lower()
        declared = declared if declared in TIME_REFERENCE_INPUT_KINDS else "unknown"
        clock = str(reference.get("clock_source") or "").strip().lower()
        clock = clock if clock in CLOCK_SOURCES else "unknown"
        raw = str(reference.get("value") or "").strip()

        base = {"epoch_utc": None, "iso_utc": None, "declared_kind": declared, "clock_source": clock}
        if declared in ("relative", "derived"):
            # Deliberately not resolved: promoting a derived reference would fabricate a chronology.
            # Checked before the empty test so a declared-relative reference keeps its label even though
            # pre_process blanks its free-text value (S-2 charset minimisation).
            return {**base, "parse_status": declared}
        if not raw:
            return {**base, "parse_status": "missing"}
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return {**base, "parse_status": "unparseable"}
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ASSUMED_TZ)
            status = "naive_assumed_jst"
        else:
            status = "absolute"
        utc = parsed.astimezone(timezone.utc)
        return {**base, "epoch_utc": utc.timestamp(), "iso_utc": utc.isoformat(), "parse_status": status}

    @staticmethod
    def normalise(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalise every minimised observation into a sortable entry. Rows without a ref are dropped."""
        out: list[dict[str, Any]] = []
        for index, obs in enumerate(observations or []):
            if not isinstance(obs, dict):
                continue
            observation_ref = obs.get("observation_ref")
            if not observation_ref:
                continue
            timing = EvidenceTimelineService.parse_time_reference(obs.get("time_reference"))
            epoch = timing["epoch_utc"]
            out.append(
                {
                    "observation_ref": observation_ref,
                    "kind": obs.get("kind", "unknown"),
                    "event_ref": obs.get("event_ref"),
                    "statement": obs.get("statement", ""),
                    "statement_length": len(obs.get("statement", "") or ""),
                    "handoff_owner_ref": obs.get("handoff_owner_ref"),
                    "checklist_ref": obs.get("checklist_ref"),
                    "source_ref": obs.get("source_ref"),
                    "supporting_source_ref": obs.get("supporting_source_ref"),
                    "site_ref": obs.get("site_ref"),
                    "traffic_band": obs.get("traffic_band", "unknown"),
                    "epoch_utc": epoch,
                    "iso_utc": timing["iso_utc"],
                    "parse_status": timing["parse_status"],
                    "declared_time_kind": timing["declared_kind"],
                    "clock_source": timing["clock_source"],
                    # Entries without a usable epoch sort last, stably, by their input order.
                    "sort_key": [0 if epoch is not None else 1, epoch if epoch is not None else 0.0, index],
                    "input_index": index,
                }
            )
        return out

    # ── Step 4 · EvidenceSequenceInterpret (bounded taxonomy placement) ──────────────────────────
    @staticmethod
    def detect_disagreements(entries: list[dict[str, Any]]) -> dict[str, str]:
        """Map observation_ref → counterpart observation_ref where two sources disagree on the same event.

        Only entries that share an ``event_ref`` and both carry a parsed epoch are compared; a spread beyond
        :data:`DISAGREEMENT_TOLERANCE_S` is a disagreement. The agent never decides which side is correct.
        """
        by_event: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            event_ref = entry.get("event_ref")
            if event_ref and entry.get("epoch_utc") is not None:
                by_event.setdefault(event_ref, []).append(entry)
        pairs: dict[str, str] = {}
        for group in by_event.values():
            if len(group) < 2:
                continue
            ordered = sorted(group, key=lambda e: e["epoch_utc"])
            low, high = ordered[0], ordered[-1]
            if (high["epoch_utc"] - low["epoch_utc"]) > DISAGREEMENT_TOLERANCE_S:
                pairs[low["observation_ref"]] = high["observation_ref"]
                pairs[high["observation_ref"]] = low["observation_ref"]
        return pairs

    @staticmethod
    def detect_indeterminate(entries: list[dict[str, Any]]) -> frozenset[str]:
        """Observation refs whose ordering is indeterminate: epochs tie within the clock-skew tolerance."""
        timed = sorted((e for e in entries if e.get("epoch_utc") is not None), key=lambda e: e["epoch_utc"])
        out: set[str] = set()
        for left, right in zip(timed, timed[1:]):
            if abs(right["epoch_utc"] - left["epoch_utc"]) <= CLOCK_SKEW_TOLERANCE_S:
                out.add(left["observation_ref"])
                out.add(right["observation_ref"])
        return frozenset(out)

    @staticmethod
    def time_reference_kind(entry: dict[str, Any]) -> str:
        """Classify how trustworthy the recorded time reference is (independent of the gap kind)."""
        status: str = entry["parse_status"]
        if status in ("missing", "unparseable"):
            return "missing"
        if status in ("relative", "derived"):
            return status
        if status == "naive_assumed_jst" or entry["clock_source"] not in TRUSTED_CLOCK_SOURCES:
            return "absolute_clock_unverified"
        return "absolute_confirmed"

    @staticmethod
    def classify(entry: dict[str, Any], disagreements: dict[str, str], indeterminate: frozenset[str]) -> dict[str, Any]:
        """Place one entry into the seeded closed taxonomy. Highest-value gap wins; ties are impossible.

        **pre-LLM source-text containment (an independent layer, separate from S-2 and S-3):** the analyst
        statement is quoted data — *evidence only, never instructions*. Instruction-shaped text is not
        interpreted; the entry is routed to ``out_of_scope`` + ``human_review_flag`` so a human decides.
        """
        statement = entry.get("statement") or ""
        contained = _contains(statement, CONTAINMENT_MARKERS)
        time_kind = EvidenceTimelineService.time_reference_kind(entry)
        ref = entry["observation_ref"]

        if contained:
            gap_kind, counterpart = "out_of_scope", None
        elif ref in disagreements:
            gap_kind, counterpart = "source_disagreement", disagreements[ref]
        elif _contains(statement, SUBSTITUTION_MARKERS):
            gap_kind, counterpart = "substituted_action", None
        elif _contains(statement, EXEMPTION_MARKERS):
            gap_kind, counterpart = "exemption_claimed", None
        elif time_kind == "missing":
            gap_kind, counterpart = "timestamp_missing", None
        elif not entry.get("source_ref"):
            gap_kind, counterpart = "source_ref_missing", None
        elif time_kind in ("relative", "derived"):
            gap_kind, counterpart = "time_reference_derived", None
        elif time_kind == "absolute_clock_unverified":
            gap_kind, counterpart = "clock_provenance_uncertain", None
        elif ref in indeterminate:
            gap_kind, counterpart = "sequence_indeterminate", None
        elif entry["kind"] == "handoff" and not entry.get("handoff_owner_ref"):
            gap_kind, counterpart = "handoff_owner_unstated", None
        else:
            gap_kind, counterpart = "ordered_confirmed", None

        return {
            **entry,
            "time_reference_kind": time_kind,
            "gap_kind": gap_kind,
            "gap_description": EVIDENCE_TIMELINE_TAXONOMY[gap_kind],
            "counterpart_ref": counterpart,
            "human_review_flag": contained,
            "is_gap": gap_kind not in _NON_GAP_KINDS,
        }

    # ── Step 5 · CitationAnchorRetrieve (deterministic anchoring, nothing invented) ──────────────
    @staticmethod
    def anchor(entry: dict[str, Any], by_ref: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Build the deterministic evidence spans for one entry.

        A span carries the S-1-resolved ``source_ref`` plus the *location* of the recorded statement
        (offset + length) — never the raw excerpt (S-4 no-persist). Nothing is fabricated: an entry with no
        resolved provenance produces no primary span. Two-sided gap kinds additionally carry the
        counterpart side, so a disagreement / substitution / exemption is never shown one-sided.
        """
        spans: list[dict[str, Any]] = []
        if entry.get("source_ref"):
            spans.append(
                {
                    "side": "primary",
                    "anchor_kind": entry.get("kind", "unknown"),
                    "source_ref": entry["source_ref"],
                    "statement_span": {"start": 0, "length": entry.get("statement_length", 0)},
                }
            )
        gap_kind = entry["gap_kind"]
        if gap_kind == "source_disagreement":
            counterpart = by_ref.get(entry.get("counterpart_ref") or "")
            if counterpart and counterpart.get("source_ref"):
                spans.append(
                    {
                        "side": "counterpart",
                        "anchor_kind": counterpart.get("kind", "unknown"),
                        "source_ref": counterpart["source_ref"],
                        "observation_ref": counterpart["observation_ref"],
                        "statement_span": {"start": 0, "length": counterpart.get("statement_length", 0)},
                    }
                )
        elif gap_kind in ("substituted_action", "exemption_claimed") and entry.get("supporting_source_ref"):
            spans.append(
                {
                    "side": "supporting_record",
                    "anchor_kind": "checklist" if gap_kind == "substituted_action" else "exemption_record",
                    "source_ref": entry["supporting_source_ref"],
                    "checklist_ref": entry.get("checklist_ref"),
                }
            )
        return spans

    @staticmethod
    def spans_satisfy_bounded_rule(gap_kind: str, spans: list[dict[str, Any]]) -> bool:
        """Bounded acceptance rule: two-sided gap kinds need both sides; every other kind needs one."""
        cited = [s for s in spans if s.get("source_ref")]
        if gap_kind in TWO_SIDED_GAP_KINDS:
            return len(cited) >= 2
        return len(cited) >= 1

    # ── Step 6 · TimelineWorksheetCompose ────────────────────────────────────────────────────────
    @staticmethod
    def evidence_time_range(entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Fix the evidence/time range. Unbounded when any entry has no confirmed instant."""
        timed = [e for e in entries if e.get("epoch_utc") is not None]
        untimed = len(entries) - len(timed)
        if not timed:
            return {"start_utc": None, "end_utc": None, "bounded": False, "entries_without_time": untimed}
        ordered = sorted(timed, key=lambda e: e["epoch_utc"])
        return {
            "start_utc": ordered[0]["iso_utc"],
            "end_utc": ordered[-1]["iso_utc"],
            "bounded": untimed == 0,
            "entries_without_time": untimed,
        }

    @staticmethod
    def compose(entries: list[dict[str, Any]], packet: dict[str, Any], default_owner_ref: str | None) -> dict[str, Any]:
        """Compose the ComplexFaultEvidenceTimelineWorksheet from cited, classified entries."""
        ordered = sorted(entries, key=lambda e: e["sort_key"])
        ordered_entries: list[dict[str, Any]] = []
        gap_items: list[dict[str, Any]] = []
        for seq, entry in enumerate(ordered, start=1):
            spans = entry.get("evidence_spans", [])
            ordered_entries.append(
                {
                    "seq": seq,
                    "stated_observation_ref": entry["observation_ref"],
                    "observation_kind": entry.get("kind", "unknown"),
                    "time_reference_kind": entry["time_reference_kind"],
                    "stated_time_utc": entry.get("iso_utc"),
                    "evidence_span": spans,
                    "gap_kind": entry["gap_kind"],
                    "gap_description": entry["gap_description"],
                    # The entry's OWN stated receiving owner — never back-filled from the packet default, or a
                    # handoff_owner_unstated gap would look as if an owner had been recorded.
                    "handoff_owner_ref": entry.get("handoff_owner_ref"),
                    "human_review_flag": entry.get("human_review_flag", False),
                    "traffic_band": entry.get("traffic_band", "unknown"),
                    "site_ref": entry.get("site_ref"),
                }
            )
            if entry["is_gap"]:
                gap_items.append(
                    {
                        "gap_kind": entry["gap_kind"],
                        "gap_description": entry["gap_description"],
                        "stated_observation_ref": entry["observation_ref"],
                        "cited_source_ref": [s["source_ref"] for s in spans if s.get("source_ref")],
                        "owner_ref": entry.get("handoff_owner_ref") or default_owner_ref,
                        "bounded_rule_satisfied": EvidenceTimelineService.spans_satisfy_bounded_rule(
                            entry["gap_kind"], spans
                        ),
                    }
                )

        distribution: dict[str, int] = {}
        for entry in ordered:
            distribution[entry["gap_kind"]] = distribution.get(entry["gap_kind"], 0) + 1

        return {
            "status_kind": "evidence_timeline_worksheet",
            "investigation_id": packet.get("investigation_id"),
            "packet_version_ref": packet.get("packet_version_ref"),
            "worksheet_schema_version": packet.get("worksheet_schema_version"),
            "rule_version": RULE_VERSION,
            "ordered_entries": ordered_entries,
            "evidence_time_range": EvidenceTimelineService.evidence_time_range(ordered),
            "gap_items": gap_items,
            "gap_distribution": distribution,
            "checklist_clauses_referenced": len(packet.get("evidence_checklist", []) or []),
            "needs_review": True,
            "citations": sorted(
                {s["source_ref"] for e in ordered for s in e.get("evidence_spans", []) if s.get("source_ref")}
            ),
        }


# ── shared redaction patterns (S-1 input hygiene and S-3 output defense-in-depth) ────────────────
# Bounded quantifiers only — an unbounded `+@` style email pattern is a ReDoS hazard on long inputs.
CREDENTIAL_RE = re.compile(r"\b(?:sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})\b")
MY_NUMBER_RE = re.compile(r"\b\d{12}\b")  # Japanese My-Number / 個人番号 (also catches a bare IMSI/IMEI run)
EMAIL_RE = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}\b")
PHONE_RE = re.compile(r"(?<![\d.])(?:(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4})(?![\d.])")
COMPANY_RE = re.compile(
    r"(?:[A-Z][A-Za-z0-9&.\-]{0,24}\s){1,4}(?:Inc|Corp|Corporation|Ltd|LLC|LLP|GmbH|PLC|K\.?K|KK)\b\.?"
    r"|[^\s\"',]{1,24}(?:株式会社|有限会社|合同会社)"
    r"|(?:株式会社|有限会社|合同会社)[^\s\"',]{1,24}"
)
REDACTED = "[REDACTED]"


def hygiene_text(text: str) -> str:
    """Redact credential / My-Number / email / phone patterns from a free-text value (S-1 input hygiene)."""
    out = CREDENTIAL_RE.sub(REDACTED, text or "")
    out = MY_NUMBER_RE.sub(REDACTED, out)
    out = EMAIL_RE.sub(REDACTED, out)
    out = PHONE_RE.sub(REDACTED, out)
    return out[:_MAX_STATEMENT]
