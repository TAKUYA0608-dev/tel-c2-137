# Template Design Specification — TEL-C2-137

Telecom Complex-Fault Investigation Evidence Timeline Extractor (Cat 2, GraphNode-in-main).

## Position in AgentCore Architecture

- **Agent Class**: `ComplexFaultEvidenceTimelineAgent` (module-level alias of `Graph`)
- **L1 Base**: AgentBaseGraph (L1 direct — Cat 2 GraphNode-in-main; **not** AutonomousBaseGraph).
  `ChatAgent` in the proposal's §1 *Base* field is a **pattern reference only** (Level 2 was retired
  2026-05-18); nothing in this repo inherits an L2 class.
- **Category**: Cat 2 — orchestrates a fixed multi-step workflow to produce one job-to-be-done deliverable
  (a `ComplexFaultEvidenceTimelineWorksheet` for a completed complex-fault investigation packet).
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible); complex fields are JSON strings (ADR-005)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only — no `config` param)
  - Graph: composition (`register_nodes()` for node substitution; domain complexity behind a `GraphNode`)

## Scope (what this agent does / does not do)

**Does**: receives a *completed*, de-identified complex-fault investigation packet (alarm references,
traffic extracts, topology extracts, analyst notes, timestamp references) plus a versioned worksheet schema
and a carrier-owned evidence checklist; aligns the *recorded* observations and handoffs into a
source-anchored chronology; fixes the evidence/time range; raises missing timestamps, missing source
references and contradictions as **cited** gaps; and returns a `needs-review` worksheet for the named
accountable network-evidence owner.

**Does not**: touch live network data or telemetry, correlate alarms, diagnose root cause, recommend
remediation, estimate restoration, dispatch crews, contact subscribers, publish outage notices, or draft /
file the 電気通信事業法 第28条 重大事故報告. There is **no LLM** in this template (see *Determinism*).

## Architecture Overview

