import { readFileSync } from "node:fs";

export type VillaniV2ProvenanceStatus = "recorded" | "derived" | "unknown";
export type VillaniV2AccountingStatus =
  "complete" | "partial" | "unknown" | "not_applicable";

export interface VillaniResourceV2 {
  schema_version: "villani.resource.v2";
  service_name: string;
  service_version: string | null;
  deployment_environment: string | null;
  host_id: string | null;
  process_id: string | null;
  attributes: Record<string, unknown>;
}

export interface VillaniArtifactDescriptorV2 {
  schema_version: "villani.artifact_descriptor.v2";
  artifact_id: string;
  digest: { algorithm: "sha256"; value: string };
  size_bytes: number;
  media_type: string;
  logical_role: string;
  sensitivity: "public" | "internal" | "confidential" | "restricted" | "secret";
  retention_class:
    "ephemeral" | "run" | "project" | "compliance" | "legal_hold";
  encryption_status: "unencrypted" | "encrypted" | "unknown";
  storage_reference: string | null;
  provenance_status: VillaniV2ProvenanceStatus;
  attributes: Record<string, unknown>;
}

export interface VillaniOutcomeV2 {
  schema_version: "villani.outcome.v2";
  run_id: string;
  attempt_id: string | null;
  verification_status:
    "accepted" | "rejected" | "unclear" | "error" | "not_run" | null;
  accepted: boolean | null;
  materialized: boolean | null;
  merged: boolean | null;
  reverted: boolean | null;
  ci_state: "pending" | "passed" | "failed" | "cancelled" | "not_run" | null;
  developer_disposition:
    "approved" | "rejected" | "modified" | "pending" | "not_reviewed" | null;
  defect_association: string | null;
  cost: number | null;
  currency: string | null;
  cost_accounting_status: VillaniV2AccountingStatus;
  latency_ms: number | null;
  latency_accounting_status: VillaniV2AccountingStatus;
  provenance_status: VillaniV2ProvenanceStatus;
  provenance: Record<string, unknown>;
}

export interface VillaniSpanV2 {
  schema_version: "villani.span.v2";
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  run_id: string;
  attempt_id: string | null;
  kind: string;
  name: string;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  attributes: Record<string, unknown>;
}

export interface VillaniAgentCapabilityV2 {
  schema_version: "villani.agent_capability.v2";
  capability_id: string;
  agent_name: string;
  agent_version: string | null;
  runner_protocols: string[];
  models: string[];
  features: string[];
  limits: Record<string, unknown>;
  published_at: string;
  provenance_status: VillaniV2ProvenanceStatus;
  attributes: Record<string, unknown>;
}

export interface VillaniVerifierCapabilityV2 {
  schema_version: "villani.verifier_capability.v2";
  capability_id: string;
  verifier_name: string;
  verifier_version: string | null;
  evidence_kinds: string[];
  task_categories: string[];
  supports_acceptance_grade: boolean;
  limits: Record<string, unknown>;
  published_at: string;
  provenance_status: VillaniV2ProvenanceStatus;
  attributes: Record<string, unknown>;
}

export interface VillaniPolicyPublicationV2 {
  schema_version: "villani.policy_publication.v2";
  publication_id: string;
  policy_id: string;
  policy_version: string;
  published_at: string;
  effective_at: string;
  expires_at: string | null;
  digest: string;
  scope: {
    organization_id: string | null;
    workspace_id: string | null;
    project_id: string | null;
    repository_id: string | null;
  };
  rules: Record<string, unknown>;
  provenance_status: VillaniV2ProvenanceStatus;
  attributes: Record<string, unknown>;
}

export interface VillaniTelemetryEnvelopeV2 {
  schema_version: "villani.telemetry_envelope.v2";
  event_id: string;
  idempotency_key: string;
  occurred_at: string;
  observed_at: string;
  sequence: number;
  sequence_scope: string;
  organization_id: string | null;
  workspace_id: string | null;
  project_id: string | null;
  repository_id: string | null;
  run_id: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  attempt_id: string | null;
  source: string;
  kind: string;
  name: string;
  status: string;
  resource: VillaniResourceV2;
  attributes: Record<string, unknown>;
  body: Record<string, unknown>;
}

export type VillaniProtocolDocumentV2 =
  | VillaniResourceV2
  | VillaniArtifactDescriptorV2
  | VillaniOutcomeV2
  | VillaniSpanV2
  | VillaniAgentCapabilityV2
  | VillaniVerifierCapabilityV2
  | VillaniPolicyPublicationV2
  | VillaniTelemetryEnvelopeV2;

export function readVillaniV2Json(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8")) as unknown;
}
