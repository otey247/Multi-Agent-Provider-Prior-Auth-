# CMS-0057 / Da Vinci Standards Layer

**What's new:** the accelerator now speaks **CMS-0057 / Da Vinci** (CRD / DTR / PAS). On top of the existing four-agent readiness pipeline, a **standards layer** answers the three questions a provider actually asks — *is a prior auth required and where does it route, exactly what does this payer want, and is my package ready to send* — using reusable, payer-specific **policy packs**.

> **Boundaries (unchanged):** This is provider-side only — it never approves/denies coverage. **No live payer API is called**; the CRD/DTR/PAS behavior is derived locally from reviewed policy packs, and pack content shipped in this repo is **synthetic demo data**. Human review is still required before submission. See [Boundaries & non-goals](#boundaries--non-goals).

---

## Why this was added

The prior version answered the provider's third question (submission readiness) well, but only partially answered the first two — it assumed PA was required and matched against **Medicare LCDs/NCDs only**, using a **generic 10-item** documentation checklist. For a commercial/Medicare-Advantage case that gap showed up directly (the orthopedics sample flagged *"pull the UnitedHealthcare lumbar-fusion policy"*).

| Provider question | Da Vinci phase | Before | Now |
|---|---|---|---|
| Is a PA required, and where does it route? | **CRD** | Assumed | Determined from the matched pack (PA-required + routing channel) |
| Exactly what does *this* payer want? | **DTR** | Generic 10-item checklist + Medicare policy | Payer/plan/procedure-specific requirement checklist, each item mapped to chart evidence |
| Is the package ready to send? | **PAS** | Gate-based readiness | Same, plus a package-preview with PAS-ready / channel / missing-for-submission |

The immediate value (per the CMS-0057 write-up that motivated this) is **grounding the model in the FHIR-aligned Da Vinci pattern** so future real CRD/DTR/PAS integrations drop in cleanly — without calling live payer APIs in the POC.

---

## Feature summary

- **Policy packs** — reusable, human-reviewed payer + plan + procedure + diagnosis requirement sets (JSON), loaded from disk. One pack drives the whole CRD/DTR/PAS experience.
- **CRD-lite** — deterministic "is PA required + routing channel (payer portal vs delegated UM vendor)" from the matched pack.
- **DTR-lite** — each payer documentation requirement evaluated against the chart/packet as **MET / INSUFFICIENT / MISSING**, with evidence and a specific gap action.
- **PAS-lite** — a prepared **package preview** (never submitted): PAS-ready / portal-ready flags, submission channel, and a missing-for-submission checklist.
- **Requirement-aware agents** — the hosted **Coverage** (Policy Matching) agent now treats a matched pack as the authoritative plan policy and maps evidence to each pack criterion; the **Compliance** agent can append payer-specific documentation requirements to its checklist.
- **Standards Alignment UI panel** — a collapsible CRD → DTR → PAS card in the assessment view.
- **Diagnostics + API** — an optional `standards` block on the review response, plus `GET /api/policy-packs` to list what's loaded.
- **Feature-flagged & additive** — everything degrades gracefully; when no pack matches or the layer is disabled, the pipeline behaves exactly as before.

---

## How it fits the pipeline

The standards layer wraps the existing four-agent flow — it does not replace it.

```
Intake ─▶ Pre-flight (CPT validation)
        └▶ CRD-lite: match_policy_pack()  ── injects requirement set ──┐
                                                                        ▼
        Phase 1  Documentation Completeness + Clinical Evidence   (Compliance gets policy_requirements)
        Phase 2  Policy Matching (Coverage)                       (Coverage gets payer_policy_pack)
        ├▶ demo-NPI verification fix (scoped)
        └▶ build_standards_assessment()  → CRD + DTR + PAS
        Phase 3  Submission Readiness (Synthesis)
        Phase 4  Audit trail + report
        ▶ ReviewResponse.standards  (+ "standards" trace phase in the Debug Console)
```

Two cooperating mechanisms:

1. **Deterministic standards service** (`backend/app/services/standards/`) — computes CRD/DTR/PAS from the matched pack + existing agent outputs using explainable field/keyword matching. This is the reliable, demo-stable source (same approach `coverage_enrich` uses to remove LLM variance).
2. **Requirement-aware agents** — the orchestrator also injects the pack into the Compliance and Coverage agent prompts so the agents *themselves* reason over the payer requirements (see [Requirement-aware agents](#requirement-aware-agents)).

---

## Policy packs

A **policy pack** is the central artifact. It lives under [`backend/policy_packs/<pack-id>/policy_set.json`](../backend/policy_packs/) and deserializes into `models.standards.PolicySet`.

> **Why under `backend/`:** the backend image builds from the `./backend` context (`az acr build … ./backend`), so a repo-root folder would not ship in the container. Packs are bundled at `backend/policy_packs/` (`COPY policy_packs/ ./policy_packs/`), resolved at `/app/policy_packs` in the container. JSON (not YAML) is used so loading needs no extra dependency.

### Structure

| Object | Purpose | FHIR / Da Vinci alignment |
|---|---|---|
| `PolicySet` | The pack: payer, plan, LOB, delegated vendor, procedure/diagnosis scope, version, source | — |
| `CoverageRule` | Whether PA is required + trigger conditions (CRD) | CRD / CDS Hooks |
| `DocumentationRequirement` | A required data element / document / attestation / attachment (DTR) | `Questionnaire` / `QuestionnaireResponse` |
| `MedicalNecessityCriterion` | A discrete medical-necessity criterion | DTR criterion / CQL rule (human-readable in lieu of CQL) |
| `EvidenceMapping` | Where/how to find evidence in the chart | `Condition`, `Procedure`, `Observation`, `DocumentReference`, … |

Field names intentionally mirror FHIR so a future real DTR/CRD feed can populate the same objects.

### Matching

`match_policy_pack(payer_name, payer_plan, procedure_codes, diagnosis_codes)` scores packs — **procedure-code overlap is mandatory**; payer, plan, and diagnosis overlap refine confidence — and returns an explainable `PolicyPackMatch` with `reasons`. Below `minimum_confidence` (default 0.5) it returns unmatched and the pipeline falls back to the existing Medicare runtime search.

### Authoring a new pack

1. Create `backend/policy_packs/<payer>-<plan>-<procedure>/policy_set.json` (copy the flagship pack as a template).
2. Fill in scope (`procedure_codes`, `diagnosis_codes`, `payer`, `plan`), a `CoverageRule`, `documentation_requirements`, `medical_necessity_criteria`, and `evidence_mappings`.
3. Verify locally: `cd backend && python scripts/check_policy_store.py`.
4. Redeploy (rebuilds the backend image) and confirm with `GET /api/policy-packs`.

The shipped example is **`uhc-commercial-lumbar-fusion-v1`** (UnitedHealthcare Commercial HMO, CPT 22612/22840): 8 documentation requirements + 3 medical-necessity criteria.

---

## The `standards` assessment

When enabled and a pack matches, `ReviewResponse.standards` (`models.standards.StandardsAssessment`) carries:

- **`crd`** (`CrdDetermination`) — `pa_required`, `routing_channel` (e.g. *"Payer portal (UnitedHealthcare)"* or *"Delegated UM vendor: …"*), `reasons`.
- **`dtr`** (`DtrAssessment`) — `requirements_met` / `requirements_total`, and a `requirement_evaluations[]` of `RequirementEvaluation` (status, confidence, evidence, `gap_action`).
- **`pas`** (`PasPreview`) — `pas_ready`, `portal_ready`, `submission_channel`, `missing_for_submission[]`, and a `package_summary`.

Evaluation is deterministic: each requirement is scored against the request fields (clinical notes, `prior_treatment_history`, `attached_note_types`) and the clinical extraction. Attachment-required items need the document named in `attached_note_types` (a narrative mention alone is `INSUFFICIENT`). `pas_ready` is true only when every **required, non-conditional** requirement is MET.

### Scoped demo-provider fix

`apply_demo_provider_verification()` marks the curated sample-case NPI (`DEMO_VERIFIED_NPIS = {1669542008}`) as **VERIFIED** so a fictional NPI doesn't block Gate 1 before the requirements story is reached. It is **skipped during coverage fallback**, so a real hosted-agent outage still surfaces honestly as unverified.

---

## Requirement-aware agents

The orchestrator injects a compact requirement set into the hosted agents (null when no pack, so default behavior is preserved):

- **Coverage / Policy Matching agent** — receives `payer_policy_pack`. Its [SKILL.md](../agents/coverage/skills/coverage-assessment/SKILL.md) Step 3b treats the pack as the **authoritative plan policy**, adds a `coverage_policies` entry for it, and maps clinical evidence to each pack criterion in `criteria_assessment` (`source` = pack id). ✅ **Verified live** — the flagship case produced 11 pack-sourced criteria (all 8 requirements + 3 criteria).
- **Compliance / Documentation Completeness agent** — receives `policy_requirements`. Its [SKILL.md](../agents/compliance/skills/compliance-review/SKILL.md) appends each payer requirement to the checklist beyond the standard 10 and adds unmet required ones to `missing_items`.

> Agent `SKILL.md` is inlined into the system prompt **at container startup** (`agents/*/main.py` `_load_skill()`), so changing an agent prompt requires an **agent image rebuild** (`azd up`) — a backend-only redeploy will not update agent behavior.

---

## UI — Standards Alignment panel

A new [`StandardsPanel`](../frontend/components/standards-panel.tsx) renders under the Submission Readiness header in the assessment tab:

- **CRD** — "Prior authorization required" + routing-channel badges.
- **DTR** — a collapsible requirement checklist (defaults open) with a `{met}/{total} met` chip; each row shows a MET / INSUFFICIENT / MISSING status, confidence, evidence, and the gap action.
- **PAS** — a "PAS package ready / Not yet PAS-ready" badge, submission channel, the package summary, and a before-submission checklist.

It reuses the existing card/badge design, is collapsible so the baseline demo stays simple, and clearly labels the data as a reviewed policy pack (no live payer API implied). When no pack matches it shows a compact "runtime Medicare LCD/NCD search applies" note. The Debug Console labels the new `standards` trace phase *"Standards Alignment (CRD/DTR/PAS)"*.

---

## API & configuration

### Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/review`, `POST /api/review/stream` | Unchanged contract; response now includes an optional `standards` block |
| `GET /api/policy-packs` | List loaded packs + resolved directory (fast, agent-free — confirms a pack shipped in the image) |
| `GET /api/policy-packs/{policy_set_id}` | Full pack detail |

### Feature flags ([`backend/app/config.py`](../backend/app/config.py))

| Variable | Default | Effect |
|---|---|---|
| `ENABLE_STANDARDS_LAYER` | `true` | Master switch for the CRD/DTR/PAS block |
| `ENABLE_POLICY_PACKS` | `true` | Enable pack matching |
| `ENABLE_PAS_PREPARE` | `true` | Build the PAS package preview (never submits) |
| `POLICY_PACKS_DIR` | *(unset)* | Override the packs directory (defaults to bundled `backend/policy_packs`) |

---

## What changed (file map)

**Backend**
- `app/models/standards.py` — all standards + policy-pack models *(new)*
- `app/services/policy_store/{loader,matcher}.py` — load + match packs *(new)*
- `app/services/standards/evaluator.py` — CRD/DTR/PAS builder + demo-NPI fix *(new)*
- `app/routers/standards.py` — `GET /api/policy-packs` *(new)*
- `app/agents/orchestrator.py` — CRD match, requirement injection, demo-NPI fix, standards assessment + trace phase *(edited)*
- `app/models/schemas.py` — `standards` on `ReviewResponse`; `app/routers/review.py`, `app/main.py`, `app/config.py`, `Dockerfile` *(edited)*
- `policy_packs/uhc-commercial-lumbar-fusion/policy_set.json` — flagship pack *(new)*
- `scripts/{check_policy_store,check_standards,e2e_standards}.py` — verification *(new)*

**Agents** — `agents/compliance/.../SKILL.md`, `agents/coverage/.../SKILL.md` *(additive requirement-aware sections)*

**Frontend** — `lib/types.ts` (standards types), `components/standards-panel.tsx` *(new)*, `components/review-dashboard.tsx` + `components/debug-console.tsx` *(edited)*

---

## Verification

| Check | Command | Result |
|---|---|---|
| Pack load + match | `cd backend && python scripts/check_policy_store.py` | PASS |
| Standards service (offline) | `cd backend && python scripts/check_standards.py` | PASS |
| Frontend types | `cd frontend && npx tsc --noEmit` | clean |
| Pack shipped in image | `GET /api/policy-packs` | `count:1`, `/app/policy_packs` |
| Full pipeline (live) | `python scripts/e2e_standards.py <backendUrl>` | PASS |

**Flagship live result** (UHC Commercial lumbar fusion): PA required → UHC portal · DTR **6/8** met · gaps = PT discharge summary + smoking-cessation · PAS **not ready** · provider **VERIFIED** · Coverage agent emitted 11 pack-sourced criteria.

---

## Deployment notes

- **`azd up` is required** to pick up backend changes and (for the SKILL.md changes) rebuild + re-register the hosted agents — the agent build/register runs in the `postprovision` hook, which fires on `azd up`/`azd provision`, **not** `azd deploy`.
- If `azd up` is run from a **separate clone** (e.g. a Codespace), that clone must `git pull origin main` **before** deploying, or it ships stale code. Confirm with `git log --oneline -1`.
- Policy packs must live under `backend/` to ship in the image (see [Policy packs](#policy-packs)).

---

## Boundaries & non-goals

This phase deliberately does **not**:

- Call live CMS-0057 CRD / DTR / PAS endpoints (derived locally from reviewed packs).
- Implement full CQL execution (human-readable descriptions/criteria are authoritative).
- Submit real prior auth requests, or make payer approve/deny determinations.
- Implement X12 278 mapping or a production EHR/FHIR ingestion pipeline.

Pack content shipped here is synthetic demo data. Medicare LCD/NCD runtime search remains the fallback when no pack matches.

## Future direction

The FHIR-aligned model is the on-ramp to: real CRD (replace runtime search with a payer coverage-requirements feed), real DTR (consume payer `Questionnaire`/CQL at runtime), real PAS submission + status tracking, and Agent-Creator-generated payer-specific clinician assistants.
