# Workflow outcome browser baselines

Playwright scripts that perform a workflow in Console and Studio the way a person
does and write the raw `events.jsonl` transcript that `tools/outcome_eval.py`
counts (see `certification/OUTCOME-RECORDING.md`). They are the clicks side of
the sentences-versus-clicks comparison.

A run of these scripts is a recording, not a grade and not a gate receipt. The
independent grader writes `grade.json` by hand from the server readback and the
controls. Only an admitted run by the authenticated producer workflow counts.

| Script | Workflow | Ends with |
| --- | --- | --- |
| `upload-publish.spec.ts` | Upload a GeoJSON file, import it and publish it as a layer | `terminal-readback` when the feature service serves the three fixture rows under the given layer name |
| `build-app.spec.ts` | Compose a private app from one published layer, a legend and a layer list, and save a version | `terminal-readback` only when the saved package is an app; `stop` with a `capability:` reason when the product saves another family |

Share, style, sign-in and roles, incident recovery, migration and install have no
script here yet. Share needs a frozen audience tier and a second approver
identity; the others need fixtures or product capabilities that are not admitted.
Until then those baselines are NOT RUN.

## Counting rules

- `click`: one event per dispatched click.
- `field`: one event per typed field, selection or file choice. `characters` is
  the typed length, and 0 for a selection or a file. The transcript holds lengths,
  never typed values; the password is written nowhere.
- A file upload is one click (open the chooser) and one field action (pick the file).
- Opening a navigation menu and following a link is two clicks. Typing an
  address is a `navigation` event and adds no click.
- An action is counted when it is dispatched, after its target is ready (a
  clickable element, an editable field, an enabled list). If it then fails, a
  `retry` event with `dispatched: true` follows. A target that never becomes
  ready is a `retry` event with `dispatched: false` and adds no count.
- Grading reads are `observer` events and add no count.
- Each trial has one `dispatch` and exactly one end boundary. A script emits
  `terminal-readback` only when every part of the workflow contract it can
  observe holds; any unmet part, any exception and the workflow time limit end
  the trial with `stop`. Events are never rewritten after grading.

`build-app.spec.ts` judges saved version 1: its hash, its one layer (by title,
with a source), its two controls and its package family. It also checks that
the owner reads the draft, that the page shows a visible canvas and that a
signed-out request for the draft is refused with 401 or 403. It does not
operate the two controls, confirm the layer's source service or probe a visitor
route: the grader checks those, and the script must gain them before its hash
is frozen for an install that can save an app.

## Running

```sh
cd tools/outcome-eval/browser
npm ci
npx playwright install chromium
OUTCOME_BASE_URL=https://localhost:<console port> \
OUTCOME_API_URL=http://127.0.0.1:<api port> \
OUTCOME_PASSWORD_FILE=<file holding the operator password> \
OUTCOME_OUT=<new empty directory> \
npx playwright test upload-publish.spec.ts
```

`OUTCOME_REPETITION` labels the names a trial creates (default `dev`).
`build-app.spec.ts` adds the first published layer whose button matches
`OUTCOME_LAYER_PATTERN` (default: a layer published by `upload-publish.spec.ts`).

Each trial creates its own table, service or map and deletes nothing. Run it
only against a disposable install.

Give each trial a new output directory: the recorder refuses to overwrite an
existing `events.jsonl`. The directory receives `events.jsonl`, `context.json`, `readback.json`
(raw product responses and the names the trial generated), `diagnostics.log`
(page errors, failed requests and API statuses) and `final.png`. Read those
files before sharing them; they are not redacted.

### Shared hosts

Chromium aborts in-flight requests with `ERR_NETWORK_CHANGED` whenever a host
network interface changes, for example when another job starts a container
network. The lost request looks like a product failure. On a shared host, run
the browser in a loopback-only network namespace:

```sh
OUTCOME_BRIDGE_PORTS="<console port> <api port>" ./isolated.sh npx playwright test upload-publish.spec.ts
```

`isolated.sh` needs passwordless `sudo` for `unshare -n`. Check
`diagnostics.log` for `requestfailed` lines before treating a failure as a
product defect. The isolation method is part of what is frozen at admission.
