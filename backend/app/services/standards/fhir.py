"""FHIR R4 artifact builders for the standards layer (PRD Epic 1 — DTR).

Renders the existing policy-pack / standards-assessment data as Da Vinci
DTR-shaped FHIR R4 resources:

  * ``questionnaire_from_pack``           — a ``Questionnaire`` whose items map
    1:1 to the pack's ``documentation_requirements``.
  * ``questionnaire_package_from_pack``   — a ``Parameters`` payload shaped
    like the DTR ``$questionnaire-package`` operation output (a collection
    ``Bundle`` holding the ``Questionnaire`` plus a ``Library`` carrying the
    pack's medical-necessity rules as human-readable text).
  * ``questionnaire_response_from_assessment`` — a ``QuestionnaireResponse``
    pre-populated from a completed review's DTR-lite evaluation: MET
    requirements are answered (with evidence and a DTR information-origin
    extension), unmet requirements stay unanswered and carry the gap action.

Everything is built as plain dicts (no new dependencies) so the FHIR shape
lives in exactly one module. Artifacts are synthetic demo output derived from
reviewed policy packs — no live payer API is called, and the disclaimer is
stamped into every resource.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from app.models.standards import DocumentationRequirement, PolicySet

# Da Vinci DTR profile / extension URLs recorded on generated artifacts so a
# future validator run (or a real payer integration) knows what was intended.
DTR_QUESTIONNAIRE_PROFILE = (
    "http://hl7.org/fhir/us/davinci-dtr/StructureDefinition/dtr-std-questionnaire"
)
DTR_QUESTIONNAIRE_RESPONSE_PROFILE = (
    "http://hl7.org/fhir/us/davinci-dtr/StructureDefinition/dtr-questionnaireresponse"
)
DTR_INFORMATION_ORIGIN_EXT = (
    "http://hl7.org/fhir/us/davinci-dtr/StructureDefinition/information-origin"
)

# Repo-scoped namespace for demo annotations with no official DTR equivalent
# (requirement evaluation status, confidence, gap action, disclaimer).
LOCAL_EXT_BASE = (
    "https://github.com/otey247/Multi-Agent-Provider-Prior-Auth"
    "/fhir/StructureDefinition"
)

# Default canonical base stamped into Questionnaire.url / Library.url.
# Deployments override via settings.FHIR_CANONICAL_BASE.
DEFAULT_CANONICAL_BASE = "https://prior-auth.example/fhir"

FHIR_DISCLAIMER = (
    "Synthetic demo artifact derived from a reviewed policy pack (CMS-0057 / "
    "Da Vinci DTR alignment). No live payer API was called. Human review is "
    "required before submission."
)


def _questionnaire_id(pack: PolicySet) -> str:
    for req in pack.documentation_requirements:
        if req.dtr_questionnaire_id:
            return req.dtr_questionnaire_id
    return f"{pack.policy_set_id}-dtr"


def _link_id(req: DocumentationRequirement) -> str:
    return req.dtr_questionnaire_item_link_id or req.requirement_id


def _item_type(req: DocumentationRequirement) -> str:
    if req.attachment_required or req.requirement_type == "attachment":
        return "attachment"
    return "boolean"


def _local_ext(name: str, key: str, value) -> dict:
    return {"url": f"{LOCAL_EXT_BASE}/{name}", key: value}


def _information_origin(source: str = "auto") -> dict:
    """DTR information-origin extension: how an answer was populated."""
    return {
        "url": DTR_INFORMATION_ORIGIN_EXT,
        "extension": [{"url": "source", "valueCode": source}],
    }


def questionnaire_from_pack(
    pack: PolicySet,
    *,
    canonical_base: str = DEFAULT_CANONICAL_BASE,
) -> dict:
    """Render a policy pack's documentation requirements as a FHIR Questionnaire."""
    qid = _questionnaire_id(pack)
    items: list[dict] = []
    for req in pack.documentation_requirements:
        extensions = [_local_ext("requirement-id", "valueString", req.requirement_id)]
        if req.requirement_type:
            extensions.append(
                _local_ext("requirement-type", "valueString", req.requirement_type)
            )
        if req.lookback_period:
            extensions.append(
                _local_ext("lookback-period", "valueString", req.lookback_period)
            )
        if req.clinician_attestation_required:
            extensions.append(
                _local_ext("clinician-attestation-required", "valueBoolean", True)
            )
        items.append({
            "linkId": _link_id(req),
            "text": req.description,
            "type": _item_type(req),
            "required": bool(req.required and not req.conditional),
            "extension": extensions,
        })

    questionnaire = {
        "resourceType": "Questionnaire",
        "id": qid,
        "meta": {"profile": [DTR_QUESTIONNAIRE_PROFILE]},
        "url": f"{canonical_base.rstrip('/')}/Questionnaire/{qid}",
        "version": pack.policy_version or "1",
        "title": f"{pack.payer} — {pack.policy_name} documentation requirements",
        "status": "active",
        "subjectType": ["Patient"],
        "publisher": pack.payer,
        "description": (
            f"Payer documentation requirements for {pack.policy_name} "
            f"(procedure codes {', '.join(pack.procedure_codes)}). {FHIR_DISCLAIMER}"
        ),
        "item": items,
    }
    date = pack.last_reviewed_at or pack.effective_date
    if date:
        questionnaire["date"] = date
    return questionnaire


