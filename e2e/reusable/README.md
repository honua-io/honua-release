# e2e/reusable/

The Slice-1 local-docker gate is a **reusable (`workflow_call`) workflow**. GitHub Actions requires
reusable workflows to live under `.github/workflows/`, so the callable definition is:

    .github/workflows/e2e-local-docker.yml   (job: slice1)

It is invoked by the release train's `gate_e2e` (`release-train.yml`), runs on every PR that touches
`e2e/**`, and runs nightly (with `require_real=1`). The harness it drives lives here under `e2e/`:

    e2e/harness/   boot.sh · compose.candidate.yml · run_all.sh · lib/{common,report}.sh · seed/
    e2e/drivers/   mcp/ · studio/ · gp/ · protocols/ · formats/ · console/ · demos/

Run locally:

    bash e2e/harness/run_all.sh check   # static gates only (no image needed)
    bash e2e/harness/run_all.sh run     # boot -> seed -> drivers -> e2e/gate-report.json
