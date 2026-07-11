# Villani distribution

This is the supported end-user distribution. A platform wheel installs the `villani`, `villani-code`, `villani-agentd`, and `vfr` commands with one `pipx install villani` operation. The wheel depends on the separately versioned internal Python distributions and contains a platform-native Flight Recorder executable, so Node.js is not a runtime dependency.

The internal distributions remain independently installable for monorepo development. Release-candidate wheels must be built through `scripts/build-release.py`; a source build without a staged Flight Recorder executable is intentionally not a complete user artifact.