def _library_from_pack(pack: PolicySet, canonical_base: str) -> dict:
    """Medical-necessity rules as a logic Library (human-readable, no CQL yet)."""
    lid = f"{pack.policy_set_id}-necessity-rules"
    lines = [
        f"{c.criterion_id} [{c.required_status}] {c.criterion_name}: "
        f"{c.rule_expression} — {c.human_readable_rationale}"
        for c in pack.medical_necessity_criteria
    ]
    citations = sorted({
        c.policy_citation for c in pack.medical_necessity_criteria if c.policy_citation
    })
    library = {
        "resourceType": "Library",
        "id": lid,
        "url": f"{canonical_base.rstrip('/')}/Library/{lid}",
        "version": pack.policy_version or "1",
        "title": f"{pack.policy_name} medical-necessity rules",
        "status": "active",
        "type": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/library-type",
                "code": "logic-library",
            }],
        },
        "publisher": pack.payer,
        "description": (
            "Human-readable medical-necessity rules from the policy pack. No "
            f"executable CQL is included (see PRD cross-cutting backlog). {FHIR_DISCLAIMER}"
        ),
        "content": [{
            "contentType": "text/plain",
            "data": base64.b64encode("\n".join(lines).encode("utf-8")).decode("ascii"),
        }],
    }
    if citations:
        library["relatedArtifact"] = [
            {"type": "citation", "display": c} for c in citations
        ]
    return library


def questionnaire_package_from_pack(
    pack: PolicySet,
    *,
    canonical_base: str = DEFAULT_CANONICAL_BASE,
) -> dict:
    """Parameters payload shaped like the DTR ``$questionnaire-package`` output.

    The real operation returns one ``return`` parameter per questionnaire, each
    a collection Bundle holding the Questionnaire and its Libraries.
    """
    return {
        "resourceType": "Parameters",
        "parameter": [{
            "name": "return",
            "resource": {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [
                    {"resource": questionnaire_from_pack(pack, canonical_base=canonical_base)},
                    {"resource": _library_from_pack(pack, canonical_base)},
                ],
            },
        }],
    }


def questionnaire_response_from_assessment(
    assessment: dict,
    pack: PolicySet,
    request_data: dict,
    request_id: str,
    *,
    canonical_base: str = DEFAULT_CANONICAL_BASE,
) -> dict:
    """Pre-populated QuestionnaireResponse from a review's DTR-lite evaluation.

    ``assessment`` is the stored ``StandardsAssessment`` dict from a completed
    review (``ReviewResponse.standards``). MET requirements become answered
    items carrying their evidence and a DTR information-origin extension
    (source ``auto`` — populated from the chart, not typed by a human); unmet
    requirements stay unanswered and expose the gap action, so the remaining
    human work is explicit in the artifact itself.
    """
    dtr = assessment.get("dtr") or {}
    evaluations = dtr.get("requirement_evaluations") or []
    by_requirement = {req.requirement_id: req for req in pack.documentation_requirements}
    qid = _questionnaire_id(pack)

    items: list[dict] = []
    for evaluation in evaluations:
        req = by_requirement.get(evaluation.get("requirement_id", ""))
        status = evaluation.get("status", "MISSING")
        extensions = [
            _local_ext("evaluation-status", "valueString", status),
            _local_ext("evaluation-confidence", "valueInteger",
                       int(evaluation.get("confidence", 0))),
        ]
        gap_action = evaluation.get("gap_action") or ""
        if gap_action:
            extensions.append(_local_ext("gap-action", "valueString", gap_action))

        item = {
            "linkId": _link_id(req) if req else evaluation.get("requirement_id", ""),
            "text": evaluation.get("description", ""),
            "extension": extensions,
        }
        if status == "MET":
            evidence = [str(e) for e in (evaluation.get("evidence") or []) if str(e)]
            answer_extensions = [_information_origin("auto")]
            answer_extensions += [
                _local_ext("evidence", "valueString", e) for e in evidence
            ]
            if req and _item_type(req) == "attachment":
                answer = {
                    "valueAttachment": {
                        "contentType": "text/plain",
                        "title": evidence[0] if evidence else evaluation.get("description", ""),
                    },
                    "extension": answer_extensions,
                }
            else:
                answer = {"valueBoolean": True, "extension": answer_extensions}
            item["answer"] = [answer]
        items.append(item)

    required_met = all(
        e.get("status") == "MET"
        for e in evaluations
        if e.get("required") and not e.get("conditional")
    )
    response = {
        "resourceType": "QuestionnaireResponse",
        "id": f"dtr-response-{request_id}",
        "meta": {"profile": [DTR_QUESTIONNAIRE_RESPONSE_PROFILE]},
        "questionnaire": f"{canonical_base.rstrip('/')}/Questionnaire/{qid}",
        "status": "completed" if (evaluations and required_met) else "in-progress",
        "authored": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "author": {
            "display": (
                "Provider Prior Auth Accelerator — deterministic standards layer "
                "(draft; human review required)"
            ),
        },
        "extension": [_local_ext("disclaimer", "valueString", FHIR_DISCLAIMER)],
        "item": items,
    }
    patient_name = str(request_data.get("patient_name") or "")
    if patient_name:
        response["subject"] = {"display": patient_name}
    return response
