from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.capabilities.models import (
    CapabilityProfile,
    CapabilitySnapshot,
    IncludedAttempt,
    ProfileKey,
)
from villani_ops.closed_loop.capabilities.scoring import wilson_lower_bound
from villani_ops.closed_loop.classification_adjustments import (
    apply_classification_policy,
)
from villani_ops.closed_loop.interfaces import (
    AttemptSummary,
    Classification,
    PolicyContext,
)
from villani_ops.closed_loop.model_management import (
    CapabilityStatus,
    ModelDetection,
    add_model_to_configuration,
    capability_status,
    inventory_document,
    route_basis,
    set_bootstrap_default,
    update_detection_state,
)
from villani_ops.closed_loop.policy import BootstrapPolicyEngine
from villani_ops.closed_loop.policy_preview import (
    build_policy_preview_document,
    initial_policy_context,
    simulate_historical_runs,
)
from villani_ops.closed_loop.policy_presets import (
    apply_policy_preset,
    configure_policy_preset,
)
from villani_ops.closed_loop.protocol import ClassificationSnapshot
from villani_ops.core.backend import Backend


RUNNER = CliRunner()
STAMP = "2026-07-14T00:00:00Z"


def _backend(
    name: str,
    *,
    capability: int = 0,
    source: str = "unrated",
    provider: str = "local",
    endpoint: str = "http://127.0.0.1:1234/v1",
    billing_mode: str = "unknown",
    fixed_cost: float | None = None,
) -> Backend:
    return Backend(
        name=name,
        provider=provider,
        base_url=endpoint,
        model=f"{name}-model",
        roles=["coding", "classification"],
        capability_score=capability,
        capability_score_source=source,
        billing_mode=billing_mode,
        fixed_cost_per_attempt=fixed_cost,
        metadata={"capability_status": "UNRATED"},
    )


def _configuration(*backends: Backend, preset: str = "balanced") -> dict[str, Any]:
    return {
        "public_policy": {
            "version": "villani-public-policy-v1",
            "preset": preset,
            "selection_preference": preset.replace("-", "_"),
        },
        "model_management": {"bootstrap_default": None},
        "policy": {
            "version": "bootstrap_v1",
            "easy_min_capability": 20,
            "medium_min_capability": 50,
            "hard_min_capability": 80,
            "economy_confidence_threshold": 0.8,
            "conservative_confidence_threshold": 0.65,
            "max_same_backend_retries": 0,
            "classifier_retry_limit": 1,
            "verifier_retry_limit": 0,
            "accepted_candidates_required": 1,
            "allow_constraint_violations": False,
            "allow_no_change_retry": False,
        },
        "capabilities": {
            "minimum_empirical_samples": 5,
            "minimum_empirical_wilson_lower_bound": 0.5,
            "target_success_probability": 0.8,
        },
        "budgets": {"max_attempts": 3, "max_cost": None},
        "verifier": {"no_llm": True},
        "backends": {
            backend.name: backend.model_dump(mode="json", exclude={"name", "api_key"})
            for backend in backends
        },
    }


def _classification(
    *, difficulty: str = "easy", risk: str = "low", confidence: float = 0.9
) -> ClassificationSnapshot:
    return ClassificationSnapshot(
        schema_version="villani.classification.v1",
        classification_id="classification_1",
        run_id="run_1",
        task_id="task_1",
        classified_at=datetime.now(timezone.utc),
        difficulty=difficulty,
        risk=risk,
        category="bug_fix",
        required_capabilities=[],
        estimated_attempts_needed=1,
        needs_tests=True,
        confidence=confidence,
        reasoning_summary="Fixture classification.",
        signals={},
        metadata={},
    )


def _profile(backend: Backend, samples: int, successes: int) -> CapabilityProfile:
    key = ProfileKey(
        backend_name=backend.name,
        provider=backend.provider,
        model=backend.model,
        task_category="*",
        difficulty="*",
        risk="*",
        classifier_version="task_classifier_v1",
        verifier_version="villani_ops_verifier_pipeline_v1",
        scorer_version="empirical_wilson_v1",
    )
    return CapabilityProfile(
        key=key,
        included_attempts=[
            IncludedAttempt(
                run_id=f"run_{index}",
                attempt_id="attempt_1",
                outcome="success" if index < successes else "verified_model_failure",
            )
            for index in range(samples)
        ],
        successes=successes,
        verified_model_failures=samples - successes,
        sample_count=samples,
        raw_success_rate=successes / samples,
        wilson_lower_bound=wilson_lower_bound(successes, samples),
        excluded_outcome_counts={},
        first_observed_at=STAMP,
        last_observed_at=STAMP,
        source_data_digest="1" * 64,
    )


