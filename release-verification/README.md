# Packaged release verification

Run `python release-verification/run_release_gate.py --mode local` for a local packaged build and connected verification. `ci` applies the same product assertions in CI. `release` additionally requires every official supply-chain scanner and refuses unavailable or stale evidence.

The gate always rebuilds tracked frontend output, validates HTML asset references, builds wheels and source distributions, installs wheels without editable installs, packs Node packages, and writes its evidence beneath `artifacts/latest`. A missing connected scenario, browser run, scanner, or reconciliation result is recorded as incomplete and fails the release verdict; it is never converted into a pass.