### Node Configuration (outer 5-slot backbone)

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema/session/trust setup | user_input | caller_trust_level, session_id | InitializeNode (default) |
| pre_process | `InvestigationPacketIngest` (**S-1**) + `NetworkDataMinimise` (**S-2, pre-LLM**) — NFKC + control strip + size cap, required-field validation, injection→degraded; **whitelist-by-construction projection** of the packet (only named fields are copied); every caller identifier **unconditionally tokenized** to an opaque surrogate; subscriber / contact / site-identifying fields dropped; commercial traffic values reduced to a band; analyst free text hygiened; `source` resolved to a citation **once**, here | user_input, input_context | validated_input, input_format, enriched_context, (error_code) | PreProcessNode (FunctionNode) |
| main | `EvidenceTimelineWorkflowGraphNode` — wraps inner `ComplexFaultEvidenceTimelineWorkflow` (composition criterion #9) | validated_input | result, entry_count, gap_count, human_review_flag, (error_code), status | GraphNode (subgraph) |
| post_process | `OutputSanitise` (**S-3** output gate + **S-4** no-persist audit) — fail-closed citation completeness, whole-report redaction, injection neutralisation, mandatory disclaimer | result | formatted_output, disclaimer, audit_logged, (error_code) | PostProcessNode (FunctionNode) |
| finalize | build response envelope | formatted_output | output, status | FinalizeNode (default) |

`src/nodes/main_node.py` is **deleted**: the `main` slot is a `GraphNode`, and it lives in
`src/graph/graph.py`. PB-6 instantiates every class under `src/nodes/` with a bare state, which a
`GraphNode` cannot survive (`KeyError: 'session_id'`), so no `GraphNode` — and no `MainNode` alias — is
placed there.

### Inner workflow (`src/graph/domain_workflow_graph.py` — BaseGraph, linear + per-node skip guard)

```
START → time_reference_normalise → evidence_sequence_interpret → citation_anchor_retrieve
      → timeline_worksheet_compose → END
```

This is SoT §4 Steps 3–6. Steps 1–2 are `pre_process` (S-1 / S-2) and Step 7 is `post_process` (S-3 / S-4).

| Inner Node | SoT step | Responsibility | Skip guard |
|------|---|---------------|-----------|
| time_reference_normalise | 3 | **Deterministic (Tool-equivalent)**: parse absolute timestamps, unify JST/UTC to an epoch, apply the clock-skew tolerance, derive a sortable key, and record `parse_status`. **Relative / derived / unparseable references are never promoted to a confirmed time** — they are flagged only. Sets `entry_count` | — (first node; emits `.skipped` and sets `NO_EVIDENCE` on rejected / 0-observation input) |
| evidence_sequence_interpret | 4 | **Agent value**: bounded classification of derived/relative times, unknown clock provenance, cross-source disagreement, substituted actions, claimed exemptions, indeterminate ordering and unstated handoff owners into the **seeded closed taxonomy**. **pre-LLM source-text containment (an independent layer, separate from S-2)**: analyst free text is *quoted data — evidence only, never instructions*; the output schema is restricted to the approved taxonomy plus cited references; prompt-shaped or taxonomy-outside text sets `human_review_flag` and `gap_kind=out_of_scope` | no-op `return {}` (after `.skipped` emit) on `error_code` / `entry_count == 0` |
| citation_anchor_retrieve | 5 | Deterministic anchoring of each entry to its evidence: the S-1-resolved `source_ref` plus a character span of the recorded statement (offset/length, never the raw excerpt). **Nothing is invented** — an entry with no resolved provenance gets no citation. Two-sided gap kinds (`source_disagreement` / `substituted_action` / `exemption_claimed`) must carry **both** sides' spans | no-op `return {}` (after `.skipped` emit) on `error_code` / `entry_count == 0` |
| timeline_worksheet_compose | 6 | Composes the deliverable: `ordered_entries[seq, stated_observation_ref, time_reference_kind, evidence_span, gap_kind, handoff_owner_ref]`, `evidence_time_range`, `gap_items[gap_kind, cited_source_ref, owner_ref]`, `status_kind`, fixed `needs_review`. On the 0-entry / rejected branch it emits the out-of-scope safe answer | emits safe answer on `error_code` / `entry_count == 0` |

> **Conditional edges do not propagate across the subgraph boundary** (a `GraphNode` wraps the inner
> graph), so the inner topology is a **static linear backbone with per-node skip guards** — the portable
> Cat 2 form shipped across the fleet. `add_conditional_edges` is intentionally **not** used inside the
> subgraph. `route()` is implemented because the `BaseGraph` ABC requires it, but it is not wired.

> **Inner nodes never read `input_context`.** `GraphNode.execute()` invokes the inner graph *without*
> `input_context`, so it does not reach the inner state. Everything an inner node needs (including the
> resolved provenance) is folded into `validated_input` by
> `pre_process`.

### Data Flow

```
START → initialize → pre_process → main(GraphNode) → {route} → post_process → finalize → END
                                        ↓ (retry, max 3)
                                     pre_process
```

**Degraded / rejected path (mandatory contract).** An injection marker, an oversize payload, empty input,
or zero usable observations **never** sets `status=ERROR`. The node returns `status=SUCCESS` **plus an
`error_code`** (`INJECTION_REJECTED` / `INPUT_TOO_LONG` / `INPUT_REJECTED` / `NO_EVIDENCE`). This is
deliberate: in the production framework `status=ERROR` short-circuits `AgentBaseGraph.route()` straight to
`finalize`, so **`main` / `post_process` would be skipped and the mandatory disclaimer + S-3 redaction +
S-4 terminal audit would never run**. With `SUCCESS + error_code`, `route()` reaches `post_process`, which
always emits the out-of-scope safe answer, the disclaimer, and the audit. On the injection / oversize path
`pre_process` **discards the offending body** (`validated_input="{}"`, `user_input` cleared) so no rejected
content is ever processed. Because `GraphNode.extract_input()` passes only `validated_input` into the fresh
inner state, `merge_output` surfaces the **outer** `error_code` first (`state.get("error_code") or
sub_result.get("error_code")`) so a pre-stage rejection code survives to the terminal S-4 audit.

### State Definition (`src/schemas/state.py`)

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| validated_input | NotRequired[str] | JSON minimised packet `{investigation_id, packet_version_ref, worksheet_schema_version, observations[], evidence_checklist[]}` | no |
| input_format | NotRequired[str] | `json` / `text` / `empty` / `rejected` | no |
| enriched_context | NotRequired[str] | JSON `{source, channel}` (read-only caller context) | no |
| normalised_entries | NotRequired[str] | JSON per-observation epoch / sort key / parse status | no |
| entry_count | NotRequired[int] | observations normalised (0 → out-of-scope safe answer) | no |
| interpreted_entries | NotRequired[str] | JSON + `time_reference_kind` / `gap_kind` / `human_review_flag` | no |
| cited_entries | NotRequired[str] | JSON + deterministic `evidence_spans[]` | no |
| result | NotRequired[str] | JSON assembled `ComplexFaultEvidenceTimelineWorksheet` | no |
| gap_count | NotRequired[int] | gap items raised for owner resolution | no |
| human_review_flag | NotRequired[bool] | True once containment / taxonomy-outside handling fires | no |
| formatted_output | NotRequired[str] | JSON final response envelope (worksheet + disclaimer) | no |
| disclaimer | NotRequired[str] | mandatory needs-review / not-a-diagnosis disclaimer | no |
| audit_logged | NotRequired[bool] | True once the terminal audit event is emitted | no |
| error_code | NotRequired[str] | `INPUT_REJECTED` / `INJECTION_REJECTED` / `INPUT_TOO_LONG` / `NO_EVIDENCE` / `CITATION_INCOMPLETE` | no |
| error_message | NotRequired[str] | operator-facing detail | no |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types); complex fields serialized as JSON strings (ADR-005)
- No JWT, API keys, credentials in State (checkpoint DB leakage) — pre_process input hygiene redacts them
- **Whitelist-by-construction.** `pre_process` builds `validated_input` by *projecting* only the named
  packet fields. An arbitrary extra caller field (an internal note carrying a crew member's name, a
  subscriber MSISDN, an exact site coordinate) is never copied, so it can never reach a citation or the
  output envelope.