def _snapshot(*profiles: CapabilityProfile) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        schema_version="villani.capability_snapshot.v1",
        scorer_version="empirical_wilson_v1",
        source_data_digest="2" * 64,
        profile_digest="3" * 64,
        generated_at=STAMP,
        profiles=list(profiles),
        excluded_outcome_counts={},
        source_run_count=len(profiles),
        source_attempt_count=sum(item.sample_count for item in profiles),
    )


def test_unrated_model_and_unknown_price_are_explicit() -> None:
    configuration: dict[str, Any] = {"backends": {}}
    add_model_to_configuration(
        configuration,
        backend_name="new-local",
        model="qwen",
        provider="local",
        endpoint="http://127.0.0.1:1234/v1",
    )

    record = inventory_document(configuration, None, {"detections": [], "tests": {}})[
        "models"
    ][0]

    assert record["capability_status"] == "UNRATED"
    assert record["observed_success_rate"] is None
    assert record["pricing_status"] == "unknown"
    assert record["observed_cost_per_accepted_task"] is None
    assert configuration["backends"]["new-local"]["capability_score_source"] == "unrated"


def test_bootstrap_default_does_not_fabricate_hard_task_qualification() -> None:
    backend = _backend("bootstrap")
    configuration = _configuration(backend)
    set_bootstrap_default(configuration, backend.name)

    decision = BootstrapPolicyEngine({backend.name: backend}, configuration).decide(
        initial_policy_context(_classification(difficulty="hard"), configuration, run_id="preview")
    )

    assert decision.chosen_backend is None
    option = decision.considered_backends[0]
    assert option.capability_provenance == "bootstrap"
    assert option.qualification_status == "estimated"
    assert option.cost_components["bootstrap_eligible"] is False
    assert capability_status(backend, configuration, None) == CapabilityStatus.BOOTSTRAP


def test_observed_profile_qualifies_only_at_sample_and_confidence_threshold() -> None:
    backend = _backend("empirical")
    configuration = _configuration(backend)
    observed = _snapshot(_profile(backend, 4, 4))
    qualified = _snapshot(_profile(backend, 5, 5))

    assert capability_status(backend, configuration, observed) == CapabilityStatus.OBSERVED
    assert capability_status(backend, configuration, qualified) == CapabilityStatus.QUALIFIED


def test_observed_and_qualified_routes_record_the_evidence_basis() -> None:
    backend = _backend("empirical", billing_mode="fixed", fixed_cost=0.25)
    configuration = apply_policy_preset(
        _configuration(backend), "cheapest-acceptable"
    )
    observed = _snapshot(_profile(backend, 1, 1))
    qualified = _snapshot(_profile(backend, 5, 5))

    assert (
        route_basis(
            backend,
            configuration,
            observed,
            qualified_empirical_route=False,
        )
        == "observed_policy"
    )

    decision = BootstrapPolicyEngine(
        {backend.name: backend},
        configuration,
        capability_snapshot=qualified,
    ).decide(
        initial_policy_context(
            _classification(), configuration, run_id="qualified-preview"
        )
    )

    assert decision.chosen_backend == backend.name
    assert decision.metadata["route_provenance"]["basis"] == "qualified_empirical"
    assert decision.metadata["route_provenance"]["empirical_evidence_used"] is True


def test_saved_preset_selection_does_not_make_reliable_tuning_sticky() -> None:
    configuration = _configuration(_backend("bootstrap"))

    reliable_saved = configure_policy_preset(configuration, "reliable")
    reliable_run = apply_policy_preset(reliable_saved)
    balanced_saved = configure_policy_preset(reliable_saved, "balanced")

    assert reliable_saved["policy"]["accepted_candidates_required"] == 1
    assert reliable_run["policy"]["accepted_candidates_required"] == 2
    assert balanced_saved["policy"]["accepted_candidates_required"] == 1


