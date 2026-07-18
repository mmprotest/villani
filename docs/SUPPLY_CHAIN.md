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

PT10 standalone archives also pass `scripts/scan-release-artifact.py`. That gate verifies
safe ZIP paths, exact manifest membership, every digest and size, the CycloneDX inventory,
and secret patterns across streamed member bytes. Local mode reports an unavailable
external malware engine as unavailable, never clean. Official Linux certification updates
ClamAV definitions and requires a successful archive scan; its version and findings are
retained in `release-artifact-malware-scan.json`. The isolated installed environment is
audited with `pip-audit`, and each OS retains `dependency-audit.json` plus the platform
certification beside the archive.
