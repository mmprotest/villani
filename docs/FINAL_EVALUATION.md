# Final deterministic evaluation

Command:

```console
python evaluation/final_gate.py --output evaluation/results/final-foundation.json
```

The locked fixture environment uses `deterministic-fixture-agent@1`, cheap/strong fixture models
at version 1, `closed-loop-system@v1`, `verification-graph@v1`, Python 3.11, and protocol v2.
There are 20 deterministic observations per strategy. Results are fixture measurements, not live
provider performance.

| Strategy | Verified success (95% Wilson CI) | False accept / reject | Cost per accepted | Wall ms | Attempts / escalations | Verifier cost |
| --- | --- | --- | --- | --- | --- | --- |
| Strong-only | 16/20, 80% (58.40–91.93%) | 0 / 1 | 2.5000 | 18,000 | 20 / 0 | 4.00 |
| Cheap-only | 10/20, 50% (29.93–70.07%) | 1 / 3 | 0.8000 | 10,000 | 20 / 0 | 2.40 |
| Cheap-first escalation | 16/20, 80% (58.40–91.93%) | 0 / 1 | 1.5625 | 22,000 | 32 / 12 | 5.00 |
| Strong-first | 16/20, 80% (58.40–91.93%) | 0 / 1 | 2.5000 | 18,400 | 20 / 0 | 4.00 |
| Adaptive candidate | 15/20, 75% (53.13–88.81%) | 0 / 2 | 1.4667 | 17,000 | 28 / 8 | 4.40 |

Cheap-first escalation produced six more fixture acceptances than cheap-only for 17.0 incremental
fixture cost units. The locked minimum for a savings claim is 30 observations per strategy, so
**no savings claim is supported**. Raw `fixture://` run references and all confidence intervals are
in the JSON report. No benchmark-specific production routing logic was added.

An opt-in live evaluation is intentionally separate:

```console
python evaluation/final_gate.py --live --output evaluation/results/live.json
```

It exits unless providers, credentials, immutable version locks, and an approved cost budget are
configured. Live evaluation is not part of ordinary CI and no live/paid measurement is claimed.