- **Privacy tokenize ≠ provenance validation — two separate code paths.**
  - **Privacy (identifiers).** `investigation_id`, `packet_version_ref`, `observation_id`, `event_ref`,
    `handoff_owner`, `checklist_ref` / `clause_ref` and `site_ref` are **unconditionally** tokenized to
    `<kind>:<sha8>`. There is **no** syntactic "this looks like a safe id" passthrough — a loose
    `[A-Za-z0-9_.:/-]` grammar lets a bare name (`Alice`, `John.Smith`, `TaroYamada`) through, which is the
    exact leak this template must not have. Tokenizing hides PII; it asserts **nothing** about authority.
    The same raw value always maps to the same surrogate, so entries, gaps and citations stay joinable.
  - **Provenance (citations).** A caller `source` becomes a citation only when its leading namespace names
    an **authorized system of record** (`AUTHORIZED_EVIDENCE_SYSTEMS` — a *semantic* registry of carrier
    evidence systems, not a character class) **and it carries a reference part**. Only then is it
    privacy-hashed to `src:<sha8>`. Anything else — a crew name, `unknown`, a bare namespace, **or a
    caller-supplied value merely *shaped* like an internal surrogate (`src:1a2b3c4d`)** — resolves to
    `None`, and S-3 blocks the worksheet as `CITATION_INCOMPLETE` (fail-closed).

    ★ **What the citation asserts, and what it does not.** It asserts that *the caller declared* this
    observation as coming from a named authorized system of record. It does **not** assert that the
    record exists there — the observation and its label arrive in the same request body, and no
    verification surface is exposed to the template: the SDK documents `input_context` as **caller**
    metadata (the SDK state-schemas reference) and the shipped `src/api/server.py` calls
    `agent.invoke(req.input, ctx=ctx)` without it at all. Every envelope therefore carries
    `citation_basis: "caller_declared_authorized_source"` so no downstream reviewer infers verification
    from the word "citation". A fabricated reference under an authorized namespace **will** cite; that
    limit is asserted in `test_declared_provenance_is_not_verification` rather than hidden.

    **Why not gate on ingress attestation anyway** — an earlier revision did. Because nothing populates
    `input_context` on the deployed path, the packet this repo's own end-to-end test expects to yield a
    six-entry worksheet returned `needs_review` with `ordered_entries: []`: the agent was inert in
    production. Forwarding a caller-supplied attestation instead would validate the caller's `source`
    label against the caller's own metadata. Real verification needs a server-side lookup or
    gateway-signed references delivered outside the caller's body — the SoT §12-B ingress agreement,
    tracked rather than simulated.
  - **Provenance is resolved exactly once, at S-1 (`pre_process`).** Downstream (`service`, inner nodes,
    `post_process`) trusts that single resolution verbatim and **never re-resolves** — re-resolving would
    correctly reject the internally produced `src:<sha8>` and regress every valid citation. Because a
    forged surrogate is dropped at S-1, any `src:<sha8>` seen downstream is internally produced, so the
    "is this forged?" question does not exist after S-1.
  - `kind` / `time_reference.kind` / `clock_source` are constrained to closed enums (outside → `unknown`).
    Commercial traffic magnitudes are reduced to a band. Numeric fields are coerced.
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

