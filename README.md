# aw-shared-workflows

Canonical reusable GitHub Actions workflows shared across Frederico's personal
repos. One file, several callers, no copy-paste drift.

## What's here

| Workflow | Purpose |
|---|---|
| `.github/workflows/code-review-reusable.yml` | The Dev Team's PR review → merge lifecycle: one blocking review, then clearance on green CI (`workflow_call`). |
| `.github/workflows/devteam-nudge-reusable.yml` | Posts a PR's live state to the Agents Platform so it can wake the coder / ask / wait (`workflow_call`). Called by the label caller. |
| `.github/workflows/devteam-nudge.yml` | This repo's own caller for the human `devteam:awaiting-fixes` label; the reference copy consumers get. |
| `.github/workflows/security-scan-reusable.yml` | Weekly Semgrep + Trivy + Gitleaks scan, SARIF to code scanning (`workflow_call`). |
| `.github/workflows/test.yml` | This repo's own CI — runs `tests/`. |
| `scripts/check_master_pin.py` | Lint: fails on any consumer `uses:` of this repo at a ref other than `@master`. See below. |

The workflows are independent and a repo can run any combination of
them. They share **one `v*` tag line** rather than having a family each — the
ruleset that makes tags immutable targets `refs/tags/v*` only, so a separate
`sec-v*` family would be unprotected. The cost is that a fix to one
workflow cuts a tag the others' callers simply don't have to bump.

`tests/test_code_review_workflow_logic.py` and
`tests/test_security_scan_workflow_logic.py` extract the reusable workflows'
embedded step scripts by text-scanning them and execute them, so they assert
against the bytes CI actually runs. The first moved here from
`tekflox/aw-app-template` along with the logic it guards. **Run them before
tagging** — `python -m pytest tests/ -q`.

## `@master` pin lint

Policy (owner decision, card 41e5bf3b-9510-8126): consumers reference this
repo at `@master`. **This supersedes the "Pin an exact tag" section below**,
which describes the older tag-pin convention and the immutable-tag ruleset;
that text is kept for history and for consumers not yet moved.

`scripts/check_master_pin.py` is the check (needs PyYAML):

```bash
python3 scripts/check_master_pin.py PATH [PATH ...]   # repo root, workflows dir, or a file
python3 scripts/check_master_pin.py /opt/aw-workspace/repos/*/   # sweep all local checkouts
```

Exit 0 clean, 1 on any `fredericowu/` or `tekflox/aw-shared-workflows/...@<ref>`
where `<ref>` is not `master` (tags, SHAs and other branches all fail), 2 on
usage error. Comment lines and trailing `# ...` are ignored. Tests:
`tests/test_check_master_pin.py`, run by this repo's `test.yml`.

Where it runs: it is deliberately a script, not a required check, because it
reads *consumer* repos and a CI job here cannot reach them. Use it as a step in
a consumer's own CI (download first, so a failed download fails the step):

```bash
curl -fsSL -o "$RUNNER_TEMP/check_master_pin.py" \
  https://raw.githubusercontent.com/fredericowu/aw-shared-workflows/master/scripts/check_master_pin.py
python3 "$RUNNER_TEMP/check_master_pin.py" .
```

or by hand over local checkouts. **Not yet wired into the drift-checker**: its
discovery (in `aw-app-maintenance-agents`) still matches only `v*` tags, so a
consumer moved to `@main` would not show up there; invoking this script from it
is a follow-up. Consumers still on tags will fail the lint until they move;
nothing here edits them.

## Pin an exact tag. Never a moving ref. (superseded, see `@master` pin lint)

```yaml
uses: fredericowu/aw-shared-workflows/.github/workflows/code-review-reusable.yml@v1.0.0
```

Not `@master`, and not a moving major alias like `@v1`. Tags here are
**immutable** — nothing is ever retagged. A change ships as a new tag, and each
consumer is bumped deliberately.

