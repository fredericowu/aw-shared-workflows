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
    uses: fredericowu/aw-shared-workflows/.github/workflows/code-review-reusable.yml@v1.1.0
    with:
      ci_check_name: pytest
    permissions:
      contents: read
      pull-requests: write
      issues: write
      statuses: write
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
