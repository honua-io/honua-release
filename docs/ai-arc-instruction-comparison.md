# Development comparison of operating instructions

The local AI-arc runner accepts `--instruction-variant guide-only` (default) or
`--instruction-variant guide-plus-skills`. Use this to measure instruction content
on setup, operations, services and Studio tasks. It does not demonstrate native
Claude/Codex skill discovery: both variants load project instructions through the
existing AGENTS.md mechanism, with skill, file and shell tools still disabled.

Use the same immutable candidate lock, model arm, goal variant (`A` or `B`), arc
specification, harness revision and independently provisioned fixture for each
run. Give each run a fresh run ID, output directory, evidence directory and slot.
Pass `--skills-package` to both trials. Run the baseline with
`--instruction-variant guide-only`, then the comparison with
`--instruction-variant guide-plus-skills`; retain every first attempt.
Held-out goals and non-local targets reject the new variant. No fixture is
provisioned by this feature.

Both runs use `certification/ai-arc/development-skills-package.v1.json`, a separate
immutable package pin that includes the operating skills. Fetch its source files:

```bash
python3 tools/ai_arc_runner.py pinned-inputs --skills-package --out /path/to/pinned
```

Use the returned quickstart package source directory for both trials and the
comparison. This pin changes the development arc digest. It cannot enter the
normal aggregate, which retains the sealed arc and historical package pin.
The development package rejects held-out goals and non-local targets.
Its source manifest pins every file under the five canonical skill directories (`honua-install`,
`honua-services`, `honua-studio-maps`, `honua-ops`, `honua-tuning`), including their
references, as well as the normal packager inputs. The product packager must
produce `agent-files.sha256`. The loader checks the complete file set and hashes,
rejects symlinks and traversal, preserves the base AGENTS.md bytes, and appends
all skill files in path order. The receipt records the base package digests,
individual skill digests and rendered instruction digest. It never changes the
published package or baseline guide.

After both runs have observed pass/fail outcomes for all four stages, compare:

```bash
python3 tools/ai_arc_instruction_compare.py \
  --guide-only baseline/trial-receipt.json \
  --guide-plus-skills skills/trial-receipt.json \
  --guide-only-evidence baseline-evidence \
  --guide-plus-skills-evidence skills-evidence \
  --quickstart-package-source /path/to/pinned/server-sources \
  --lock-path /path/to/candidate-lock.json \
  --lock-digest sha256:REPLACE_WITH_LOCK_DIGEST
```

The command independently rebuilds the pinned package and verifies both receipts
against their retained evidence and current harness. It rejects mismatched model,
candidate, goals or harness, model claims that differ from the pinned arm or
retained sessions, missing skill provenance, and blocked/unavailable
stages. The JSON report includes each stage's baseline and skill result, their
binary difference, safety verification, and the receipt hashes. A single pair is
descriptive evidence; it provides no statistical significance or certification.
The normal baseline aggregate rejects guide-plus-skills runs, so these results
cannot enter a preregistered cohort accidentally. No measured improvement is
claimed until an actual governed fixture and both first-attempt runs exist.