**That is enforced by GitHub, not by discipline.** A ruleset on this repo
(`Immutable version tags`, target `tag`, pattern `refs/tags/v*`) blocks
`update`, `deletion` and `non_fast_forward`, with **no bypass actors** — so not
even the repo owner can move a published tag. This matters because every caller
hands its *entire merge gate* to a pinned ref here: an unprotected tag is only
marginally better than `@master`, since anyone with push access could silently
re-point it and change three repos' merge logic with no commit, no diff and no
CI signal in any of them.

Caught by the Dev Team reviewer on AP-MT PR #6, which checked and found
`/tags/protection` 404ing and `/rulesets` empty while this file claimed
immutability. Verified after fixing, by attempting the attack rather than
trusting the API:

```
$ git push -f origin v1.0.1          # re-point to another commit
 ! [remote rejected] v1.0.1 -> v1.0.1 (push declined due to repository rule violations)
$ git push --delete origin v1.0.0
 ! [remote rejected] v1.0.0 (push declined due to repository rule violations)
```

All three tag SHAs unchanged afterwards. To add a new version you create a *new*
tag; that is still allowed (only `update`/`deletion` are blocked, not creation).

### `v1.1.3` is a DUD. Never pin it.

It points at exactly the same commit as `v1.1.2` and contains none of the fix
it was cut for. A shell-quoting error made the `git commit` fail while the
`git tag` and both pushes succeeded, so the tag was created on the unchanged
`HEAD`. The content is identical to v1.1.2, so pinning it is harmless rather
than broken — it is simply a lie about which version you are on. The fix it
was meant to carry shipped as **`v1.1.4`**.

This is the immutability rule working as designed and costing what it is
supposed to cost: the obvious repair is to re-point `v1.1.3`, the ruleset
refuses that for everyone including the owner, and so the version number is
burned instead of the history being rewritten under three consumers. Verify a
tag's *content* after cutting it, not just that the push succeeded:

```
gh api repos/fredericowu/aw-shared-workflows/contents/\
  .github/workflows/code-review-reusable.yml?ref=vX.Y.Z -q .content \
  | base64 -d | grep <something the release changed>
```

That rule is not stylistic. `app-release.yml` in this estate is referenced via a
moving `@master` ref from ~45 app repos, and one fail-loud edit there turned the
whole marketplace red simultaneously. Exact tags make the blast radius of a
change exactly one repo at a time.

**Rollout order for a new tag:** bump `tekflox/aw-app-template` first — it is the
oldest and most-exercised consumer, so it is the canary — then the rest.

A tag that changes the caller's **trigger surface** (as `v1.3.0` does) costs
the canary **two** PRs, not one: `issue_comment` and `workflow_run` both fire
against the *default-branch* copy of the caller's workflow, so the PR that
adds a trigger can never exercise the path that trigger enables. PR 1 lands
the triggers and the pin; PR 2 is the live proof.

## `code-review-reusable.yml`

A thin caller looks like this:

```yaml
name: Code Review

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize, closed]
  issue_comment:
    types: [created]
  # The CI workflow's `name:`, not its filename. "Test" here; "CI" on
  # aw-stack. This is what makes the merge event-driven — see below.
  workflow_run:
    workflows: ["Test"]
    types: [completed]

concurrency:
  group: code-review-${{ github.event.pull_request.number || github.event.issue.number || github.event.workflow_run.head_branch }}
  cancel-in-progress: false

jobs:
  review:
    uses: fredericowu/aw-shared-workflows/.github/workflows/code-review-reusable.yml@v1.3.0
    with:
      ci_check_name: pytest
    permissions:
      contents: read
      pull-requests: write
      issues: write
      statuses: write
      checks: read          # REQUIRED — see "Permissions" below.
      actions: read         # REQUIRED — see "Permissions" below.
    # Pass by NAME, not `secrets: inherit` — see "Secrets" below.
    secrets:
      AW_CODEREVIEW_V1_KEY: ${{ secrets.AW_CODEREVIEW_V1_KEY }}
      AW_DEVTEAM_MERGE_TOKEN: ${{ secrets.AW_DEVTEAM_MERGE_TOKEN }}
      AW_DEVTEAM_EVENT_SECRET: ${{ secrets.AW_DEVTEAM_EVENT_SECRET }}
```

