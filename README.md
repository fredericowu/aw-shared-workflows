# aw-shared-workflows

Canonical reusable GitHub Actions workflows shared across Frederico's personal
repos. One file, several callers, no copy-paste drift.

## What's here

| Workflow | Purpose |
|---|---|
| `.github/workflows/code-review-reusable.yml` | The Dev Team's two-round PR review → merge loop (`workflow_call`). |
| `.github/workflows/ai-review-reusable.yml` | PR-Agent's advisory AI review, status `devteam/ai-review` (`workflow_call`). |
| `.github/workflows/security-scan-reusable.yml` | Weekly Semgrep + Trivy + Gitleaks scan, SARIF to code scanning (`workflow_call`). |
| `.github/workflows/test.yml` | This repo's own CI — runs `tests/`. |

The three workflows are independent and a repo can run any combination of
them. They share **one `v*` tag line** rather than having a family each — the
ruleset that makes tags immutable targets `refs/tags/v*` only, so a separate
`ai-v*` or `sec-v*` family would be unprotected. The cost is that a fix to one
workflow cuts a tag the others' callers simply don't have to bump.

`tests/test_code_review_workflow_logic.py`,
`tests/test_ai_review_workflow_logic.py` and
`tests/test_security_scan_workflow_logic.py` extract the reusable workflows'
embedded step scripts by text-scanning them and execute them, so they assert
against the bytes CI actually runs. The first moved here from
`tekflox/aw-app-template` along with the logic it guards. **Run them before
tagging** — `python -m pytest tests/ -q` (157 tests).

## Pin an exact tag. Never a moving ref.

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
| `pull_request: synchronize` | Round 2 starts by itself on the coder's fix push. Before this, every round 2 needed a manual `/aw-review` comment. |
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

`synchronize` costs nothing on PRs that are not mid-fix-cycle: the gate
reads the PR's labels and refuses a non-round-2 synchronize before any paid
judge run.

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
- **Two labels** — `devteam:awaiting-fixes` (`#FBCA04`) and `devteam:needs-human`
  (`#B60205`). The round state machine lives in them, and
  `devteam:awaiting-fixes` is now also what admits an automatic round 2 on a
  fix push.
- **Optional `AW_REVIEW_TRUSTED_AUTHORS`** repo variable (comma-separated
  logins). Unset falls back to `fredericowu` in the gate's own code.

The full caller checklist, with the `gh` commands, is at the bottom of the
workflow file itself.

## `ai-review-reusable.yml`

PR-Agent's advisory review, published as the commit status
`devteam/ai-review` on the PR head SHA. Never a required check. Inference
runs through Agents Platform's OpenAI-compatible `/v1` door against the
dedicated `pr-agent-llm` agent.

A whole thin caller — this is the entire file a consumer repo needs:

```yaml
name: AI Review

on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]
  issue_comment:
    types: [created]

concurrency:
  group: ai-review-${{ github.event.pull_request.number || github.event.issue.number }}-${{ github.event.sender.type == 'Bot' && 'bot-noop' || 'review' }}
  cancel-in-progress: true

jobs:
  ai-review:
    uses: fredericowu/aw-shared-workflows/.github/workflows/ai-review-reusable.yml@v1.2.0
    permissions:
      contents: read
      pull-requests: write
      issues: write
      statuses: write
    secrets:
      AW_PRAGENT_V1_KEY: ${{ secrets.AW_PRAGENT_V1_KEY }}
```

`synchronize` **is** in that trigger list, unlike the code-review caller
above. That is deliberate: PR-Agent reviews every push by design, whereas
the review→merge loop is driven by explicit rounds.

### Inputs

| Input | Required | Default | Notes |
|---|---|---|---|
| `api_base` | no | `https://agents-platform.aw.tekflox.com/v1` | Fed to `OPENAI__API_BASE`. An input only so a staging Agents Platform can be pointed at without forking the file; every caller today uses the default. |

That is the whole input surface. The model (`openai/agent/pr-agent-llm`), the
empty fallback list, the token/timeout values, the auto-flags
(describe + review on, `/improve` on demand only) and the SHA-pinned
`The-PR-Agent/pr-agent@efdde4a` (v0.49.0) are all **hardcoded**. They do not
vary per repo, and parameterizing a thing that does not vary is how
`code-review.yml`'s three copies drifted 554 lines apart.

`runs-on: ubuntu-latest` is hardcoded too, in all three jobs, and there is no
`runs_on` input. This workflow has an open `pull_request` trigger, so on a
public caller anyone can open a fork PR, and a fork PR on
`[self-hosted, aw-baremetal]` is arbitrary code execution on the bare-metal
host that runs the whole workspace. There is also no `actions/checkout` —
PR-Agent reads the diff through the GitHub API, so untrusted code never
reaches the runner at all. A private caller therefore pays GitHub-hosted
minutes (~3–5 min/PR), which also keeps this workflow off the single
self-hosted runner AP-MT's CI, deploy and code-review loop already share.

