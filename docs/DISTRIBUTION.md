# Villani local distribution and lifecycle

## Release verification status

`python release-verification/run_release_gate.py --mode local` rebuilds every Python and Node
package, installs wheels into fresh environments without editable installs, runs the recorded
installed-user setup/sample/doctor/UI/secret-scan gate, checks compatibility, executes the connected
scenarios, applies PostgreSQL migrations, reconciles packaged consumers, and runs browser tests.
Official `--mode release` also fails when a required external scanner is
absent or did not execute.

## Architecture

The supported user installation is one platform wheel named `villani`. It depends on the versioned internal `villani-ops`, `villani-code`, and `villani-agentd` Python distributions and owns the user-facing entry points. Release builds compile the existing Flight Recorder TypeScript output with Bun into a platform-native executable and package that executable as `vfr`; Node.js and Bun are build-time tools and are not needed by an installed product.

The intended published command is `pipx install villani`. This pass builds local release candidates only and publishes nothing. Monorepo developers may continue installing internal distributions independently with `scripts/install-local.py`.

Each Windows, macOS, and Linux CI runner builds its own platform wheel and a self-contained ZIP containing `villani`, `villani-code`, `villani-agentd`, and `vfr`. PyInstaller produces the Python runtime executable; the native Flight Recorder executable is included alongside it. A platform is supportable only while its CI build and command smoke remain green.

## Guided setup and public service

`villani setup` is the supported first-run path. It detects a Git repository, loopback model
servers, model listings, cloud credential presence, supported local session sources, and current
service state. The selected model is written as an unrated bootstrap backend; unknown price stays
unknown. Configuration is validated, fsynced, backed up when replacing an existing file, and
activated with an atomic rename. Secret values remain in environment or supported credential
storage and are never written to the YAML or setup record.

Normal users manage the runtime as Villani Service:

```console
villani service status
villani service start
villani service stop
villani service restart
```

`villani service start --automatic` enables the platform user-level startup adapter:

- Linux: `systemd --user` unit under `~/.config/systemd/user`.
- macOS: launchd user agent under `~/Library/LaunchAgents`.
- Windows: a per-user Task Scheduler task triggered at logon.

Status reports running/installed state, automatic startup, PID health, log path, last error, and the
one Console URL. Starts and stops are idempotent, stale PID state is recovered, duplicate processes
are refused, and shutdown is bounded. The compatibility command `villani uninstall-service`
removes only the service definition and preserves configuration, run bundles, artifacts, and the
SQLite spool. Data removal requires both `--delete-data` and `--confirm-delete-data`; the deletion
target is safety checked. None of these default paths requires administrator privileges.

CI uses redirected service-definition roots and dry-run platform commands as the documented VM approximation. This validates exact unit/plist/task generation and command selection without changing the hosted runner's login session.

## Upgrade safety

Before managed commands and normal product execution, Villani checks the configuration version, SQLite `user_version`, and canonical run protocol major versions. Agentd is the single source of truth for the spool contract. Legacy daemon spools with the known table layout migrate idempotently from versions 0 through 3 to version 4 without rewriting runs, events, artifacts, retry state, dead letters, or local-import records. Dry-run checks never mutate the spool, and an existing version 4 spool opens unchanged. A newer unsupported config, spool, or protocol version stops the older executable instead of downgrading data.

## Checksums and signing

Release assembly is deterministic for identical inputs: archive member ordering, timestamps, permissions, and compression are fixed. `SHA256SUMS` is generated for every archive and verified in CI. Archives include an explicit unsigned-release-candidate record. Real signing credentials are intentionally absent; public release automation must replace that placeholder with platform and provenance signing before publication.
