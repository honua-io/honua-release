# Skill evaluation results

The GA capability gate (`tools/ga_capabilities.py`, row `ga-capabilities`) fails the candidate when a
skill it ships (`skills/<name>/SKILL.md` in honua-server) has no passing with/without evaluation here.
The policy is `skillEvaluations` in `certification/ga-capabilities.v1.json`; the rule is the
skill-refresh plan's keep rule (main ruling, 22 Nov 2026).

One file per skill, `<name>.json`:

```json
{
  "schema": "honua.skill-eval/v1",
  "skill": "honua-ops",
  "skillTreeSha256": "sha256:…",
  "serverSha": "<candidate the runs used>",
  "observedAt": "2026-11-26T18:00:00Z",
  "evidence": "<https URL, or a file in this repository, holding the transcripts and grader output>",
  "models": {
    "opus": {
      "withSkill":    {"runs": 3, "successes": 3, "medianSeconds": 220, "medianToolCalls": 18},
      "withoutSkill": {"runs": 3, "successes": 2, "medianSeconds": 300, "medianToolCalls": 31}
    },
    "sol": {
      "withSkill":    {"runs": 3, "successes": 3, "medianSeconds": 250, "medianToolCalls": 22},
      "withoutSkill": {"runs": 3, "successes": 3, "medianSeconds": 260, "medianToolCalls": 24}
    }
  }
}
```

- `skillTreeSha256` is the digest of the files committed under `skills/<name>/` at HEAD of a server
  checkout: `python tools/skill_evals.py digest <server checkout> <name>`. Any edit to the skill
  changes it, so a rewritten skill needs a fresh evaluation.
- `serverSha` is the 40-character server commit the runs used; `observedAt` is a UTC `Z` stamp no
  older than the policy's `maxAgeDays`; `evidence` is an https URL or a path in this repository.
- `successes` counts independently verified successes, not the agent's own report.
- `medianSeconds` and `medianToolCalls` are medians over the arm's successful runs; they are `null`
  when the arm has no successful run.
- Equivalence: a record run on an earlier server commit still counts while the candidate commits the
  same skill content and the record is inside `maxAgeDays`. The receipt's `sourceObservedAt` is the
  oldest stamp it relies on, so admission rechecks the window.
- Keep rule, checked by the gate: every model in the policy has at least `minRunsPerArm` runs per arm
  and no lower success rate with the skill; and on at least one model the skill raises the success
  rate, or cuts the median wall time or median tool calls by `minCutPercent` or more when both arms
  have successful runs.
