# PT4 founder hardening analysis

Status: **INSUFFICIENT_EVIDENCE**

Gate B: **INSUFFICIENT_EVIDENCE**

## Evidence boundary

- paired real tasks: 0 / 20
- baseline integrity: None
- human labels complete: False
- production changes authorized: False

## Failure taxonomy

| Taxonomy | Count |
|---|---:|
| task_misunderstanding | 0 |
| irrelevant_navigation | 0 |
| context_waste | 0 |
| patch_quality | 0 |
| validation_failure | 0 |
| missing_validation | 0 |
| verifier_false_reject | 0 |
| verifier_false_accept | 0 |
| runner_infrastructure | 0 |
| model_capability | 0 |
| environment_mismatch | 0 |
| retry_without_progress | 0 |
| premature_escalation | 0 |
| late_escalation | 0 |
| selection_error | 0 |
| delivery_conflict | 0 |
| unknown_accounting | 0 |
| user_flow_friction | 0 |
| evidence_backed_other | 0 |

## Prioritization

Formula: `frequency x recoverable accepted-change loss x average cost or supervision burden x diagnostic confidence`

Status: **insufficient_evidence**

```json
[]
```

## Verifier diagnostics

```json
{
  "calibration": {
    "probability_fabricated": false,
    "reason": "Binary verification records no success probability.",
    "status": "not_defined"
  },
  "confusion_matrix": {
    "false_negative": 0,
    "false_positive": 0,
    "true_negative": 0,
    "true_positive": 0
  },
  "evidence_type_correlations": [],
  "f1": null,
  "false_negative_cases": [],
  "false_positive_cases": [],
  "human_labelled_cases": 0,
  "infrastructure_exclusions": {
    "cases": [],
    "count": 0
  },
  "precision": null,
  "recall": null,
  "requirement_level_errors": [],
  "semantic_deterministic_disagreement": {
    "cases": [],
    "count": 0
  },
  "specificity": null
}
```

## Before and after

Status: **insufficient_evidence**

## Certificate

Not issued. Gate B did not pass.

PT5 was not started.