### Triggers (`v1.3.0` — the caller contract changed)

All three `on:` entries above are load-bearing, and `v1.3.0` is the release
that made the last two required. A caller that bumps the pin without them
still reviews and still merges the easy case; it just quietly loses the two
behaviours the release exists for.

| Trigger | What it buys |
|---|---|
| `pull_request: synchronize` | A fix push to an already-reviewed PR is cleared to merge on green CI by itself, without a new deep review. |
| `workflow_run: [completed]` on the caller's own CI | The merge re-attempts when CI *actually* finishes, however long that takes. This replaced a 20-minute poll that gave up — see below. |

Three ways to get that block wrong, all of which fail silently:

- **`workflows:` takes the CI workflow's `name:`** — the human string at the
  top of the file, not the filename and not the job id. Confirm it:
  `gh api repos/<owner>/<repo>/actions/workflows --jq '.workflows[].name'`.
  Rename the CI workflow later and nothing breaks loudly; merges on the
  slow-CI path just stop happening.
- **The `|| github.event.workflow_run.head_branch` arm of the concurrency
  group is not cosmetic.** A `workflow_run` event has neither
  `pull_request.number` nor `issue.number`, so without it every such run in
  the repo collapses into the single group `code-review-` and, with
  `cancel-in-progress: false`, queues one at a time.
- **`workflow_run` only fires for a trigger definition already on the
  DEFAULT BRANCH.** The PR that adds this block cannot exercise what it
  enables. Land it, then prove it on the next PR. (That is why v1.3.0's
  canary took two PRs on aw-app-template.)

`synchronize` never buys a second deep review: after a completed review 1
the gate clears the new head without one, and before it the push is reviewed
as review 1.

### How the merge works (and why it stopped polling)