def test_manual_override_is_advanced_and_never_labelled_empirical() -> None:
    configuration: dict[str, Any] = {"backends": {}}
    add_model_to_configuration(
        configuration,
        backend_name="manual",
        model="manual-model",
        provider="local",
        endpoint="http://127.0.0.1:1234/v1",
        manual_capability_score=77,
    )
    backend = Backend.model_validate(
        {"name": "manual", **configuration["backends"]["manual"]}
    )
    record = inventory_document(configuration, None, {"detections": [], "tests": {}})[
        "models"
    ][0]

    assert record["manual_override"] is True
    assert record["manual_override_label"] == "Advanced manual capability override"
    assert route_basis(
        backend, configuration, None, qualified_empirical_route=False
    ) == "manual_override"


def test_unavailable_model_and_detected_context_are_exposed() -> None:
    backend = _backend("offline")
    configuration = _configuration(backend)
    state = update_detection_state(
        {"tests": {}},
        [
            ModelDetection(
                detector="fixture",
                provider="local",
                provider_display_name="Fixture",
                endpoint=backend.base_url or "",
                availability="unreachable",
                models=(backend.model,),
                tool_support=True,
                context_metadata={backend.model: {"context_window": 32768}},
                detected_at=STAMP,
                diagnostic="Fixture is unreachable.",
            )
        ],
    )

    record = inventory_document(configuration, None, state)["models"][0]

    assert record["availability"] == "unreachable"
    assert record["available"] is False
    assert record["tool_support"] == "supported"
    assert record["context_window"] == 32768


def test_local_first_starts_local_and_escalates_to_stronger_eligible_model() -> None:
    local = _backend("local-bootstrap", capability=45)
    expert = _backend(
        "expert",
        capability=80,
        source="manual_override",
        provider="openai-compatible",
        endpoint="https://models.invalid/v1",
        billing_mode="fixed",
        fixed_cost=1.0,
    )
    configuration = apply_policy_preset(_configuration(local, expert), "local-first")
    set_bootstrap_default(configuration, local.name)
    engine = BootstrapPolicyEngine(
        {local.name: local, expert.name: expert}, configuration
    )
    initial = initial_policy_context(_classification(), configuration, run_id="run_1")

    first = engine.decide(initial)
    escalated = engine.decide(
        PolicyContext(
            run_id=initial.run_id,
            trace_id=initial.trace_id,
            state="VERIFIED",
            classification=initial.classification,
            attempts=(
                AttemptSummary(
                    attempt_id="attempt_1",
                    backend_name=local.name,
                    exit_code=1,
                    status="failed",
                    cost_usd=None,
                    cost_accounting_status="unknown",
                    failure_category="capability_failure",
                ),
            ),
            verifications=(),
            eligible_candidate_ids=(),
            budget=initial.budget,
            policy_configuration=configuration,
        )
    )

    assert first.chosen_backend == local.name
    assert escalated.action == "escalate"
    assert escalated.chosen_backend == expert.name


