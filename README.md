# aw-shared-workflows

Canonical reusable GitHub Actions workflows shared across Frederico's personal
repos. One file, several callers, no copy-paste drift.

## What's here

| Workflow | Purpose |
|---|---|
| `.github/workflows/code-review-reusable.yml` | The Dev Team's two-round PR review → merge loop (`workflow_call`). |
| `.github/workflows/test.yml` | This repo's own CI — runs `tests/`. |

`tests/test_code_review_workflow_logic.py` extracts the reusable workflow's
embedded step scripts by text-scanning its heredoc markers and executes them,
so it asserts against the bytes CI actually runs. It moved here from
`tekflox/aw-app-template` along with the logic it guards. **Run it before
tagging** — `python -m pytest tests/ -q`.

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

## `code-review-reusable.yml`

A thin caller looks like this:

```yaml
name: Code Review

on:
  pull_request:
    types: [opened, reopened, ready_for_review, closed]
  issue_comment:
    types: [created]

concurrency:
  group: code-review-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: false

jobs:
  review:
    uses: fredericowu/aw-shared-workflows/.github/workflows/code-review-reusable.yml@v1.1.4
    with:
      ci_check_name: pytest
    permissions:
      contents: read
      pull-requests: write
      issues: write
      statuses: write
      checks: read          # REQUIRED — see "Permissions" below.
    # Pass by NAME, not `secrets: inherit` — see "Secrets" below.
    secrets:
      AW_CODEREVIEW_V1_KEY: ${{ secrets.AW_CODEREVIEW_V1_KEY }}
      AW_DEVTEAM_MERGE_TOKEN: ${{ secrets.AW_DEVTEAM_MERGE_TOKEN }}
      AW_DEVTEAM_EVENT_SECRET: ${{ secrets.AW_DEVTEAM_EVENT_SECRET }}
```

### Inputs

| Input | Required | Default | Notes |
|---|---|---|---|
| `ci_check_name` | yes | — | The caller repo's own CI check that must be **present and passing** on the PR head SHA before a merge. `coverage` on aw-app-template, `pytest` on AP-MT, `unit` on aw-workspace core. Verify the real name with `gh pr checks` — don't trust the YAML job id alone. |
| `runs_on` | no | `'"ubuntu-latest"'` | JSON-encoded `runs-on`, consumed as `fromJSON(...)`. A JSON string or a JSON array both work (`'["self-hosted","aw-baremetal"]'`). |
| `dispatch_url` | no | `https://agents-platform.aw.tekflox.com` | Agents Platform base for the reviewer dispatch. |
| `event_url` | no | `https://agents-platform.aw.tekflox.com` | Agents Platform base for the round-outcome event. |

### Permissions

All five lines in the example above are required. **`checks: read` is the one
that bites**, because leaving it out is completely invisible on a public repo
and fatal on a private one.

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
*with* stderr content is treated as a failed read and retried until the
20-minute deadline, rather than as "zero checks exist". That makes the
symptom a timeout instead of an instant false negative; it is not a
substitute for granting the scope (and on v1.1.1 specifically it was the
*only* thing that landed — see above).

**v1.1.4** makes the gate able to fail *loudly*. Two defects inherited from the
original copies meant it could instead die with no explanation at all:
`gh pr checks` was called with no `timeout=`, so a hung call blocked the step
silently; and the job's `timeout-minutes` equalled the gate's own 20-minute
deadline, so the job was killed before the gate could reach that deadline and
print its diagnostic. Measured on AP-MT PR #6: a round 2 that **passed with
zero findings** sat in that call for 17m53s, emitted nothing between
`##[endgroup]` and `The operation was canceled`, and cost a full paid review
cycle. The call is now bounded and retried, the job budget nests the gate's
instead of overlapping it, and both are asserted by tests.

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

- **`concurrency`** — keyed on the caller's PR number. A reusable workflow's own
  `concurrency` would key on this file's identity instead.
- **Two labels** — `devteam:awaiting-fixes` (`#FBCA04`) and `devteam:needs-human`
  (`#B60205`). The round state machine lives in them.
- **Optional `AW_REVIEW_TRUSTED_AUTHORS`** repo variable (comma-separated
  logins). Unset falls back to `fredericowu` in the gate's own code.

The full caller checklist, with the `gh` commands, is at the bottom of the
workflow file itself.

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