### Seeded evidence-timeline taxonomy (closed set, no free generation)

`ordered_confirmed` · `time_reference_derived` · `clock_provenance_uncertain` · `timestamp_missing` ·
`source_ref_missing` · `source_disagreement` · `substituted_action` · `exemption_claimed` ·
`sequence_indeterminate` · `handoff_owner_unstated` · `out_of_scope`

Anything the bounded rules cannot place inside this set becomes `out_of_scope` **plus**
`human_review_flag=True`. Thresholds (clock-skew tolerance, disagreement tolerance) and the marker
vocabularies are seeded defaults in `src/services/service.py`, calibratable by CoE without touching node
logic.

### Bounded acceptance rules for the two-sided gap kinds

`source_disagreement`, `substituted_action` and `exemption_claimed` are only presented when **all** of:

1. **Both** sides' exact source spans are retained (e.g. `source_disagreement` → the alarm-export row *and*
   the analyst-note span; `substituted_action` → the checklist clause reference *and* the note span;
   `exemption_claimed` → the exemption reference *and* the note span).
2. The entry carries `needs_review` — no entry is promoted to *confirmed* without owner review.
3. The output contains **no downstream assertion** — the agent never states which timestamp is correct,
   that a step *was* performed, or that an exemption *is* valid. It presents the contradiction /
   substitution / exemption claim, cited, and stops.

If (1) is not satisfiable, S-3 is fail-closed and the worksheet body is withheld.

### presence ≠ entailment ≠ authority

| Layer | Question | Enforced by |
|---|---|---|
| **presence** (completeness) | Does the span / row exist and is it attached to the output? | **S-3, mechanically** (missing → fail-closed) |
| **entailment** (support) | Does the span's content actually support the assigned `gap_kind` / time reading? | **Not enforced by S-3** — the named accountable network-evidence owner's review *is* the entailment gate |
| **authority** (legitimate origin) | Did the feed come from a genuine system of record rather than the caller's say-so? | **Not enforceable here** — the authorized-namespace registry checks the *label*, not the record. Declared as `citation_basis`; real verification is the SoT §12-B ingress agreement |

The mandatory disclaimer states this explicitly.

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, caller_trust_level) — read-only inside nodes
- [x] **S-2 (`NetworkDataMinimise`, pre-LLM minimisation)**: `_extra_security_gate_input(self, state) -> dict`
      on `PreProcessNode` — size cap + prompt-injection markers, plus the field-level minimisation performed
      in `execute()` (subscriber identifiers / commercial traffic values / site-identifying information
      dropped, banded or tokenized **before any downstream node sees them**). SDK 1.0.0 contract: the hook
      **MUST NOT raise, and MUST NOT return `status=ERROR`**. A rejection is surfaced as a **degraded
      `SUCCESS + error_code`**; `execute()` re-checks the same conditions (the `@final` hook is not invoked
      by the local stub framework) and discards the offending body so the pipeline still reaches
      `post_process` and always emits the disclaimer + audit.
- [x] **pre-LLM source-text containment (an independent layer — *not* S-2, *not* S-3)**: implemented in
      `evidence_sequence_interpret`. Analyst free text is treated as **quoted data, evidence only, never
      instructions**; the node's output schema is restricted to the approved taxonomy plus cited
      references; prompt-shaped or taxonomy-outside text is routed to `human_review_flag` +
      `gap_kind=out_of_scope` rather than being interpreted. It is tested independently of S-3.
- [x] **S-3 (`OutputSanitise`)**: `PostProcessNode.execute()` enforces **fail-closed citation
      completeness** — a grounded worksheet with any uncited ordered entry or gap item, or a two-sided gap
      kind carrying fewer than two spans, is **not presented**; it degrades to a `needs_review` envelope
      with the worksheet body withheld (`error_code=CITATION_INCOMPLETE`, still `SUCCESS` so the disclaimer
      + S-4 audit run). A whole-report redactor re-redacts credential / My-Number / email / phone /
      company-name leakage and neutralises residual injection markers.
      `_extra_security_gate_output(self, result) -> dict` additionally verifies the mandatory disclaimer is
      present and MAY raise to block.
