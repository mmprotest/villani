# Final foundation limitations

This repository is a release candidate foundation, not a generally available service and not an
independently certified compliance product.

- No production cloud KMS/BYOK adapter has a real provider integration test. The development and
  fake-KMS providers must not protect production data.
- Production SAML and SCIM integrations are unsupported. OIDC production verification requires a
  deployment-supplied verifier.
- The local process rate limiter and metrics accumulator are not distributed systems.
- PostgreSQL integration/load tests require `VILLANI_TEST_POSTGRES_URL`; offline migration SQL and
  SQLite behavior do not substitute for every production topology.
- The local release image had zero high/critical findings in the retained Docker Scout SARIF at the
  time of this pass. That result expires with its vulnerability database; connected releases must
  refresh it, and air-gapped releases must import a verified current database.
- Linux/macOS/Windows package jobs are configured in CI. A local Windows run does not prove the
  other operating systems.
- Deterministic fixture evaluation has 20 observations per strategy, below the locked minimum for
  savings claims. No live-provider quality, cost, availability, or latency claim is made.
- SLOs are objectives whose production windows remain unmeasured. No SLA, compliance
  certification, or general-availability claim is made.
