# Villani distribution

This is the supported end-user distribution. Install it with `pipx install villani`, then run:

```console
villani setup
villani doctor
villani open
```

Standalone release archives can be installed without Python or a checkout by running
the extracted `villani install --artifact ARCHIVE --sha256 DIGEST` bootstrap command.
The managed installation provides user-controlled update/rollback, Doctor, local
privacy-redacted support bundles, cleanup previews, and offline entitlements. The full
cross-platform procedure is in `docs/SELF_SERVICE.md` at the repository root.

The guided setup detects a repository and available models, validates the selected backend, writes configuration atomically, and offers to start Villani Service and run a disposable sample task. It does not ask for internal routing scores or require manual YAML editing.

Setup also migrates the selected coding backend into a non-secret, content-addressed Villani Code
agent-system entry. Use `villani agents list`, `villani agents inspect`, and `villani agents doctor`
to inspect the complete route and qualification evidence; external harnesses remain disabled.

The sample is accepted only through the normal evidence path. Authoritative validation is projected
through `villani.validation_coverage.v1`; unrelated passing suites do not prove requirements, and a
focused probe is scheduled when coverage remains uncertain. Terminal, Console, reports, and static
run viewers all consume `villani.run_summary.v1`, so unknown accounting is displayed as unknown and
check totals cannot diverge between surfaces.

The wheel depends on separately versioned internal distributions and bundles native observability assets, so Node.js is not a runtime dependency. Internal distributions remain independently installable only for monorepo development. Release-candidate wheels must be built through `scripts/build-release.py`; a source build without staged native assets is intentionally not a complete user artifact.
