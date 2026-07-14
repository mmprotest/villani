# Villani distribution

This is the supported end-user distribution. Install it with `pipx install villani`, then run:

```console
villani setup
villani doctor
villani open
```

The guided setup detects a repository and available models, validates the selected backend, writes configuration atomically, and offers to start Villani Service and run a disposable sample task. It does not ask for internal routing scores or require manual YAML editing.

The wheel depends on separately versioned internal distributions and bundles native observability assets, so Node.js is not a runtime dependency. Internal distributions remain independently installable only for monorepo development. Release-candidate wheels must be built through `scripts/build-release.py`; a source build without staged native assets is intentionally not a complete user artifact.