### Permissions

All four lines above are required, **on both sides**, for the same
intersection reason as `code-review-reusable.yml` — see that section above;
the `v1.1.1` lesson applies identically here, and
`test_the_reusable_requests_every_scope_the_caller_checklist_demands` in the
new suite compares the two lists in both directions.

There is **no `checks: read`** here, and that is not an omission: nothing in
this workflow touches the Checks API. The gate resolves a fork with
`gh api repos/.../pulls`, and the review publishes *commit statuses*, which
are a different API from check-runs. Adding the scope would fail the parity
test.

### Secrets

| Secret | Used for |
|---|---|
| `AW_PRAGENT_V1_KEY` | PR-Agent's inference. **Must be workspace-bound** — an unbound Agents Platform ApiKey gives an opaque 500 at the `/v1` door, not a 401 — and scoped to `pr-agent-llm` alone, so a leak cannot dispatch any other agent. |

**Pass it by name, never `secrets: inherit`** — same account-boundary
silence documented above. The `ai-review` job reads it with an explicit
"unset" branch *before* invoking PR-Agent, so a missing secret is a one-line
CI error naming the repo and the secret. The original `ai-review.yml` had no
such guard and an empty key surfaced as a confusing 401 from the `/v1` door.

**Mint one per caller repo.** Not a shared key: per-repo revocation, same
convention as `AW_CODEREVIEW_V1_KEY`. Verify a fresh mint against the door
*before* setting it as a secret — 200 with exactly one model listed, 403 for
an out-of-scope agent, 401 for a garbage key:

```
curl -s -H "Authorization: Bearer $KEY" \
  https://agents-platform.aw.tekflox.com/v1/models | jq '.data | length'   # -> 1
gh secret set AW_PRAGENT_V1_KEY --body "$KEY" --repo <owner/repo>
```

### What the caller still owns

- **`concurrency`** — and the group expression above is **literal, not
  illustrative**. A reusable workflow's own `concurrency` keys on the shared
  file's identity, so it has to live in the caller; and the `bot-noop` arm is
  the `809a1a6` fix for CodeRabbit's auto-comment cancelling PR-Agent's own
  in-flight review (`cancel-in-progress` is evaluated at workflow level,
  before any job-level `if`, so the reusable's `sender.type != 'Bot'` guard on
  the `gate` job is correct but unreachable in time). The guard moved into the
  shared file and the key stayed in the caller, so the two halves of one
  invariant now live in different repos — which is why the caller checklist
  carries the exact string and two tests assert both halves.
- **Nothing else.** No labels, no repo variables, no `.pr_agent.toml` (that
  file is read from the default branch only, and every load-bearing value is
  set as a host env var in the reusable; `api_base` is rejected from a repo
  file outright). `tekflox/aw-app-template` keeps its own as documentation of
  that asymmetry.

### If you are editing a caller's own `ai-review.yml` in a PR

`pull_request` runs the caller's PR copy, so a conversion or pin-bump PR does
exercise its own `pull_request` path live. `issue_comment` — typing `/review`
or `/improve` — **always runs the default-branch copy**, so such a PR cannot
exercise its own comment path. Verify that on the next PR after merge rather
than burning time debugging it.

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

Hour 0 UTC was entirely unused across all 61 copies as of 2026-11-05.

### Inputs

| Input | Required | Default | Notes |
|---|---|---|---|
| `runs_on` | no | `'["self-hosted","aw-baremetal"]'` | JSON-encoded `runs-on`, consumed as `fromJSON(...)`. Pass `'"ubuntu-latest"'` from a caller that cannot reach those runners — see below, it **queues forever** rather than failing. |
| `sarif_upload` | no | `true` | Set **false** on a repo without GitHub Advanced Security: the code-scanning upload API refuses those outright, so the job would fail on a scan that worked. |

Everything else is hardcoded: the tool versions, Semgrep's rule packs,
Trivy's scanner set, the three categories and the report-only posture. They
do not vary per repo, and parameterizing a thing that does not vary is how
`code-review.yml`'s three copies drifted 554 lines apart.

### `runs_on` defaults to self-hosted — the opposite of `ai-review-reusable.yml`

That difference is load-bearing, not an inconsistency. The AI review has an
open `pull_request` trigger, so a fork PR on `[self-hosted, aw-baremetal]`
would be arbitrary code execution on the bare-metal host. This workflow
declares **only `workflow_call`** and is reachable just from the caller's
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