Up to `v1.2.1`, the review job finished by polling `gh pr checks` in a
20-minute loop until the caller's CI went green, then merged inline. That
shape produced two separate production bugs on its own (a masking empty read
on AP-MT PR #7, a missing `actions: read` on PR #9), spent a 20-minute window
on every PR whose CI finished after the verdict, and capped the whole loop:
a suite slower than 20 minutes could never merge at all.

`v1.3.0` replaces it with a commit status plus a real event:

1. The review job publishes **`devteam/merge-eligible`** (`state=success`) on
   the PR head SHA at the moment it computes `eligible=true`, and never
   otherwise. Eligibility is still decided in exactly one place — the round
   rule, the `blocks_merge` rule and the draft rule are unchanged.
2. A **`merge-attempt`** job reads that status live and merges. Two entry
   points, one script: the same run (`needs: [gate, code-review]`) for the
   common case where CI was already green, and the caller's forwarded
   `workflow_run` event for the case the poll existed for.

A **commit status**, not a label, because eligibility is a property of a
*SHA*. A label is PR-scoped and would survive the push that invalidates the
review that set it, handing a brand-new unreviewed head the eligibility its
predecessor earned. That per-SHA property also *is* the head-moved guard,
which is new: `merge-attempt` re-reads the PR's live head before merging, and
a head that moved simply has no status on it. The old inline merge merged
"the PR", not "the SHA the review passed".

What it costs, stated plainly so nobody rediscovers it as a bug:

- **No poll means no self-healing window.** A dropped `workflow_run`
  delivery, or a `merge-attempt` that fails mechanically, is not retried by
  anything. `/aw-review` (or another push) is the recovery path, and it is
  deliberately kept for exactly this.
- **Every completion of the named CI workflow, on any branch, spawns a
  few-second `merge-attempt` job**, which exits neutral when no open PR owns
  that SHA. On the two self-hosted callers that is real, small, runner noise.
- **A renamed CI workflow silently kills the event path.** It degrades to
  manual `/aw-review`; it never merges red, because `merge-attempt` reads the
  checks itself.

In exchange the 20-minute ceiling is gone (slow CI still merges), and a
re-run of a failed CI job that passes the second time now auto-merges.

### Inputs

| Input | Required | Default | Notes |
|---|---|---|---|
| `ci_check_name` | yes | — | The caller repo's own CI check that must be **present and passing** on the PR head SHA before a merge. `coverage` on aw-app-template, `pytest` on AP-MT, `unit` on aw-workspace core. Verify the real name with `gh pr checks` — don't trust the YAML job id alone. |
| `runs_on` | no | `'"ubuntu-latest"'` | JSON-encoded `runs-on`, consumed as `fromJSON(...)`. A JSON string or a JSON array both work (`'["self-hosted","aw-baremetal"]'`). |
| `dispatch_url` | no | `https://agents-platform.aw.tekflox.com` | Agents Platform base for the reviewer dispatch. |
| `event_url` | no | `https://agents-platform.aw.tekflox.com` | Agents Platform base for the round-outcome event. |

### Permissions

All six lines in the example above are required. **`checks: read` and
`actions: read` are the ones that bite**, because leaving either out is
completely invisible on a public repo and fatal on a private one.

**A scope must appear on BOTH sides, and a caller grant alone buys nothing.**
The effective `GITHUB_TOKEN` is the *intersection* of the caller's `uses:`-job
block and this workflow's own top-level `permissions:`. Once a `permissions:`
key names any scope, every scope it does **not** name is forced to `none`
rather than inherited (GitHub docs, `permissions`: *"If you specify the access
for any of these permissions, all of those that are not specified are set to
`none`"*). So an omission in the shared file is an explicit `none` that caps
every caller to zero.

v1.1.1 got this exactly wrong, and it is worth stating plainly because the
release was *named* for the fix: it added `checks: read` to all three callers
and to the caller checklist, and never touched its own `permissions:` block.
Net effect was no fix at all, just a clearer error message. Caught in review on
`aw-app-template#17`, not by CI, because nothing compared the two lists.
**v1.1.2** fixes the block and adds
`test_the_reusable_requests_every_scope_the_caller_checklist_demands`, which
parses both lists and fails if they diverge.

The merge gate reads the PR's checks with `gh pr checks`, which goes through
the Checks API. On a **private** repo that read is refused without
`checks: read` — and it does not error, it returns an **empty list**. The gate
then says:

```
refusing to merge — required check(s) ['pytest'] did not report a pass
          on this head SHA. Saw: []
```

which reads like a CI failure and sends you to debug the wrong workflow. It is
deterministic, not flaky: measured on AP-MT PR #7 as four consecutive
`/aw-review` rounds returning empty instantly, including ~40 minutes after
`pytest` had already gone green. The identical caller on the **public**
aw-app-template merged on the first attempt, because a public repo's check
rollup is readable without the scope.

As of `v1.1.1` the gate at least distinguishes the two cases — empty stdout
*with* stderr content is treated as a failed read and retried, rather than as
"zero checks exist". That makes the symptom a visible retry-then-refuse
instead of an instant false negative; it is not a substitute for granting the
scope (and on v1.1.1 specifically it was the *only* thing that landed — see
above). The reader kept that three-way answer when the merge moved into
`merge-attempt` in v1.3.0: it is what the three-attempt retry budget hangs
off, and the budget covers a *failed read* only, never a CI run that is still
going.

`actions: read` (**v1.2.1**) is a third, separate gap in that same call. `gh
pr checks` always embeds `checkSuite{workflowRun{...}}` in its GraphQL query
regardless of the `--json` fields requested (gh v2.97.0,
`api/query_builder.go`), and resolving that edge is an *Actions*-resource
read. On a private caller, missing it is a hard `Resource not accessible by
integration (...checkSuite.workflowRun)` on every read. Live case: AP-MT PR
#9, CI green for hours, gate refused to merge anyway.

**v1.1.4** makes the check read able to fail *loudly*. Two defects inherited
from the original copies meant it could instead die with no explanation at
all: `gh pr checks` was called with no `timeout=`, so a hung call blocked the
step silently; and the job's `timeout-minutes` equalled the gate's own
20-minute deadline, so the job was killed before the gate could reach that
deadline and print its diagnostic. Measured on AP-MT PR #6: a round 2 that
**passed with zero findings** sat in that call for 17m53s, emitted nothing
between `##[endgroup]` and `The operation was canceled`, and cost a full paid
review cycle. The call is bounded, and remains so in `merge-attempt`. The
second half of that pair no longer has a defect to guard: v1.3.0 deleted the
deadline along with the poll, so there are no longer two budgets that could
overlap.

**v1.3.0 needs no new scopes.** `statuses: write` already covered publishing
`devteam/merge-eligible` and reading it back; resolving a PR from a SHA is a
`pull-requests` read. The two permission lists are therefore identical to
v1.2.1's, which is what the parity test requires.

### Secrets

**Pass them by name. Do not use `secrets: inherit`.**

All three are declared as `required: false` under `on.workflow_call.secrets`,
and callers pass each explicitly. `inherit` is not sufficient here, and it
fails *silently*: GitHub's rule is that "workflows that call reusable workflows
**in the same organization or enterprise** can use the `inherit` keyword" — it
does not cross an account boundary. This repo is owned by `fredericowu`, and
one of the callers is `tekflox/aw-app-template`.

Measured live (aw-app-template PR #16, 2026-11-04): the caller passed
`secrets: inherit`, `AW_CODEREVIEW_V1_KEY` **was** set on that repo, and it
still arrived as an **empty string** — while the same-owner caller
(`fredericowu/aw-workspace`) inherited it fine. That is the shape of bug that
reads as "the secret is missing" and sends you to the wrong repo.

Each secret is still read with an explicit "unset" branch, so a genuinely
missing one produces a fail-loud CI error naming the repo and the secret
rather than a confusing downstream 401 or 500.

| Secret | Used for |
|---|---|
| `AW_CODEREVIEW_V1_KEY` | Reviewer dispatch. **Must be workspace-bound** — an unbound Agents Platform ApiKey gives an opaque 500 at the `/v1` door, not a 401. |
| `AW_DEVTEAM_MERGE_TOKEN` | The squash-merge. A `GITHUB_TOKEN` fallback is refused, loudly: pushes it authors don't trigger downstream workflows, so the caller's deploy/release/image-build would silently stop firing. |
| `AW_DEVTEAM_EVENT_SECRET` | HMAC over the round-outcome event the platform verifies. |

### What the caller still owns

- **Every trigger** — a `workflow_call` workflow has none of its own, so the
  whole `on:` block above is the caller's, including the CI workflow's
  literal `name:` in `workflow_run.workflows`. See "Triggers" above; this is
  the half of the contract that fails silently when it drifts.
- **`concurrency`** — keyed on the caller's PR number, with the
  `workflow_run.head_branch` fallback arm. A reusable workflow's own
  `concurrency` would key on this file's identity instead.
- **No labels to create.** The round memory (`devteam:reviewed`) and the
  ceiling counter (`review-count:N`) are auto-created on first use.
  `devteam:needs-human` is gone: the gate neither writes nor reads it and
  deletes a stray one left on an old PR.
- **Optional `AW_REVIEW_TRUSTED_AUTHORS`** repo variable (comma-separated
  logins). Unset falls back to `fredericowu` in the gate's own code.

The full caller checklist, with the `gh` commands, is at the bottom of the
workflow file itself.

## `devteam-nudge-reusable.yml` (event-driven PR lifecycle)

A person adding `devteam:awaiting-fixes` calls this workflow (via the label
caller below), which reads the PR's live labels and `gh pr view` JSON
and posts one HMAC-signed call (`AW_DEVTEAM_EVENT_SECRET`, same secret as the
review events) to `<event_url>/api/github/devteam-pr-nudge`. The platform
decides wake / ask / wait / nothing; **this workflow holds no decision
logic.** `code-review-reusable.yml` has no nudge job: its review events carry
`nudge_follows: false`, so the platform wakes the coder directly.

- **Never fails a PR.** Every path exits 0: an unset secret, a 404 (platform
  not deployed yet) or a persistent 5xx is a warning. Retries 3 times on
  transport errors and 5xx.
- **Skips quietly** when the PR is not open, has no `aw-run:`/`aw-card:`
  label, or (label trigger) the label is not a still-live
  `devteam:awaiting-fixes`.
- `trigger_kind` is `review_done`, `push_unreviewed`, `ci_done`, `comment`
  or `label` (the review workflow only ever sends `label` now). When the platform did not wake
  anyone, the job posts one short PR comment with the verdict and reason.
- Never checks out PR code (the label caller is `pull_request_target`).

The **human label trigger** needs a per-repo file: copy
[`.github/workflows/devteam-nudge.yml`](.github/workflows/devteam-nudge.yml)
as is. Labels written by the pipeline itself use `GITHUB_TOKEN` and start no
workflow, so only a person (or a PAT) adding `devteam:awaiting-fixes` fires it.
Roll out the platform route (AP-MT `/api/github/devteam-pr-nudge`) first; until
then the job logs a 404 warning and does nothing.

## `security-scan-reusable.yml`

Free SAST + SCA + secrets scanning as a no-cost alternative to Veracode:
three independent jobs (Semgrep, Trivy, Gitleaks), each uploading its own
SARIF under its own category. **Report-only** — every scan step is
`continue-on-error` and the gitleaks run swallows its own exit 1, so a finding
never fails the build. It is not a merge gate and must not become one without
a deliberate decision: a repo-wide gate that reddens 61 repos at once is the
failure mode `app-release.yml` already taught this estate.

A whole thin caller — this is the entire file a consumer repo needs:

```yaml
name: Security scan

on:
  schedule:
    - cron: "11 0 * * 1"
  workflow_dispatch: {}

jobs:
  security-scan:
    uses: fredericowu/aw-shared-workflows/.github/workflows/security-scan-reusable.yml@v1.6.0
    permissions:
      actions: read
      contents: read
      security-events: write
```

**The cron lives in the caller and cannot be an input.** GitHub Actions does
not execute `on.schedule` from a called workflow — triggers always belong to
the caller, so a schedule in the shared file would simply never fire. Each
caller therefore keeps its own staggered slot plus `workflow_dispatch: {}`,
which is also what makes a rollout verifiable on demand
(`gh workflow run "Security scan" --repo <owner>/<repo>`).

**Pick an unused slot.** The stagger exists so 60+ repos don't hammer the 4
shared self-hosted runners at once, and 8 repos already collide on
`"45 2 * * 3"` from a rollout that copy-pasted one. Check first:

```bash
grep -h 'cron:' repos/*/.github/workflows/security-scan.yml | sort | uniq -c
```

Hour 0 UTC held no cron at all across the 61 standalone copies as of
2026-11-05 — but the `v1.6.0` canary rollout then claimed five hour-0 slots
itself (aw-stack, aw-backend, aw-console, aw-tekflox, aw-remote-host), so
that is a snapshot and not a standing fact. Re-run the grep rather than
reading "hour 0 is free" and picking another minute in it.

### Inputs

| Input | Required | Default | Notes |
|---|---|---|---|
| `runs_on` | no | `'["self-hosted","aw-baremetal"]'` | JSON-encoded `runs-on`, consumed as `fromJSON(...)`. Pass `'"ubuntu-latest"'` from a caller that cannot reach those runners — see below, it **queues forever** rather than failing. |
| `sarif_upload` | no | `true` | Set **false** on a repo without GitHub Advanced Security: the code-scanning upload API refuses those outright, so the job would fail on a scan that worked. |

Everything else is hardcoded: the tool versions, Semgrep's rule packs,
Trivy's scanner set, the three categories and the report-only posture. They
do not vary per repo, and parameterizing a thing that does not vary is how
`code-review.yml`'s three copies drifted 554 lines apart.

### `runs_on` defaults to self-hosted — the opposite DEFAULT of `ai-review`'s

Both workflows take the same JSON-encoded `runs_on` input; they differ only in
which way it defaults, and that difference is load-bearing rather than an
inconsistency. The AI review has an open `pull_request` trigger, so a fork PR
on `[self-hosted, aw-baremetal]` would be arbitrary code execution on the
bare-metal host — it therefore defaults to hosted and a public caller leaves
it there. This workflow declares **only `workflow_call`** and is reachable
just from the caller's
`schedule`/`workflow_dispatch`, which can run nothing but trusted
default-branch code.

> **A caller must never add a `pull_request` or `pull_request_target`
> trigger.** Doing so converts the self-hosted default into exactly that RCE
> path. A caller that genuinely needs PR-time scanning must pass
> `runs_on: '"ubuntu-latest"'` in the same change.
> `test_the_reusable_declares_only_workflow_call` asserts this file's half.

`runs_on` is an input at all because the `aw-baremetal` label is served two
different ways here, and only one is portable: `tekflox/*` repos reach the org
runner group `aw-private` (4 shared runners), while some `fredericowu/*` repos
have their **own** repo-level runner. An org runner group cannot be shared
outside its org, so a personal-account repo with neither would **queue
forever rather than fail** — a silent hang. That is the live fact behind the
`aw-app-template` owner-conditional variant and `aw-app-uc-phd`'s
`ubuntu-latest`.

### `sarif_upload`, and why a failed upload stays red

Measured live on 2026-11-05: `tekflox/aw-stack` returns "Advanced Security
must be enabled for this repository to use code scanning", and
`fredericowu/aw-backend`, `aw-console` and `aw-tekflox` return "Code scanning
is not enabled for this repository" — 4 of the 5 canary repos, all private.
With `sarif_upload: false` the three `upload-sarif` steps are skipped and the
scans still run.

When it is `true`, the upload steps carry **no `continue-on-error`** on
purpose. On a repo that is supposed to be feeding its Security tab, an upload
that silently stops working is a reporting pipeline that looks healthy and
reports nothing. `aw-app-research-search`'s variant made the upload
best-effort instead, which also swallows a genuine regression on the repos
where upload is the whole point. An explicit per-caller boolean says which
repo is which. Check which case a repo is in *before* rolling out:

```bash
gh api repos/<owner>/<repo>/code-scanning/analyses --jq 'length'
```

### The one behaviour added vs the 61 standalone copies

All three `.sarif` files are **also** uploaded as run artifacts
(`if: always()`). A private caller with `sarif_upload: false` would otherwise
run three scans and keep none of their findings anywhere readable. It is also
what makes a canary verifiable on a private repo, where
`code-scanning/analyses` returns nothing by definition:

```bash
gh run view <run-id> --repo <owner>/<repo> --json artifacts
```

### Permissions

Both lines are required **on both sides**, for the same intersection reason as
the two workflows above — `security-events: write` missing on *either* side
403s every upload on a repo that *does* have Advanced Security, which looks
exactly like the no-GHAS failure `sarif_upload` exists for and sends you to
flip the wrong switch. The checklist lists both scopes even for a
`sarif_upload: false` caller, deliberately: the flag is configuration a repo
flips the day it gets Advanced Security, and a permissions block needing the
same edit is a second step someone will miss.

### What the caller still owns

- **The triggers** — cron slot and `workflow_dispatch`, per above. Not
  optional and not replaceable by an input.
- **`concurrency`** — none needed. A weekly cron has nothing to serialize
  against and the 61 standalone copies declared none.
- **Nothing else.** No secrets, no labels, no repo variables. The scan uses
  only the automatic `GITHUB_TOKEN`.

## Provenance

Extracted from `fredericowu/agents-platform-multitenant@2aab9cd5`'s
`.github/workflows/code-review.yml` — the third copy of a file that started life
in `tekflox/aw-app-template`. Behaviour is unchanged from that commit; only the
three axes of genuine variance between the copies became inputs, plus one fix
the extraction itself required (a reusable workflow's jobs report to
`gh pr checks` as `"<caller job> / <job>"`, so the merge gate's advisory-check
filter had to match on the name's last `/` segment too — the flat exact-name
match the originals used would have had the merge gate waiting on itself).

Every design comment in that file was kept. Read it before changing anything
here; most of what looks redundant is load-bearing and has a live incident
behind it.

`security-scan-reusable.yml` was extracted from the 97-line standalone
`security-scan.yml` rolled out to **61 repos** in September 2026
(`tekflox/aw-app-weather`'s copy is the representative one). Semantics are
unchanged — same three jobs, same SHA pins, same report-only posture, every
inline rationale carried across with the step it explains. Two things are
new: the `sarif_upload` input, and the SARIF run artifacts.

Those 61 copies were **not** identical when this was extracted, which is the
whole argument for converging them. Five distinct variants existed: 52 on the
canonical file, 6 (including `aw-app-template`) deriving `runs-on` from the
repo owner, `aw-app-uc-phd` hardcoding `ubuntu-latest`,
`aw-app-knowledgeable` with most design comments dropped, and
`aw-app-research-search` with a best-effort upload plus retained artifact. The
first three are all the same underlying runner-reachability fact, and they are
why `runs_on` is an input.

`ai-review-reusable.yml` was extracted from `tekflox/aw-app-template`'s
standalone `.github/workflows/ai-review.yml`, the only copy that existed —
AP-MT and aw-workspace never had one, which is the point of the extraction:
PR-Agent becomes part of the common block every rollout repo gets rather than
a one-repo feature. Behaviour is unchanged from that file, all three of its
jobs came across, and every design comment came with them. One thing was
*added* rather than ported: the fail-loud empty-secret guard, which the
original did not need because it read a repo secret directly instead of a
`workflow_call` one.

## The PR lifecycle (`code-review-reusable.yml`)

There is one lifecycle. It is not opt-in and there is no second version:
every caller pinned to `@master` follows it with no change in its own repo.
The `lifecycle` input is **deprecated and ignored**; it is still declared so a
caller that passes `lifecycle: v2` is not rejected, and it never branches
behaviour. Delete it from callers whenever convenient.

Review 1 is the only review that can block: a finding at or above
`block_severity` (default `medium`; a finding with a missing/unknown severity
always blocks) turns the `code-review` job red as its last step and withholds
`devteam/merge-eligible`. Any `pull_request` event after a completed review 1
(memory = a `github-actions[bot]`-written `devteam:reviewed`, or legacy
`devteam:fix-cycle`, label, read from the PR timeline; an unreadable timeline
fails closed) writes `devteam/code-review` + `devteam/merge-eligible` success
with one shared attempt URL and merges once CI is green, with no new deep
review. Drafts get no clearance; forks and untrusted authors never reach it. A
manual `/aw-review` or `/aw-improve` after review 1 is advisory only. The
per-PR review ceiling (`max_reviews`, `review-count:N`) only counts reviews
that returned a verdict: an attempt that errored (reviewer `aw_status=error`,
invalid verdict, dispatch failure) records a `review-error:<run id>-<attempt>` label
(once per run, so a failed-job rerun cannot double count) and, if a unit was
charged, is refunded with a `review-refund:N:<run id>-<attempt>` label (budget used = charged -
refunded). A completed review adds `review-ok`, and `max_errored_reviews`
(default 3) bounds the error events since the last `review-ok`, independently
of `max_reviews`, so a broken reviewer cannot loop. An errored review comments once per head on the
PR and wakes the opener coder (`review_error` trigger on the platform's
`devteam-pr-nudge` door, once per head). Two triggers for the same head
dedupe: the gate serialises per PR and a fresh in-flight
`devteam/code-review` pending status refuses the second one.

Labels: the gate writes only `devteam:reviewed` and `review-count:N`. It never
writes or reads `devteam:needs-human` (human-needed state lives on the Kanban
card / `ask_human`, not on the PR) and deletes a stray one left on an old PR;
a stray `devteam:awaiting-fixes` is dropped on push. There is no nudge job: the
review event carries `nudge_follows: false` so the platform wakes the coder
directly. The merge is reported by the org webhook; the merged-event job stays
until slice C and the platform dedupes both on one ledger key.