def test_policy_preview_includes_adjustment_and_verifier_route_explanation() -> None:
    backend = _backend(
        "coder",
        capability=80,
        source="manual_override",
        billing_mode="fixed",
        fixed_cost=0.25,
    )
    configuration = _configuration(backend)
    configuration["classification_policy"] = {
        "version": "classification-policy-test-v1",
        "risk_floor": "high",
    }
    raw = _classification(risk="low")
    effective_value, adjustments, _version = apply_classification_policy(
        Classification(
            difficulty=raw.difficulty,
            risk=raw.risk,
            category=raw.category,
            required_capabilities=(),
            estimated_attempts_needed=1,
            needs_tests=True,
            confidence=raw.confidence,
            reasoning_summary=raw.reasoning_summary,
        ),
        configuration,
        timestamp=datetime.now(timezone.utc),
    )
    effective = raw.model_copy(update={"risk": effective_value.risk})
    decision = BootstrapPolicyEngine({backend.name: backend}, configuration).decide(
        initial_policy_context(effective, configuration, run_id="preview")
    )

    preview = build_policy_preview_document(
        raw_classification=raw,
        effective_classification=effective,
        adjustments=[item.model_dump(mode="json") for item in adjustments],
        decision=decision,
        configuration=configuration,
        backends={backend.name: backend},
    )

    assert preview["schema_version"] == "villani.policy_preview.v1"
    assert preview["raw_classification"]["risk"] == "low"
    assert preview["effective_classification"]["risk"] == "high"
    assert preview["adjustments"][0]["rule_id"] == "risk_floor.v1"
    assert preview["selected_coding_route"]["backend"] == backend.name
    assert preview["selected_verifier_route"]["selected"]["route"] == "deterministic-verifier"
    assert preview["selected_verifier_route"]["repository_validation_required"] is True
    assert preview["estimated_cost"]["status"] == "complete"
    backend_explanation = preview["backend_explanations"][0]
    assert backend_explanation["configured_score"] == 80
    assert backend_explanation["effective_score"] == 80
    assert backend_explanation["score_provenance"] == "explicit_override"
    assert "reserve_satisfied" in backend_explanation["reserve_impact"]
    assert (
        preview["selected_coding_route"]["stage_budget_projection"][
            "reserve_satisfied"
        ]
        is True
    )


def test_policy_simulation_reports_evidence_limitations_without_causal_claims(
    tmp_path: Path,
) -> None:
    actual = _backend(
        "actual",
        capability=80,
        source="manual_override",
        billing_mode="fixed",
        fixed_cost=1.0,
    )
    cheaper = _backend(
        "cheaper",
        capability=80,
        source="manual_override",
        billing_mode="fixed",
        fixed_cost=0.25,
    )
    configuration = _configuration(actual, cheaper)
    run = tmp_path / "runs" / "run_1"
    run.mkdir(parents=True)
    (run / "task.json").write_text(
        json.dumps({"run_id": "run_1", "instruction": "Fix parser"}),
        encoding="utf-8",
    )
    (run / "classification.json").write_text(
        json.dumps(_classification().model_dump(mode="json")), encoding="utf-8"
    )
    (run / "policy_decisions.jsonl").write_text(
        json.dumps(
            {
                "action": "attempt",
                "chosen_backend": actual.name,
                "considered_backends": [
                    {"backend_name": actual.name, "estimated_cost_usd": 1.0}
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = simulate_historical_runs(
        runs_root=tmp_path / "runs",
        configuration=configuration,
        backends={actual.name: actual, cheaper.name: cheaper},
        snapshot=None,
        preset="cheapest-acceptable",
    )

    assert report["tasks_evaluated"] == 1
    assert report["tasks_affected"] == 1
    assert report["live_policy_changed"] is False
    assert report["causal_savings_supported"] is False
    assert "causal cost savings" in report["unsupported_counterfactual_claims"]
    assert report["route_changes"][0]["counterfactual_outcome_known"] is False


def test_public_model_and_policy_cli_need_no_capability_score(
    tmp_path: Path, monkeypatch: Any
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    initialized = RUNNER.invoke(unified.app, ["init"])
    assert initialized.exit_code == 0, initialized.output

    added = RUNNER.invoke(
        unified.app,
        [
            "models",
            "add",
            "local-qwen",
            "--model",
            "qwen",
            "--provider",
            "local",
            "--endpoint",
            "http://127.0.0.1:1234/v1",
            "--default",
        ],
    )
    listed = RUNNER.invoke(unified.app, ["models", "--json"])
    selected = RUNNER.invoke(unified.app, ["policy", "use", "Reliable"])
    policies = RUNNER.invoke(unified.app, ["policy", "list", "--json"])

    assert added.exit_code == 0, added.output
    assert "capability was not fabricated" in added.output
    assert listed.exit_code == 0, listed.output
    inventory = json.loads(listed.output)
    assert inventory["models"][0]["capability_status"] == "BOOTSTRAP"
    assert selected.exit_code == 0, selected.output
    assert policies.exit_code == 0, policies.output
    assert any(item["id"] == "reliable" and item["active"] for item in json.loads(policies.output))
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved["public_policy"]["preset"] == "reliable"
    assert saved["policy"]["accepted_candidates_required"] == 1
