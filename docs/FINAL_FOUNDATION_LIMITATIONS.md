# Final foundation limitations

This repository is a release candidate foundation, not a generally available service and not an
independently certified compliance product.

- No production cloud KMS/BYOK adapter has a real provider integration test. The development and
  fake-KMS providers must not protect production data.
- Production SAML and SCIM integrations are unsupported. OIDC production verification requires a
  deployment-supplied verifier.
- The local process rate limiter and metrics accumulator are not distributed systems.
- PostgreSQL integration/load tests require `VILLANI_TEST_POSTGRES_URL`; offline migration SQL and
  SQLite behavior do not substitute for them. This pass executed 8 PostgreSQL integration tests,
  the 100,000-event load smoke, live migrations, and backup/restore against PostgreSQL 16. CI now
  publishes JUnit and migration-SQL evidence and fails if those tests are skipped unexpectedly.
- The local release image had zero high/critical findings in the retained Docker Scout SARIF at the
  time of this pass. That result expires with its vulnerability database; connected releases must
  refresh it, and air-gapped releases must import a verified current database.
- Linux/macOS/Windows package jobs are configured in CI. A local Windows run does not prove the
  other operating systems.
- The web package executed all 10 pinned-Chromium Playwright scenarios in this pass. CI installs
  Chromium and its Linux dependencies and retains traces/screenshots only on failure.
- Deterministic fixture evaluation invokes no model and attempts no coding task. Its 20 protocol
  observations per strategy are schema fixtures, not routing-quality or economic evidence. No
  live-provider quality, savings, cost, availability, or latency claim is made.
- Implemented means source and focused tests exist; integration-tested means the executable
  cross-component path ran; production-tested requires a real provider/deployment exercise. The
  development fake KMS, SAML, and SCIM providers are not production integrations.
- SLOs are objectives whose production windows remain unmeasured. No SLA, compliance
  certification, or general-availability claim is made.