- [x] **S-4 (no-persist audit)**: `emit_trace_event()` inside **every** `execute()` path (including skip /
      safe branches) — domain events only (never `node_start` / `node_complete`, which `BaseNode.__call__()`
      emits automatically). Rule version + counts + error codes only; the raw packet, the analyst narrative
      and any prompt text are **never** persisted to the audit trail.

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass (`PreProcessNode`, `PostProcessNode`, and the four inner nodes) → framework
>   `@final` gate always runs automatically; extend via `_extra_security_gate_input()` /
>   `_extra_security_gate_output()` only.
> - `GraphNode` (`EvidenceTimelineWorkflowGraphNode`) → deliberate no-op (the wrapped subgraph nodes'
>   gates already apply).
> - Custom `BaseNode` subclass → not used in this template.

### Trust Level (S-1)

All concrete `FunctionNode` subclasses declare `required_trust_level = TrustLevel.VERIFIED_EXTERNAL`
explicitly (gate-trust-level-check), matching the agent-level `required_trust_level` in
`config/agent.yaml`. `GraphNode` is excluded by design (delegated S-1).

| Node | Class | `required_trust_level` |
|------|-------|------------------------|
| pre_process | `PreProcessNode` | `VERIFIED_EXTERNAL` |
| main | `EvidenceTimelineWorkflowGraphNode` | n/a — `GraphNode` (delegated to inner nodes) |
| inner · time_reference_normalise | `TimeReferenceNormaliseNode` | `VERIFIED_EXTERNAL` |
| inner · evidence_sequence_interpret | `EvidenceSequenceInterpretNode` | `VERIFIED_EXTERNAL` |
| inner · citation_anchor_retrieve | `CitationAnchorRetrieveNode` | `VERIFIED_EXTERNAL` |
| inner · timeline_worksheet_compose | `TimelineWorksheetComposeNode` | `VERIFIED_EXTERNAL` |
| post_process | `PostProcessNode` | `VERIFIED_EXTERNAL` |

### Determinism (no LLM)

The template is **fully deterministic** — no LLM is used anywhere. Time normalisation is `datetime`
parsing plus epoch arithmetic; classification is threshold banding plus a seeded marker vocabulary;
citation anchoring is offset arithmetic over the recorded statement; worksheet composition is stable
sorting plus keyed composition. `config/agent.yaml` declares no model and `pyproject.toml` declares no LLM
dependency. The value the proposal claims for the *Agent* layer (SoT §2-4) is the **bounded interpretation
and taxonomy placement of ambiguous evidence**, implemented here as auditable seeded rules; the G1/G2
gates in SoT §12 decide whether that layer stays an Agent or is re-classified as a `shared/tools/` checker.

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — outer `AgentBaseGraph` 5-slot backbone with a `GraphNode` in the
  `main` slot wrapping an inner `BaseGraph` (`ComplexFaultEvidenceTimelineWorkflow`).
- **Composition target**: `src/graph/domain_workflow_graph.py::ComplexFaultEvidenceTimelineWorkflow`
- **Subgraph caching**: `get_subgraph()` caches the inner workflow on a **`ClassVar`
  (`EvidenceTimelineWorkflowGraphNode._subgraph`), assigned via the class — not `self`** — so it is *not*
  mutable node-instance state (CoE §9 composition consistency; SDK how-to `compose-agents-graphnode.md`).
- **Error propagation strategy**: `propagate` — an inner ERROR surfaces as `SubgraphError`; the
  deterministic degraded path (0-observation / rejected) instead returns `status = SUCCESS + error_code`
  and a safe answer.

## EU AI Act Art.13 Design-Time Evidence (Advisory until 2026-09-01; required from 2026-09-01 when `docs/01_proposal.md` declares Annex III `In scope`)

> This is design-time evidence for the proposal declaration. Record only information supported
> by the template design and its dependencies. See the platform security rules.

