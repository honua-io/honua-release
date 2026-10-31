// S9 observes the unmodified page and its OGC job responses; it never polls on the page's behalf.
export function gpExecutionSettled() {
  const out = document.querySelector("#gp-exec-out")?.textContent || "";
  const pill = document.querySelector("#gp-exec-pill")?.textContent || "";
  return /successful\. Results:/.test(out) ||
    /·\s*(done|failed|dismissed)|poll timeout|job failed|job dismissed|error|HTTP \d/i.test(pill);
}

export function gpRenderedExecution() {
  const out = document.querySelector("#gp-exec-out")?.textContent || "";
  return {
    pill: document.querySelector("#gp-exec-pill")?.textContent?.trim() || "",
    out: out.length <= 16000 ? out : out.slice(0, 8000) + "\n…\n" + out.slice(-8000),
    summary: document.querySelector("#gp-result-summary")?.textContent?.trim() || "",
  };
}

export class GpExecutionTrace {
  constructor(page, base, clock = Date.now) {
    this.clock = clock;
    this.origin = new URL(base).origin;
    this.pending = new Set();
    this.started = new WeakMap();
    this.pollCount = 0;
    page.on("request", (request) => this.started.set(request, clock()));
    page.on("response", (response) => {
      const url = new URL(response.url());
      const poll = response.request().method() === "GET" && /^\/ogc\/processes\/jobs\/[^/]+$/.test(url.pathname);
      const submission = response.request().method() === "POST" &&
        /^\/ogc\/processes\/processes\/[^/]+\/execution$/.test(url.pathname);
      if (url.origin !== this.origin || (!poll && !submission)) return;
      const receivedAt = clock();
      const status = {
        jobId: poll ? decodeURIComponent(url.pathname.split("/").at(-1)) : null,
        httpStatus: response.status(),
        startedAt: this.started.get(response.request()) ?? receivedAt,
        receivedAt,
        body: null,
      };
      status.durationMs = receivedAt - status.startedAt;
      if (poll) {
        this.pollCount++;
        this.lastStatus = status;
      } else {
        this.submission = status;
      }
      const read = response.text().then((body) => {
        status.body = body.slice(0, 16000);
        status.bodyTruncated = body.length > 16000;
        if (submission) {
          try { status.jobId = JSON.parse(body).jobID ?? null; } catch { /* retain the verbatim response */ }
        }
      }, (error) => { status.bodyReadError = String(error.message); });
      this.pending.add(read);
      read.finally(() => this.pending.delete(read));
    });
  }

  start() { this.startedAt = this.clock(); }

  async evidence(page) {
    // A response body can still be arriving when the page settles or its wait times out.
    // Bound the drain so a broken connection cannot hide the original failure.
    let timer;
    await Promise.race([
      Promise.allSettled([...this.pending]),
      new Promise((resolve) => { timer = setTimeout(resolve, 1000); }),
    ]);
    clearTimeout(timer);
    const rendered = await page.evaluate(gpRenderedExecution).catch(() => ({}));
    const observedAt = this.clock();
    return {
      ...rendered,
      jobId: this.lastStatus?.jobId ?? this.submission?.jobId ?? rendered.pill?.match(/job\s+(gp-[\w-]+)/)?.[1] ?? null,
      lastStatus: this.lastStatus ? { ...this.lastStatus } : null,
      submission: this.submission ? { ...this.submission } : null,
      pollCount: this.pollCount,
      startedAt: this.startedAt ?? null,
      observedAt,
      elapsedMs: this.startedAt == null ? null : observedAt - this.startedAt,
    };
  }
}
