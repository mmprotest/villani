# Deployment modes

Villani supports these explicitly bounded modes. None implies a managed-service SLA.

- **Local-only:** the public `villani` CLI, local daemon, filesystem artifacts, and local run
  bundles. No control-plane or external dependency is required.
- **Hosted development:** `components/villani-control-plane/docker-compose.yml` starts PostgreSQL,
  a one-shot migration service, and the API. The committed credentials are disposable development
  values only.
- **Self-hosted Kubernetes:** `deploy/helm/villani-control-plane` supplies rolling API replicas,
  readiness/liveness probes, disruption budget, network policy, and a pre-upgrade migration Job.
  Operators supply the database, ingress, TLS, secrets, backup target, and image registry.
- **Hybrid:** execution remains local through the pull-based agent daemon. Only governed metadata,
  permitted artifacts, and outcomes synchronize to the hosted control plane. Repository checkout
  credentials stay in the local secret broker.
- **Air-gapped:** set `VILLANI_CONTROL_PLANE_AIR_GAPPED=true`, use filesystem object storage and
  local identity/key providers, pre-load images/packages, and disable OIDC, S3, external model,
  webhook, and OTLP endpoints. Startup fails if a network object store is configured.

Cloud KMS, production SAML, and production SCIM are not supported integrations in this release.
Their interfaces and deterministic fakes exist for adapter testing only.
