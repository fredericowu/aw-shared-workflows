"""Regression tests for the executable logic and the load-bearing invariants
of ``.github/workflows/ai-review-reusable.yml``.

**Why this file exists.** That workflow is the PR-Agent review, extracted
from ``tekflox/aw-app-template``'s standalone ``ai-review.yml`` so every
rollout repo gets it from one place. Almost everything in it that can break
breaks *silently*: a dropped permission scope is an explicit ``none`` rather
than an error, a ``concurrency`` group copied without its ``bot-noop`` arm
cancels real reviews, a non-empty ``fallback_models`` sends the diff to a
metered third-party provider on any error, and a job budget that overlaps
the inference budget instead of nesting it kills the run with no diagnostic
at all. Each of those has a live incident behind it, and none of them is
reachable by running the workflow on a healthy repo — which is exactly the
class of defect that survived three copies of the sibling
``code-review-reusable.yml`` and two of its releases.

**How it works.** Same pattern as
``tests/test_code_review_workflow_logic.py``: the step scripts are
EXTRACTED from the workflow file by text-scanning it and then executed
directly, so these tests assert against the bytes CI actually runs. They
cannot drift from the workflow the way a hand-maintained copy of the logic
would — if someone renames a step, extraction fails loudly rather than
silently testing nothing.

Deliberately NOT parsed with PyYAML, for the same reason as the sibling
suite: adding a parser dependency to run a text scan would be incidental
coupling. pytest is the only requirement. (``test.yml`` does separately
``yaml.safe_load`` every workflow as a smoke check — that catches "no longer
a valid workflow", which is a different failure from "the logic is wrong".)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

WORKFLOW = (Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "ai-review-reusable.yml")


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _dedent_block(block: str) -> str:
    """Strip the common leading indentation a YAML block scalar carries."""
    lines = block.split("\n")
    widths = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    pad = min(widths) if widths else 0
    return "\n".join(line[pad:] if len(line) >= pad else line for line in lines)


def _step_run_body(step_name: str) -> str:
    """The ``run: |`` body of the named step, dedented.

    Bounded by the next ``- name:`` so a step's script can never bleed into
    the following one.
    """
    text = WORKFLOW.read_text()
    anchor = text.index(f"- name: {step_name}")
    nxt = text.find("\n      - name: ", anchor + 1)
    step = text[anchor:nxt if nxt != -1 else len(text)]
    body_start = step.index("run: |")
    body_start = step.index("\n", body_start) + 1
    return _dedent_block(step[body_start:])


def _run_shell(script: str, tmp_path: Path, env: dict) -> tuple[int, str]:
    """Run a shell fragment with a real ``$GITHUB_OUTPUT`` and return
    ``(returncode, stdout + stderr + the GITHUB_OUTPUT contents)``."""
    path = tmp_path / "fragment.sh"
    path.write_text(script)
    out = tmp_path / "gh_output"
    out.write_text("")
    proc = subprocess.run(
        ["bash", str(path)],
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(
            tmp_path / "summary"), **env},
    )
    return proc.returncode, proc.stdout + proc.stderr + out.read_text()


# --------------------------------------------------------------------------
# the gate's command-token parse
# --------------------------------------------------------------------------
#
# `/review` is the FIRST WHITESPACE-SEPARATED TOKEN OF THE FIRST LINE and
# nothing else. The two obvious alternatives are both wrong in a way that
# costs a real paid review: `startsWith('/review')` also fires on "/reviewing
# this tomorrow", and an equality test breaks on the trailing newline a
# web-UI comment carries. This is the only piece of genuinely conditional
# logic in the workflow, and it decides whether a credentialed run happens.


@pytest.fixture(scope="module")
def command_fragment() -> str:
    """The gate's command parse + `case` allowlist, made runnable."""
    text = WORKFLOW.read_text()
    start = text.index('cmd="$(printf')
    end = text.index("esac", start) + len("esac")
    body = _dedent_block(text[start:end])
    assert "awk '{print $1}'" in body, "extracted the wrong fragment — no token parse in it"
    assert "/describe|/review|/improve|/ask" in body, "extracted fragment has no allowlist"
    # `cmd` is only echoed on the ACCEPTED path: the reject arm of the `case`
    # exits 0 first, so the marker's presence IS the verdict.
    return 'set -euo pipefail\n' + body + '\necho "ACCEPTED=$cmd"\n'


