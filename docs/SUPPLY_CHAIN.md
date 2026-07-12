# Supply-chain release gate

Run `python scripts/supply-chain-gate.py --output <directory>` followed by the repository secret,
migration, restore, package, and dependency-audit commands. The gate emits a CycloneDX SBOM,
SHA-256 checksum, and HMAC signature using an explicitly test-only key. Public artifacts remain
unsigned release candidates until an external release signing system replaces that key.

The offline container policy scan rejects root execution, floating `latest` tags, and retained pip
caches. It is not a CVE database scanner. Connected release CI must additionally run Trivy/Grype or
an equivalent pinned scanner against the built image and retain its database/version and report.
Air-gapped releases import a separately verified vulnerability database; absence of that database
is reported as unsupported, never as a clean vulnerability result.

The CI matrix defines Linux, macOS, and Windows package smokes. A local run proves only the current
operating system; the other jobs must be green in CI before publication.