| Evidence item | Design reference / description |
|---------------|--------------------------------|
| Intended purpose and operating context | Back-office evidence hygiene for a Japanese telecom operator: aligning the recorded observations of an **already-completed** complex-fault investigation into a cited chronology for the accountable network-evidence owner. Not safety-critical operation, not live network control, not a regulatory filing (Annex III `Out of scope` as declared in `docs/01_proposal.md`). |
| System capabilities and limitations | Capabilities: deterministic time normalisation, bounded taxonomy classification of ambiguous evidence, deterministic citation anchoring, worksheet composition. Limitations: no live data, no alarm correlation, no root-cause diagnosis, no remediation or dispatch, no external reporting; relative/derived times are never promoted to confirmed values; S-3 verifies citation **presence**, not **entailment**. |
| User-facing transparency information | Every response carries a fixed disclaimer (worksheet is `needs-review`, not a diagnosis and not an external report; citation presence ≠ entailment; the entailment judgement belongs to the named owner), `status_kind`, `citation_complete`, and per-entry `gap_kind` labels with their evidence spans. |
| Human oversight mechanism | Output is fixed to `needs_review`; no entry is promoted to *confirmed* by the agent, no downstream assertion is generated, and every `gap_item` carries an `owner_ref` for the named accountable network-evidence owner. S-3 is fail-closed: an ungrounded worksheet is withheld rather than shown. |

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0) — PB-4
- [x] Import targets: `framework/` and `shared/` only (via the `src.utils.audit` fallback shim)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | **AgentBaseGraph** | AutonomousBaseGraph | **AgentBaseGraph** | Fixed multi-step workflow, no autonomous reasoning loop — Cat 2 |
| Composition pattern | FunctionNode-in-main (Cat 1) | **GraphNode-in-main (Cat 2)** | **GraphNode-in-main** | SoT §4 has 7 ordered steps → encapsulate 3–6 behind a subgraph |
| GraphNode location | `src/nodes/main_node.py` | **`src/graph/graph.py`** | **`src/graph/graph.py`** | PB-6 bare-state-instantiates everything under `src/nodes/`; a `GraphNode` cannot survive it |
| Inner topology | conditional edges | **linear + skip guards** | **linear + skip guards** | Conditional edges do not propagate across the subgraph boundary |
| Time handling | infer/complete missing times | **flag only, never promote** | **flag only** | Fabricating a chronology is the proposal's risk #2; derived/relative stay labelled and go to the owner |
| Interpretation | LLM narrative over analyst notes | **seeded closed taxonomy + bounded rules** | **deterministic** | Auditable and reproducible; free text stays *quoted data*, never instructions |
| Citation trust | imply verification | **authorized namespace + reference, and say so** | **declared, not implied** | An allowlist alone lets a caller name a system of record it never read from — so the envelope declares `citation_basis` instead of claiming verification the platform cannot provide (SoT §12-B) |
| Provenance resolution | resolve at each layer | **resolve once at S-1** | **once at S-1** | Re-resolution rejects internally produced surrogates and regresses valid citations |
| Degraded path | status=ERROR | **status=SUCCESS + error_code** | **SUCCESS + error_code** | ERROR would skip post_process (S-3/S-4) in the production framework |
| Owner sign-off | agent marks entries confirmed | **fixed `needs_review` + named owner ref** | **needs_review** | The agent presents evidence; entailment and every downstream judgement are the owner's |

## Open Items (deferred to Stage ③ Implementation MR)

- Node `execute()` bodies (rewritten `pre_process` / `post_process`, the four inner nodes), the inner
  `ComplexFaultEvidenceTimelineWorkflow`, `EvidenceTimelineService` (taxonomy + time normalisation +
  citation anchoring + worksheet composition), `src/utils/audit.py` (S-4 shim), the deletion of
  `src/nodes/main_node.py`, unit / integration / boundary tests, and `docs/03` + `docs/07` land in the
  Stage ③ implementation MR. This design MR ships `docs/01_proposal.md` (reconciled),
  `docs/02_design.md` and `src/schemas/state.py` only.
- **SoT §12-B external dependencies remain open** and are tracked, not silently closed by this design:
  (1) the PM/CoE boundary ruling vs a sibling template and released a sibling template, (2) the
  Compliance/DPO + security sign-off on 通信の秘密 handling, (3) carrier-owner approved sanitised packets
  with a verified reference chronology, and (4) the **ingress-attestation agreement** — a server-side
  record lookup or gateway-signed source references delivered outside the caller's body. Until (4)
  lands, a citation records a caller *declaration* and every envelope says so
  (`citation_basis`); the template does not simulate the missing verification, and does not gate on
  `input_context`, which the SDK defines as caller metadata and which the shipped `server.py` never
  populates (see *Provenance*).