def _accepts(fragment: str, tmp_path: Path, body: str) -> bool:
    rc, log = _run_shell(fragment, tmp_path, {"BODY": body})
    assert rc == 0, f"the fragment itself failed:\n{log}"
    accepted = "ACCEPTED=" in log
    if not accepted:
        assert "allowed=false" in log, f"rejected without emitting allowed=false:\n{log}"
    return accepted


@pytest.mark.parametrize(
    "body",
    [
        "/review",
        "/review\n",            # web-UI comments carry a trailing newline
        "/review\r\n",          # ...and a CRLF one from some clients
        "  /review  ",          # leading/trailing whitespace is not a command change
        "/review please look at the auth change",
        "/describe",
        "/improve",
        "/ask what does this regex do?",
        "/review\nsecond line is ignored",
    ],
)
def test_a_real_command_is_accepted(command_fragment, tmp_path, body):
    assert _accepts(command_fragment, tmp_path, body) is True


@pytest.mark.parametrize(
    "body",
    [
        "/reviewing this tomorrow",   # the startsWith() trap: must NOT review
        "/describes the problem",
        "/asking for help",
        "/improvement idea",
        "please /review this",        # not the FIRST token
        "LGTM",
        "",
        "\n/review",                  # not on the FIRST line
        "I /review nothing",
        "//review",
        "/Review",                    # case-sensitive on purpose
    ],
)
def test_anything_that_is_not_a_bare_command_token_is_rejected(command_fragment, tmp_path, body):
    assert _accepts(command_fragment, tmp_path, body) is False


def test_only_a_bare_command_token_is_accepted(command_fragment, tmp_path):
    """The two directions stated together, so the invariant is readable as
    one assertion rather than inferred from two parametrize lists."""
    assert _accepts(command_fragment, tmp_path, "/review") is True
    assert _accepts(command_fragment, tmp_path, "/reviewing later") is False


# --------------------------------------------------------------------------
# the empty-secret guard
# --------------------------------------------------------------------------
#
# An optional, unpassed `workflow_call` secret arrives as an EMPTY STRING,
# never an error. The original ai-review.yml had no guard at all, so an empty
# key reached the model call and came back as a 401 from the /v1 door — which
# reads as "the platform rejected us" rather than "this repo never had a
# credential", and sends you to the wrong system.


@pytest.fixture(scope="module")
def credential_guard() -> str:
    src = _step_run_body("Verify the inference credential is present")
    assert "AW_PRAGENT_V1_KEY" in src, "extracted the wrong step — no secret name in it"
    return src


def test_an_unset_credential_fails_loudly_and_names_the_repo(credential_guard, tmp_path):
    rc, log = _run_shell(
        credential_guard, tmp_path, {"AW_PRAGENT_V1_KEY": "", "REPO": "fredericowu/aw-workspace"}
    )
    assert rc != 0, "an empty credential must fail the job, not reach the model call"
    assert "::error::" in log, "the failure must be an annotation, not just a log line"
    assert "AW_PRAGENT_V1_KEY" in log, "the error must name the secret"
    assert "fredericowu/aw-workspace" in log, "the error must name the repo that is missing it"


def test_a_present_credential_passes_the_guard(credential_guard, tmp_path):
    rc, log = _run_shell(
        credential_guard, tmp_path, {"AW_PRAGENT_V1_KEY": "sk-whatever", "REPO": "o/r"}
    )
    assert rc == 0, f"a present credential must not fail the guard:\n{log}"


def test_the_guard_runs_before_the_pr_agent_action():
    """A guard after the action guards nothing."""
    text = WORKFLOW.read_text()
    guard = text.index("- name: Verify the inference credential is present")
    action = text.index("uses: The-PR-Agent/pr-agent@")
    assert guard < action, "the credential guard must precede the PR-Agent action"


