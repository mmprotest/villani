# Villani self-service install, operation, and recovery

Villani 1.0 supports Windows, macOS, and Linux only through the matching release
archive produced and certified on that operating system in CI. The exact version is
shown by `villani --version`, `villani version --json`, Doctor, and Console Settings.
All Villani packages in one release use that same version.

## Install a standalone archive

A release consists of `villani-<version>-<os>-<architecture>.zip`, `SHA256SUMS`,
`update-feed.json`, and certification reports. The ZIP contains native commands,
release notes, a strict package manifest, and a CycloneDX SBOM. It does not require a
source checkout, Python, Node.js, or a sibling `node_modules` directory.

On Linux, verify and install from an empty directory:

```console
sha256sum -c SHA256SUMS
unzip villani-1.0.0-*.zip -d villani-installer
HASH=$(awk '/villani-1.0.0-.*\.zip/ {print $1; exit}' SHA256SUMS)
./villani-installer/villani install --artifact ./villani-1.0.0-*.zip --sha256 "$HASH"
export PATH="$HOME/.villani/bin:$PATH"
villani doctor --installation-only
```

On macOS, use `shasum -a 256 -c SHA256SUMS` for the checksum step, then run the
same remaining commands.

On Windows PowerShell:

```powershell
$archive = Get-Item .\villani-1.0.0-windows-*.zip
$expected = (Get-Content .\SHA256SUMS).Split()[0].ToLowerInvariant()
$actual = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "Villani archive checksum mismatch" }
Expand-Archive $archive .\villani-installer
.\villani-installer\villani.exe install --artifact $archive --sha256 $expected
$env:Path = "$HOME\.villani\bin;$env:Path"
villani doctor --installation-only
```

Add the same `~/.villani/bin` directory to the shell's persistent user PATH after
the first verified run. On Windows, always run updates through
`%USERPROFILE%\.villani\bin\villani.cmd`; the stable launcher keeps the active
executable outside the atomically switched directory.

For an offline installation, move only the ZIP and `SHA256SUMS` across the offline
boundary, verify the checksum there, and run the same `install` command. Installation
does not contact an update service. Preserve the ZIP and checksum as rollback and
audit evidence.

## Guided setup without YAML

From the root of an existing Git repository, run:

```console
villani setup
```

Setup detects the repository, Git, Villani Code, Codex, Claude Code, provider
authentication, model identity, supported protocol, conformance, and available local
qualification evidence. It chooses the strongest safe available initial coding route,
keeps delivery non-destructive by default, and stores configuration and runs locally.
The initial unrated system remains visibly Experimental and may make only the
setup-selected verifier-gated bootstrap attempt; this creates no qualification or
acceptance authority. A candidate can be accepted only through acceptance-grade
verifier evidence.

Use `--coding-system villani-code`, `--coding-system codex`, or
`--coding-system claude-code` to choose an installed system explicitly. Setup reports
one exact authentication or conformance repair action when a selected system is not
ready. Normal setup never requires YAML. Advanced configuration files remain supported,
but every loaded file is schema and migration validated and newer unknown versions fail
closed.

After setup:

```console
villani doctor
villani run "Fix the failing repository test"
villani open
```

## User-controlled updates and rollback

Villani never checks or installs an update merely because a command starts. Select a
release feed and channel explicitly:

```console
villani update channel stable --feed /path/or/https/url/update-feed.json
villani update channel beta --feed /path/or/https/url/update-feed.json
villani update channel pinned --version 1.0.0 --feed /path/or/https/url/update-feed.json
villani update status
villani update check
villani update preview
villani update install
```

An update check sends only an HTTP GET with the installed Villani version in the user
agent. It never uploads source, prompts, diffs, repository identity, or terminal data.
Plain HTTP is rejected except on loopback. `preview` checks configuration, spool, and
run-protocol compatibility and lists migrations without changing them.

Install verifies the feed digest, archive boundaries, exact package contents, every
file digest and size, the manifest, SBOM, commands, release notes, reported version,
startup, and installation-only Doctor. Configuration is backed up. The new version is
staged beside the old version, switched atomically, and rolled back automatically if
verification fails. Explicit rollback is:

```console
villani update rollback
villani doctor --installation-only
```

Interrupted updates are detected from their durable transaction journal on the next
status or Doctor invocation and restored fail closed. No update or rollback modifies a
repository.

## Doctor and exact repairs

`villani doctor --json` checks installation/package integrity, canonical versions,
service readiness, storage, configuration and migrations, repository and Git access,
harnesses and authentication, protocol/model/provider identity, permissions,
worktree isolation, validation and qualification, stale runs, dead letters, update
state, entitlement state, bounded logs, and disk space. Each check records:

- what passed or failed;
- whether a repository was modified (Doctor always reports `false`);
- one exact repair action for every failure; and
- the local evidence path.

Common recovery commands are deliberately explicit:

```console
villani service restart
villani update rollback
villani cleanup --json
villani doctor --installation-only
```

`cleanup` is a preview unless `--apply` is supplied. It can remove only expired update
downloads/transactions/failures, inactive Windows command runners, and excess log
backups. It never selects run bundles, evidence, configuration, licenses, or a current
or previous installation.

## Privacy-preserving support bundles

Support archives are opt-in, local, and never uploaded automatically. First inspect the
manifest:

```console
villani support preview --json
villani support preview --run RUN_ID
villani support create --run RUN_ID --confirm-manifest --json
```

By default Villani removes secrets, prompts, source, diffs, repository names, usernames,
absolute paths, and terminal content. It includes versions, public schemas, bounded
redacted logs, Doctor output, failure codes, and only allowlisted evidence from run IDs
selected explicitly. The archive contains its final manifest and checksum. Review it
locally before sharing it by a channel you choose.

## Daily repository workflow

Run the CLI or `villani open` from a repository root. Console retains exactly the four
primary pages: New task, Activity, Agents, and Settings. Activity's Repeat task action
prefills a new task without mutating the previous run. Completed results can copy a
proof summary and deep-link to recorded evidence.

Delivery remains explicit:

```console
villani run "Fix the parser edge case" --delivery suggest
villani run "Fix the parser edge case" --delivery branch
villani run "Fix the parser edge case" --delivery pull-request
```

Branch delivery uses a separate worktree and leaves the original checkout unchanged.
Pull-request delivery is a Pro feature and still requires acceptance-grade evidence.
Later corrections and reversions are imported explicitly; Villani does not passively
monitor a repository:

```console
villani verification feedback-import RUN_ID --outcome corrected_before_use --correction-summary "Adjusted the boundary case"
villani verification feedback-import RUN_ID --outcome reverted --linked-reference REVERT_COMMIT
villani verification feedback RUN_ID
```

Adverse imported outcomes quarantine only the exact recorded agent-system identity from
automatic use; audit history is appended, never rewritten.

## Free and Pro entitlements

Entitlement checks are centralized and offline. `villani license install PATH` verifies
and atomically installs a signed local license; `villani license status` performs no
network request. No source or repository data enters licensing.

Free retains one configured coding system, worktree isolation, verification and proof,
manual delivery, Activity basics, Doctor, support bundles, updates, and permanent access
to recorded evidence. Pro enables multi-harness qualification, automatic routing and
escalation, adaptive verification, persistent repository learning, pull-request
delivery, analytics, and advanced export.

Each signed license declares an offline grace period from 0 through 90 days, shown in
Settings and `license status`. During grace, Pro remains active. After grace, Pro-only
actions lock, but core safety remains available: evidence stays readable and accepted
runs stay verifiable. Development fixtures are signed but remain disabled unless the
explicit development-only environment gate is set; they are never a production license.

## Release evidence

Every platform CI job builds and inspects its own artifact, installs it outside the
checkout, exercises setup/service commands, Doctor, update and rollback, support bundle,
dependency audit, package/secret scan, and performance targets. Linux release
certification additionally requires an external ClamAV database and malware scan.
Reports are retained beside the built archive. A platform is not supported for a release
whose matching certification job is not green.