# --------------------------------------------------------------------------
# the two permissions lists must not drift
# --------------------------------------------------------------------------
#
# For a reusable workflow the effective GITHUB_TOKEN is the INTERSECTION of
# the caller's `uses:`-job grant and this file's own top-level block, and once
# a `permissions:` key names any scope, every unnamed scope is forced to
# `none` rather than inherited (GitHub docs, `permissions`). So a scope has to
# appear on BOTH sides and the two lists have to be identical. The sibling
# workflow's v1.1.1 was cut specifically to fix a missing scope, added it to
# every caller and to its checklist, never touched its own block, and shipped
# no fix at all — just a better error message. Nothing compared the lists.
# This does, in both directions.


def _own_permissions() -> dict:
    """The workflow's own top-level `permissions:` block, as {scope: access}."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln == "permissions:")
    out = {}
    for ln in lines[start + 1:]:
        if ln.startswith("  #") or not ln.strip():
            continue
        if not ln.startswith("  ") or ln.startswith("   "):
            break
        scope, _, access = ln.strip().partition(":")
        out[scope.strip()] = access.strip()
    return out


CHECKLIST_ANCHOR = "In the caller's `uses:` job, REQUIRED"


def _checklist_permissions() -> dict:
    """The `permissions:` block the CALLER CHECKLIST tells callers to use,
    un-commented back into {scope: access}.

    Anchored on the checklist's own "REQUIRED" line rather than on the first
    commented `permissions:` in the file — the checklist opens with a
    complete caller example that also contains one, and parsing that would
    run straight on into its `secrets:` block.
    """
    lines = WORKFLOW.read_text().splitlines()
    anchor = next(i for i, ln in enumerate(lines) if CHECKLIST_ANCHOR in ln)
    start = next(i for i, ln in enumerate(lines[anchor:], anchor)
                 if ln.strip().startswith("#") and ln.strip().endswith("permissions:"))
    out = {}
    for ln in lines[start + 1:]:
        body = ln.lstrip("#").strip()
        if not body:
            break
        body = body.split("#", 1)[0].strip()      # drop a trailing comment
        if ":" not in body:
            break
        scope, _, access = body.partition(":")
        out[scope.strip()] = access.strip()
    return out


def test_the_reusable_requests_every_scope_the_caller_checklist_demands():
    own, checklist = _own_permissions(), _checklist_permissions()
    assert checklist, "could not parse the caller checklist's permissions block"
    assert own == checklist, (
        "the reusable workflow's own permissions and the caller checklist have "
        f"drifted — own={own} checklist={checklist}. An unlisted scope is "
        "`none`, not inherited, so a caller cannot make up the difference; and "
        "a scope only this file requests is dead weight the caller never grants."
    )


def test_the_requested_scopes_are_exactly_what_the_review_needs():
    """Stated explicitly, because the parity test above is satisfied by any
    two lists that happen to match — including two that are both wrong.

    `statuses: write` publishes `devteam/ai-review`; `pull-requests: write`
    and `issues: write` are PR-Agent's own comment/description writes (GitHub
    files PR comments under the issues API). No `checks` scope: unlike the
    sibling merge gate, nothing here reads the Checks API — the gate resolves
    a fork with `gh api repos/.../pulls`.
    """
    assert _own_permissions() == {
        "contents": "read",
        "pull-requests": "write",
        "issues": "write",
        "statuses": "write",
    }


# --------------------------------------------------------------------------
# concurrency belongs to the caller, and its exact group key is the fix
# --------------------------------------------------------------------------
#
# A reusable workflow's own `concurrency` keys on THIS file's identity, so it
# would serialize every caller's every PR against every other. What needs
# serializing is "reviews of caller-repo PR #N", which only the caller can
# express. The group key is not boilerplate: `cancel-in-progress` is
# evaluated at WORKFLOW level, before any job-level `if`, so the gate's
# `sender.type != 'Bot'` guard is correct but unreachable in time, and a
# CodeRabbit auto-comment run was cancelling the real `pull_request` review
# it can never itself perform (aw-app-template PR #5, fix 809a1a6). The
# `bot-noop` arm keys bot senders out one layer earlier, where the kill
# happens. The guard here and the key there change together.

BOT_NOOP_GROUP = (
    "group: ai-review-${{ github.event.pull_request.number || "
    "github.event.issue.number }}-${{ github.event.sender.type == 'Bot' && "
    "'bot-noop' || 'review' }}"
)


def test_the_reusable_declares_no_workflow_level_concurrency():
    for ln in WORKFLOW.read_text().splitlines():
        assert not ln.startswith("concurrency:"), (
            "workflow-level `concurrency` here keys on this shared file's "
            "identity, not on the caller's PR — it must stay in the caller"
        )


def test_the_caller_checklist_keeps_the_bot_noop_concurrency_group():
    """The caller owns the key, so this file is the only place that can keep
    the literal honest. A checklist that silently loses the `bot-noop` arm
    re-creates the cancellation bug in every repo copied from it.

    The group is stated TWICE on purpose — once in the header's explanation
    and once in the copy-pasteable checklist — so each half is asserted
    separately against its own section. A single whole-file substring check
    passes on either copy alone, which is the wrong guard: the copy a caller
    actually pastes is the checklist's.
    """
    text = WORKFLOW.read_text()
    split = text.index("── CALLER CHECKLIST ──")
    header, checklist = text[:split], text[split:]

    assert BOT_NOOP_GROUP in checklist, (
        "the CALLER CHECKLIST no longer carries the exact bot-noop concurrency "
        f"group — that is the copy consumers paste. Expected to find:\n  {BOT_NOOP_GROUP}"
    )
    assert BOT_NOOP_GROUP in header, (
        "the header's concurrency explanation no longer shows the group it "
        "explains, so the rationale and the literal can drift"
    )
    assert "cancel-in-progress: true" in checklist


def test_the_gate_still_guards_on_the_sender_not_being_a_bot():
    """The other half of the same invariant. Losing this re-opens the
    comment loop the concurrency key cannot stop (PR-Agent reacting to its
    own comments)."""
    text = WORKFLOW.read_text()
    gate = text[text.index("  gate:"):]
    gate = gate[: gate.index("  ai-review:")]
    assert "github.event.sender.type != 'Bot'" in gate


# --------------------------------------------------------------------------
# inference routing invariants
# --------------------------------------------------------------------------


def test_there_is_no_metered_fallback_model():
    """Security, not cost. PR-Agent ships `fallback_models = ["gpt-6-luna"]`,
    a paid third-party model, so a non-empty list sends the diff off the
    contracted inference path on ANY error on the AP-MT path — silently, and
    only under failure, which is when nobody is looking."""
    m = re.search(r'^\s*CONFIG__FALLBACK_MODELS:\s*(.+?)\s*$',
                  WORKFLOW.read_text(), re.MULTILINE)
    assert m, "CONFIG__FALLBACK_MODELS is not set at all — the shipped default applies"
    assert m.group(1) == '"[]"', (
        f"CONFIG__FALLBACK_MODELS must be exactly \"[]\", got {m.group(1)!r}"
    )


def test_the_pr_agent_action_is_sha_pinned():
    """A tag is mutable; a third-party action that reviews our diffs with a
    credential must be pinned to a commit."""
    m = re.search(r"uses:\s*The-PR-Agent/pr-agent@(\S+)", WORKFLOW.read_text())
    assert m, "the PR-Agent action reference is gone"
    ref = m.group(1)
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"PR-Agent must be pinned to a full 40-char commit SHA, got {ref!r}"
    )


def test_every_job_consumes_the_runs_on_input():
    """All three jobs take `runs-on` from the `runs_on` input, same contract
    as the sibling workflow. A job left on a hardcoded literal would silently
    ignore a private caller's override and keep burning the hosted
    account-wide concurrency cap this input exists to stop burning."""
    runs_on = re.findall(r"^\s+runs-on:\s*(.+?)\s*$", WORKFLOW.read_text(), re.MULTILINE)
    assert len(runs_on) == 3, f"expected one runs-on per job, found {runs_on}"
    assert set(runs_on) == {"${{ fromJSON(inputs.runs_on) }}"}, (
        f"every job must consume fromJSON(inputs.runs_on), got {runs_on}"
    )


def test_the_runs_on_input_defaults_to_a_github_hosted_runner():
    """The DEFAULT is what protects a public caller, so it is pinned here.
    This workflow has an open `pull_request` trigger, so on a public caller
    anyone can open a fork PR, and a fork PR on `[self-hosted, aw-baremetal]`
    is arbitrary code execution on the bare-metal host that runs the whole
    workspace. A caller must opt INTO self-hosted, never inherit it — and the
    value is JSON, so the quotes are part of it (a bare `ubuntu-latest` fails
    `fromJSON` at expression evaluation)."""
    m = re.search(
        r"^      runs_on:\n(?:.*\n)*?        default:\s*(.+?)\s*$",
        WORKFLOW.read_text(),
        re.MULTILINE,
    )
    assert m, "the `runs_on` input no longer declares a default"
    assert m.group(1) == "'\"ubuntu-latest\"'", (
        "runs_on must default to the JSON string '\"ubuntu-latest\"', got "
        f"{m.group(1)!r}"
    )


def test_there_is_no_checkout_step():
    """PR-Agent reads the diff through the GitHub API. A checkout would put
    untrusted fork code on the runner, which is the whole reason the previous
    test can be satisfied cheaply.

    Comment lines are skipped: the header explains at length why there is no
    checkout, and the explanation must not be what makes this pass.
    """
    for line in WORKFLOW.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "actions/checkout" not in stripped, f"checkout reintroduced: {line!r}"


def test_the_job_timeout_nests_the_inference_budget():
    """The job budget must strictly EXCEED two full inference timeouts, not
    merely equal one: a `/review` pass makes more than one model call, and a
    job killed mid-call leaves `The operation was canceled` as the only
    signal. The sibling workflow's v1.1.4 was cut for exactly that defect
    (AP-MT PR #6: 17m53s of silence then a cancel, on a review that had
    already passed with zero findings)."""
    text = WORKFLOW.read_text()

    m = re.search(r'^\s*CONFIG__AI_TIMEOUT:\s*"(\d+)"\s*$', text, re.MULTILINE)
    assert m, "could not find CONFIG__AI_TIMEOUT"
    ai_timeout_s = int(m.group(1))

    job = text[text.index("  ai-review:"):]
    m = re.search(r"^\s+timeout-minutes:\s*(\d+)\s*$", job, re.MULTILINE)
    assert m, "the ai-review job has no timeout-minutes"
    job_s = int(m.group(1)) * 60

    assert job_s > 2 * ai_timeout_s, (
        f"job timeout ({job_s // 60}m) must strictly exceed 2x the inference "
        f"timeout ({ai_timeout_s}s each), or a multi-call review is killed "
        f"mid-flight with no diagnostic"
    )


# --------------------------------------------------------------------------
# the advisory contract
# --------------------------------------------------------------------------


def test_the_status_context_is_the_advisory_one_the_merge_gate_knows():
    """`devteam/ai-review` is in `code-review-reusable.yml`'s ADVISORY set.
    Renaming it here would make a caller running both workflows wait on this
    review at its merge gate and then refuse to merge at the deadline."""
    text = WORKFLOW.read_text()
    contexts = set(re.findall(r"-f context=(\S+)", text))
    assert contexts == {"devteam/ai-review"}, f"unexpected status contexts: {contexts}"

    sibling = WORKFLOW.parent / "code-review-reusable.yml"
    assert "devteam/ai-review" in sibling.read_text(), (
        "the sibling merge gate no longer lists devteam/ai-review as advisory"
    )


def test_the_final_status_is_published_even_when_the_review_fails():
    """A `pending` status that never resolves is indistinguishable from a
    queue backlog, and blocks nothing while looking like it does."""
    text = WORKFLOW.read_text()
    step = text[text.index("- name: Publish AI review status"):]
    step = step[: step.index("run: |")]
    assert "if: always()" in step
