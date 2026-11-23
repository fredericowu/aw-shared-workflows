"""Regression tests for the executable logic embedded in
``.github/workflows/code-review-reusable.yml``.

**Why this file exists.** That workflow carries a real two-round state
machine — label-driven round detection, round-1 vs round-2 merge
eligibility, the `blocks_merge`-missing fail-closed rule, the draft-merge
guard — which is meaningfully more logic than workflow YAML usually holds,
and which CI could not otherwise exercise at all. It was flagged twice by
the Dev Team reviewer (once on the predecessor PR for this file, again on
PR #13) as shipping with no committed regression guard: a future edit could
flip `eligible = not blocking` to the wrong boolean and nothing would fail
until a real PR misbehaved. Fixing it the second time rather than filing the
same note a third time.

**How it works.** The step scripts are EXTRACTED from the workflow file by
text-scanning its heredoc markers and then executed directly, so these tests
assert against the bytes CI actually runs. They cannot drift from the
workflow the way a hand-maintained copy of the logic would — if someone
renames a step or changes a marker, extraction fails loudly rather than
silently testing nothing.

Deliberately NOT parsed with PyYAML: adding a parser dependency to run a
text scan would be the kind of incidental coupling that makes a test the
first thing someone deletes. pytest is the only requirement.

**This file MOVED here from tekflox/aw-app-template** (``tests/
test_code_review_workflow_logic.py``) when that repo's ~1100-line
code-review.yml became a thin caller of this repo's reusable workflow. The
logic it guards moved; the guard had to move with it. Leaving it behind was
not an option — it broke instantly (``ValueError: substring not found``, the
heredoc it scans was gone), and deleting it would have silently dropped the
only regression guard the round state machine has, which is the exact gap
the Dev Team reviewer flagged TWICE before this file was written.

It is run by this repo's own ``.github/workflows/test.yml``, which exists
because of this move: a reusable workflow that ~3 repos depend on had no CI
of its own, so nothing executed these assertions. A test nobody runs is not
a guard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import textwrap
import re
import pytest

WORKFLOW = (Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "code-review-reusable.yml")

# The `merge-attempt` job's one step. v1.3.0 moved the merge — and the
# `gh pr checks` reader with it — out of the `code-review` job, so several
# guards below that used to anchor on `- name: Wait for the repo's own CI to
# be green` (deleted along with its 20-minute poll) anchor here instead.
_MERGE_STEP = "- name: Attempt the merge"


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _dedent_block(block: str) -> str:
    """Strip the common leading indentation a YAML heredoc carries."""
    lines = block.split("\n")
    widths = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    pad = min(widths) if widths else 0
    return "\n".join(line[pad:] if len(line) >= pad else line for line in lines)


def _code_only(src: str) -> str:
    """``src`` with whole-line ``#`` comments stripped.

    Scan CODE through this. Assert on prose only where prose is the subject.

    The standing hazard: this workflow is deliberately comment-dense, and a
    comment explaining why something matters necessarily repeats the token a
    guard would scan for — so scanning raw text for a code token tests the
    documentation, not the code. It bit once already. The comment above
    ``checks()`` opens with "``timeout=`` is not optional", which by itself
    satisfied ``"timeout=" in call`` and left the bounded-call guard below
    passing with the real ``timeout=60`` kwarg deleted.

    Honest scope, because the convenient story is wrong: stripping comments
    is NOT what repaired that guard — ``_call_args`` is, by scoping the scan
    to the call's own argument list, where the offending comment never was.
    Regressing this function to ``return src`` leaves all 34 tests green.
    It is kept as the general discipline for scans that cannot be scoped
    that tightly, not as this defect's fix.
    """
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def _call_args(code: str, callee: str) -> str:
    """The literal argument list of the first ``callee(…)`` call in ``code``.

    Paren-matched rather than scanning to the next ``)``, so a nested call
    can never truncate the slice early and quietly shrink what a guard sees.
    """
    start = code.index(callee + "(") + len(callee) + 1
    depth, i = 1, start
    while depth:
        depth += {"(": 1, ")": -1}.get(code[i], 0)
        i += 1
    return code[start:i - 1]


def _extract_heredoc(marker_before: str, text: str | None = None) -> str:
    """The body of the first ``python3 … <<'PYEOF' … PYEOF`` heredoc that
    appears after ``marker_before`` in the workflow.

    ``text`` defaults to the real workflow; pass a string to read a modified
    copy, which is how the vacuity guard below feeds a mutated version in.
    """
    text = WORKFLOW.read_text() if text is None else text
    anchor = text.index(marker_before)
    start = text.index("PYEOF", anchor)
    body_start = text.index("\n", start) + 1
    body_end = text.index("PYEOF", body_start)
    return _dedent_block(text[body_start:body_end])


@pytest.fixture(scope="module")
def eligibility_script(tmp_path_factory) -> Path:
    """The `Post findings …` step's Python, which owns merge eligibility."""
    src = _extract_heredoc("- name: Post findings and prepare the status description")
    assert "eligible" in src, "extracted the wrong block — no eligibility logic in it"
    path = tmp_path_factory.mktemp("wf") / "eligibility.py"
    path.write_text(src)
    return path


@pytest.fixture(scope="module")
def fake_gh(tmp_path_factory) -> Path:
    """A `gh` on PATH that records its args and never touches the network.

    `GH_LABELS_FAIL=1` makes the label endpoint fail, which is how the
    fail-loud path below is driven. `GH_LOG`, when set, receives one line per
    invocation — that is how the eligibility-status assertions below check
    what the step actually posted rather than what it printed.
    """
    bin_dir = tmp_path_factory.mktemp("bin")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'if [ -n "${GH_LOG:-}" ]; then printf "%s\\n" "$*" >> "$GH_LOG"; fi\n'
        'if [[ "$*" == *"/labels"* ]]; then\n'
        '  if [ "${GH_LABELS_FAIL:-0}" = "1" ]; then echo "gh: rate limited" >&2; exit 1; fi\n'
        "  printf '%s\\n' $FAKE_LABELS; exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    gh.chmod(0o755)
    return bin_dir


# --------------------------------------------------------------------------
# merge eligibility
# --------------------------------------------------------------------------


def _run_eligibility(
    script: Path,
    tmp_path,
    fake_gh,
    *,
    findings,
    counts,
    round_: str,
    verdict=None,
    is_draft: str = "false",
    agent_slug: str = "code-reviewer-sonnet",
) -> dict:
    """Drive the real step script and return its GITHUB_OUTPUT as a dict.

    Writes `dispatch_response.json` under this call's own `tmp_path` and
    points `RUNNER_TEMP` at it, mirroring the production script's own fix:
    a shared `/tmp/dispatch_response.json` is real host state two jobs on
    this self-hosted runner can race on (confirmed live — the Release
    workflow's own test run and the standalone Test workflow, triggered by
    the same push, clobbered each other's file and produced exactly the
    'any round-1 finding must wake the coder' failure this test guards).
    tmp_path is already unique per call, so this isolates each invocation
    the same way RUNNER_TEMP isolates each real job.
    """
    if verdict is None:
        verdict = {"findings": findings, "counts": counts}
    (tmp_path / "dispatch_response.json").write_text(
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "Here is my review:\n```json\n"
                            + json.dumps(verdict)
                            + "\n```"
                        }
                    }
                ]
            }
        )
    )
    out = tmp_path / "gh_output"
    out.write_text("")
    env = {
        **os.environ,
        "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "REPO": "tekflox/aw-app-template",
        "PR_NUMBER": "42",
        "HEAD_SHA": "a" * 40,
        "ROUND": round_,
        "IS_DRAFT": is_draft,
        "GITHUB_OUTPUT": str(out),
        "GH_TOKEN": "fake",
        "RUNNER_TEMP": str(tmp_path),
        "RUN_URL": "https://example.invalid/run/1",
        "GH_LOG": str(tmp_path / "gh_calls.log"),
        "AGENT_SLUG": agent_slug,
    }
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"step script failed:\n{proc.stderr[-2000:]}"
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


def _gh_calls(tmp_path) -> list[str]:
    """Every `gh` invocation the last `_run_eligibility` in this tmp_path made."""
    log = tmp_path / "gh_calls.log"
    return log.read_text().splitlines() if log.exists() else []


MINOR = {"severity": "low", "title": "unused import", "file": "a.py", "line": 3, "body": "x"}
# severity=high but NOT blocking, and severity=medium but blocking: the pair
# that proves `blocks_merge` is the gate and `severity` is not.
HIGH_NON_BLOCKING = {
    "severity": "high",
    "title": "confusing name",
    "file": "a.py",
    "line": 9,
    "body": "x",
    "blocks_merge": False,
}
MEDIUM_BLOCKING = {
    "severity": "medium",
    "title": "SQL injection",
    "file": "b.py",
    "line": 20,
    "body": "x",
    "blocks_merge": True,
}
NO_BLOCKS_FIELD = {
    "severity": "low",
    "title": "judge omitted the field",
    "file": "c.py",
    "line": 1,
    "body": "x",
}
CROSS_CUTTING = {"severity": "info", "title": "architecture note", "body": "no file/line"}


@pytest.mark.parametrize("verdict", [
    {"output": '{"findings": [], "counts": {}}', "completed": 2, "total": 2},
    {"findings": []},
    {"findings": None, "counts": {}},
    {"findings": [], "counts": None},
])
def test_invalid_verdict_fails_before_posting(eligibility_script, tmp_path, fake_gh, verdict):
    with pytest.raises(AssertionError, match="invalid review verdict"):
        _run_eligibility(
            eligibility_script, tmp_path, fake_gh,
            findings=[], counts={}, round_="1", verdict=verdict,
        )
    assert _gh_calls(tmp_path) == []
    assert (tmp_path / "gh_output").read_text() == ""


def test_the_review_banner_names_the_dispatched_agent_slug(eligibility_script, tmp_path, fake_gh):
    """The posted comment's banner must name whichever agent_slug the caller
    passed, not a slug hardcoded at the default — a canary dispatching
    `code-reviewer-pipeline` must not post a comment claiming
    `code-reviewer-sonnet` did the review."""
    _run_eligibility(
        eligibility_script,
        tmp_path,
        fake_gh,
        findings=[CROSS_CUTTING],
        counts={"info": 1},
        round_="1",
        agent_slug="code-reviewer-pipeline",
    )
    log = (tmp_path / "gh_calls.log").read_text()
    assert "code-reviewer-pipeline" in log
    assert "code-reviewer-sonnet" not in log


def test_informational_round_never_merges(eligibility_script, tmp_path, fake_gh):
    """`/aw-improve` suggestion runs are not a review round at all — they
    must never merge, whatever they found."""
    got = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[], counts={}, round_="informational"
    )
    assert got["eligible"] == "false"


@pytest.mark.parametrize("draft", ["true", "True", "TRUE"])
def test_a_draft_pr_is_never_merge_eligible(eligibility_script, tmp_path, fake_gh, draft):
    """`opened` fires for a PR created as a draft, so without this a clean
    review un-drafted and merged somebody's work-in-progress."""
    for round_ in ("1", "2"):
        got = _run_eligibility(
            eligibility_script,
            tmp_path,
            fake_gh,
            findings=[],
            counts={},
            round_=round_,
            is_draft=draft,
        )
        assert got["eligible"] == "false", f"draft merged on round {round_}"


@pytest.mark.parametrize("not_draft", ["false", "False", ""])
def test_a_non_draft_pr_is_unaffected_by_the_draft_guard(
    eligibility_script, tmp_path, fake_gh, not_draft
):
    got = _run_eligibility(
        eligibility_script,
        tmp_path,
        fake_gh,
        findings=[],
        counts={},
        round_="1",
        is_draft=not_draft,
    )
    assert got["eligible"] == "true"


# --------------------------------------------------------------------------
# `devteam/merge-eligible` — the commit status that replaced the poll
# --------------------------------------------------------------------------
#
# v1.3.0's hand-off: the review job publishes this on the head SHA at the
# moment it decides eligible=true, and `merge-attempt` — possibly in a
# COMPLETELY DIFFERENT workflow run, triggered by the caller's CI finishing
# an hour later — reads it back. If this write goes missing the loop does not
# break loudly; it just silently never merges anything again.
#
# And the inverse matters as much: a status written on an INELIGIBLE head
# would hand the merge job a licence the review never granted. Round 1 with
# a blocking finding, a draft PR, an informational run — all of them must
# leave the SHA with no status at all.


_ELIGIBLE_STATUS = "context=devteam/merge-eligible"


def _status_posts(tmp_path) -> list[str]:
    return [c for c in _gh_calls(tmp_path) if _ELIGIBLE_STATUS in c]


def test_the_eligible_status_is_published_on_the_head_sha(
    eligibility_script, tmp_path, fake_gh
):
    """The whole hand-off. Without this write, `merge-attempt` reads no
    eligibility on any SHA and the loop stops merging — silently, because
    every individual job still goes green."""
    got = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[], counts={}, round_="1"
    )
    assert got["eligible"] == "true"
    posts = _status_posts(tmp_path)
    assert len(posts) == 1, f"expected exactly one status post, got {posts}"
    assert f"statuses/{'a' * 40}" in posts[0], (
        "the status must be written on the reviewed head SHA — a status on "
        "anything else is unreadable by the merge job"
    )
    assert "state=success" in posts[0]


def test_the_status_write_is_structurally_inside_the_eligible_branch():
    """Complements the two behavioural tests above by pinning the SHAPE: the
    post lives under `if eligible:` and targets `{sha}`. Scanned on code
    only — this file's comments necessarily repeat the context name, and a
    guard satisfied by its own documentation guards nothing (see
    `_code_only`)."""
    src = _code_only(_extract_heredoc(
        "- name: Post findings and prepare the status description"))
    guarded = src[src.index("if eligible:"):]
    assert "context=devteam/merge-eligible" in guarded
    assert "statuses/{sha}" in guarded, (
        "the status must be posted on the reviewed {sha}, inside `if eligible:`"
    )


# --------------------------------------------------------------------------
# round detection (the gate's label-driven state machine)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def round_script(tmp_path_factory) -> Path:
    """The gate's round-detection fragment, extracted from the `Decide` step
    and made runnable by seeding the three variables the surrounding script
    would have set by then."""
    text = WORKFLOW.read_text()
    start = text.index("# ---- round, from the PR's LIVE labels ----")
    end = text.index('echo "round=$round is_draft=')
    body = (
        text[start:end]
        .replace("${{ github.repository }}", "tekflox/aw-app-template")
        .replace("${{ github.event.action }}", "${EVENT_ACTION:-synchronize}")
        .replace("${{ github.event_name }}", "${EVENT_NAME:-pull_request}")
        .replace('"$GITHUB_OUTPUT"', "/dev/null")
    )
    assert "has_label" in body, "extracted the wrong fragment"
    path = tmp_path_factory.mktemp("wf") / "round.sh"
    path.write_text(
        'set -euo pipefail\npr_number=42\nmode="${MODE_IN}"\n'
        + body
        + '\necho "FINAL_ROUND=$round"\n'
    )
    return path


def _detect_round(
    script: Path, fake_gh, *, labels: str, mode: str = "review", fetch_fails: bool = False,
    gh_log: Path | None = None, action: str = "synchronize", event: str = "pull_request",
):
    env = {
        "EVENT_ACTION": action,
        "EVENT_NAME": event,
        **({"GH_LOG": str(gh_log)} if gh_log else {}),
        **os.environ,
        "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "MODE_IN": mode,
        "FAKE_LABELS": labels,
        "GH_LABELS_FAIL": "1" if fetch_fails else "0",
        "GH_TOKEN": "fake",
    }
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    emitted = [
        line.split("=", 1)[1]
        for line in proc.stdout.splitlines()
        if line.startswith("FINAL_ROUND=")
    ]
    return proc.returncode, (emitted[0] if emitted else None), proc.stdout + proc.stderr


@pytest.mark.parametrize("action", ["created", "opened", "synchronize"])
def test_review_n_alone_is_round_1_on_every_trigger(round_script, fake_gh, action):
    """/aw-improve and a timed-out dispatch stamp review-N without any round-1
    verdict, so it must never read as round memory, push or /aw-review."""
    rc, round_, _ = _detect_round(round_script, fake_gh, labels="devteam:review-1", action=action)
    assert rc == 0 and round_ == "1"


@pytest.mark.parametrize("labels", ["", "devteam:awaiting-fixes"])
def test_aw_improve_stays_outside_the_round_machine(round_script, fake_gh, labels):
    """A suggestion re-run must not consume round 1 or trigger a merge."""
    rc, round_, _ = _detect_round(round_script, fake_gh, labels=labels, mode="improve")
    assert rc == 0
    assert round_ == "informational"


def test_an_unreadable_label_set_fails_loud_instead_of_defaulting_to_round_1(round_script, fake_gh):
    """Empty output is a legitimate answer here (a PR with no labels IS round
    1), so "no labels" and "could not read the labels" must not look alike.
    Swallowing the error silently reset a mid-escalation PR to a fresh round 1
    and could loop, with nothing going red."""
    rc, round_, log = _detect_round(
        round_script, fake_gh, labels="devteam:needs-human", fetch_fails=True
    )
    assert rc != 0, "a failed label fetch must fail the gate"
    assert round_ is None, "no round may be emitted when the labels are unknown"
    assert "Refusing to guess the review round" in log


# --------------------------------------------------------------------------
# `Set round labels` — the round memory's one write to the PR
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def labels_script(tmp_path_factory) -> Path:
    """The `Set round labels` step's bash, extracted and made runnable by
    substituting the two `${{ }}` expressions it references. `fake_gh`
    answers every `*/labels*` call (POST to add, DELETE to remove) with
    exit 0, so assertions below read `GH_LOG` to see what was actually
    called rather than relying on real GitHub state."""
    text = WORKFLOW.read_text()
    start = text.index("          add_label() {")
    end = text.index("      # ---- tell the platform what this round decided ----")
    body = (
        text[start:end]
        .replace("${{ github.repository }}", "tekflox/aw-app-template")
        .replace("${{ needs.gate.outputs.pr_number }}", "42")
    )
    assert "devteam:reviewed" in body, "extracted the wrong fragment"
    path = tmp_path_factory.mktemp("wf") / "labels.sh"
    path.write_text("set -euo pipefail\n" + body)
    return path


def _run_labels(
    script: Path, fake_gh, tmp_path, *, round_: str, findings_count: int = 0, blocking_count: int = 0,
    live_labels: str = "", outputs: Path | None = None, fix_push: str = "",
):
    log = tmp_path / "gh_calls.log"
    env = {
        **os.environ,
        "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "ROUND": round_,
        "FINDINGS_COUNT": str(findings_count),
        "BLOCKING_COUNT": str(blocking_count),
        "GH_TOKEN": "fake",
        "GH_LOG": str(log),
        "FAKE_LABELS": live_labels,
        "FIX_PUSH": fix_push,
        "GITHUB_OUTPUT": str(outputs) if outputs else "/dev/null",
    }
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"label step failed:\n{proc.stderr[-2000:]}"
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.stdout, calls


def _failing_gh(tmp_path, fail_marker: str, live: str = ""):
    """A fake gh that fails exactly the calls whose argv contains fail_marker."""
    bin_dir = tmp_path / "failbin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        f'if [[ "$*" == *"{fail_marker}"* ]]; then exit 1; fi\n'
        'if [[ "$*" == *"-X "* ]]; then echo "$*" >> "$GH_LOG"; exit 0; fi\n'
        f"printf '%s\\n' {live or ''}\n"
    )
    gh.chmod(0o755)
    return bin_dir


# --------------------------------------------------------------------------
# `synchronize` carries the coder's fix push: the gate never refuses it on
# label state. After a completed review 1 it is cleared (bypass) without a
# second deep review; before one it is reviewed as review 1.


@pytest.fixture(scope="module")
def synchronize_script(tmp_path_factory) -> Path:
    """The gate's `Decide` step from the round machine through the
    synchronize guard, made runnable the same way `round_script` is."""
    text = WORKFLOW.read_text()
    start = text.index("# ---- round, from the PR's LIVE labels ----")
    end = text.index("          if ! is_trusted ")
    body = (
        text[start:end]
        .replace("${{ github.repository }}", "tekflox/aw-app-template")
        .replace("${{ github.event.action }}", "${EVENT_ACTION}")
        .replace('"$GITHUB_OUTPUT"', '"$OUT"')
    )
    assert "synchronize" in body, "extracted the wrong fragment — no synchronize guard"
    path = tmp_path_factory.mktemp("wf") / "synchronize.sh"
    path.write_text(
        'set -euo pipefail\npr_number=42\nmode="${MODE_IN}"\n'
        + body
        + '\necho "allowed=true" >> "$OUT"\n'
    )
    return path


def _decide_allowed(script: Path, fake_gh, tmp_path, *, labels: str, action: str,
                    mode: str = "review") -> dict:
    out = tmp_path / "gh_output"
    out.write_text("")
    env = {
        "GH_LOG": str(tmp_path / "gh_calls.log"),
        **os.environ,
        "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "MODE_IN": mode,
        "EVENT_ACTION": action,
        "FAKE_LABELS": labels,
        "GH_LABELS_FAIL": "0",
        "GH_TOKEN": "fake",
        "OUT": str(out),
    }
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    parsed = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )
    parsed["_log"] = proc.stdout + proc.stderr
    log = tmp_path / "gh_calls.log"
    parsed["_calls"] = log.read_text().splitlines() if log.exists() else []
    return parsed


def _drops_awaiting_fixes(got) -> bool:
    return any("-X DELETE" in c and "labels/devteam%3Aawaiting-fixes" in c for c in got["_calls"])


def test_synchronize_drops_awaiting_fixes_and_still_reviews(synchronize_script, fake_gh, tmp_path):
    got = _decide_allowed(
        synchronize_script, fake_gh, tmp_path, labels="devteam:awaiting-fixes", action="synchronize"
    )
    assert _drops_awaiting_fixes(got), got["_calls"]
    assert got["round"] == "1" and got["allowed"] == "true"


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review", "created"])
def test_a_non_synchronize_trigger_does_not_drop_awaiting_fixes(
    synchronize_script, fake_gh, tmp_path, action
):
    got = _decide_allowed(
        synchronize_script, fake_gh, tmp_path, labels="devteam:awaiting-fixes", action=action
    )
    assert not _drops_awaiting_fixes(got)


@pytest.mark.parametrize("labels", ["", "devteam:review-1", "devteam:needs-human"])
def test_synchronize_without_awaiting_fixes_deletes_nothing(
    synchronize_script, fake_gh, tmp_path, labels
):
    got = _decide_allowed(synchronize_script, fake_gh, tmp_path, labels=labels, action="synchronize")
    assert not _drops_awaiting_fixes(got)


def test_a_failed_drop_is_a_warning_not_a_failure(synchronize_script, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == *"-X DELETE"* ]]; then exit 1; fi\n'
        'if [[ "$*" == *"/labels"* ]]; then printf "%s\\n" devteam:awaiting-fixes; fi\n'
    )
    gh.chmod(0o755)
    got = _decide_allowed(
        synchronize_script, bin_dir, tmp_path, labels="", action="synchronize"
    )
    assert got["allowed"] == "true"
    assert "::warning::could not drop devteam:awaiting-fixes" in got["_log"]


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review"])
@pytest.mark.parametrize("labels", ["", "devteam:awaiting-fixes"])
def test_the_synchronize_guard_does_not_touch_the_other_pull_request_actions(
    synchronize_script, fake_gh, tmp_path, action, labels
):
    """`opened` on a fresh PR is round 1 and must still be reviewed — a guard
    that keyed on "round != 2" without checking the action would have
    switched the entire loop off."""
    got = _decide_allowed(
        synchronize_script, fake_gh, tmp_path, labels=labels, action=action
    )
    assert got["allowed"] == "true"


def test_aw_review_still_overrides_from_any_round(synchronize_script, fake_gh, tmp_path):
    """The manual path is the recovery path for everything the event path
    cannot do — a dropped workflow_run delivery, a re-review of round 1, a
    retry after a mechanical merge failure. An `issue_comment` run carries no
    `synchronize` action, so the guard must not see it at all."""
    for labels in ("", "devteam:awaiting-fixes", "devteam:needs-human"):
        got = _decide_allowed(
            synchronize_script, fake_gh, tmp_path, labels=labels, action="created"
        )
        assert got["allowed"] == "true", f"/aw-review blocked on labels {labels!r}"


def test_the_callers_are_told_to_add_synchronize_and_workflow_run():
    """The reusable cannot declare its own triggers — a `workflow_call`
    workflow has none. The caller contract is therefore prose, and prose that
    nobody updated is how a caller ends up on a tag whose round 2 it never
    enables. The checklist is the only place that contract exists."""
    text = WORKFLOW.read_text()
    checklist = text[text.index("# ── CALLER CHECKLIST"):]
    assert "synchronize" in checklist
    assert "workflow_run:" in checklist
    assert "github.event.workflow_run.head_branch" in checklist, (
        "the concurrency-group arm for workflow_run events must be in the "
        "checklist: without it every such run in the repo shares one group"
    )


# --------------------------------------------------------------------------
# invariants that are cheap to assert and expensive to rediscover
# --------------------------------------------------------------------------


def test_the_merge_step_never_un_drafts_a_pr():
    """`gh pr ready` here would convert a deliberately-draft PR to ready and
    merge it. The comment explaining why it is gone may mention it; a real
    invocation may not."""
    for line in WORKFLOW.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "gh pr ready" not in stripped, f"gh pr ready reintroduced: {line!r}"


def test_the_merge_is_not_authenticated_with_github_token():
    """A GITHUB_TOKEN-authored merge push does not trigger downstream
    workflows, so release.yml would silently stop firing — green CI, no more
    releases.

    The merge moved out of the `code-review` job into `merge-attempt` in
    v1.3.0, so this scans the merge script rather than a step whose name no
    longer exists. `secrets.GITHUB_TOKEN` IS legitimately present in that
    job's env (every READ uses it), so the assertion is on the merge call's
    own token, not on the job as a whole.
    """
    src = _code_only(_extract_heredoc(_MERGE_STEP))
    merge_args = src.index('"gh", "pr", "merge"')
    merge_call = _call_args(
        src[src.rindex("subprocess.run(", 0, merge_args) :], "subprocess.run"
    )
    assert "merge_token" in merge_call, (
        "the merge call must pass AW_DEVTEAM_MERGE_TOKEN as GH_TOKEN"
    )
    assert 'MERGE_TOKEN: ${{ secrets.AW_DEVTEAM_MERGE_TOKEN }}' in WORKFLOW.read_text()
    assert "GITHUB_TOKEN" not in merge_call


def test_the_merge_still_requires_the_repos_own_ci_to_be_green():
    """`--auto` only waits for checks branch protection marks required, and
    master has none — so the merge must gate on CI itself.

    v1.3.0 deleted the 20-minute poll, NOT the gate: the check read is now a
    single bounded read whose retry is the next CI-completion event. What
    must not come back is a path from "review said eligible" to `gh pr
    merge` that never looks at the checks.
    """
    src = _code_only(_extract_heredoc(_MERGE_STEP))
    assert "def checks():" in src
    # the merge call must come AFTER the required-check comparison
    assert src.index("REQUIRED - passed") < src.index('"gh", "pr", "merge"')
    assert "ci_check_name" in WORKFLOW.read_text()


def test_the_twenty_minute_poll_is_gone():
    """The whole point of v1.3.0. A re-introduced wait loop would restore the
    bug class this release exists to kill — and would do it silently, since a
    poll that usually succeeds looks fine until the day CI is slow."""
    src = _code_only(_extract_heredoc(_MERGE_STEP))
    assert "while True" not in src, "the merge must not poll"
    assert "deadline" not in src, "the merge must not carry its own wait deadline"
    # the retry budget that remains is for FAILED READS only, and is small
    assert "for attempt in (1, 2, 3):" in src


# --- the checks() reader ------------------------------------------------------
#
# AP-MT PR #7 burned four consecutive /aw-review rounds on this one function.
# The caller's token lacked `checks: read`, so on that PRIVATE repo `gh pr
# checks` returned nothing at all — and `json.loads(out.stdout or "[]")` made
# that indistinguishable from "this PR legitimately has zero checks", so the
# gate failed instantly with `Saw: []` instead of retrying.
#
# The function moved from the deleted `ci_gate` step into `merge-attempt` in
# v1.3.0. These assertions came with it unchanged: the reader's three-way
# answer (rows / [] / None) is what makes a transient read failure
# distinguishable from a PR that genuinely has no checks, and that
# distinction is now what the 3-attempt retry hangs off.


def _checks_reader(stdout: str, stderr: str):
    """Run the real `checks()` body from the merge step against a faked
    `subprocess.run` result, and return whatever it decided."""
    src = _extract_heredoc(_MERGE_STEP)
    assert "def checks():" in src, "extracted the wrong block — no checks() in it"

    body = src[src.index("def checks():"):]
    # up to the next top-level statement after the function
    body = body[: re.search(r"\n(?=[A-Za-z#])", body).start()]

    ns = {"json": json, "os": os, "subprocess": None, "pr_number": "1", "REPO": "o/r"}

    class _Out:
        pass

    out = _Out()
    out.stdout, out.stderr = stdout, stderr

    class _FakeSubprocess:
        @staticmethod
        def run(*a, **kw):
            return out

    ns["subprocess"] = _FakeSubprocess
    exec(textwrap.dedent(body), ns)
    return ns["checks"]()


def test_a_real_read_is_parsed():
    assert _checks_reader('[{"name": "pytest", "bucket": "pass"}]', "") == [
        {"name": "pytest", "bucket": "pass"}
    ]


def test_an_empty_read_with_stderr_is_a_failed_read_not_zero_checks():
    """The `checks: read` case. Returning [] here fails the gate instantly;
    returning None makes the caller retry until its 20-minute deadline."""
    assert _checks_reader("", "gh: Resource not accessible by integration") is None


def test_an_empty_read_with_no_stderr_is_genuinely_zero_checks():
    assert _checks_reader("", "") == []


def test_unparseable_output_is_a_failed_read():
    assert _checks_reader("not json at all", "") is None


# --- the two permissions lists must not drift ---------------------------------
#
# v1.1.1 was cut specifically to fix a missing `checks: read`, added it to all
# three callers and to the CALLER CHECKLIST, and never touched this file's own
# top-level `permissions:` block. That is not a partial fix, it is no fix: for
# a reusable workflow the effective GITHUB_TOKEN is the INTERSECTION of the
# caller's grant and the called workflow's own block, and once a `permissions:`
# key names any scope, every unnamed scope is forced to `none` rather than
# inherited (GitHub docs, `permissions`). So the omission here capped every
# caller to `checks: none` no matter what they granted, and the bug the release
# was named for stayed fully live — with a better error message.
#
# Caught in review on aw-app-template#17, not by CI, because nothing compared
# the two lists. Now something does.


def _own_permissions() -> dict:
    """The workflow's own top-level `permissions:` block, as {scope: access}.

    Text-scanned, not YAML-parsed: pytest is this suite's only dependency and
    the file's docstring keeps it that way deliberately.
    """
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


def _checklist_permissions() -> dict:
    """The `permissions:` block the CALLER CHECKLIST comment tells callers to
    use, un-commented back into {scope: access}."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines)
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
    """If the checklist tells callers to grant a scope, this file must request
    it too — otherwise the intersection silently drops it to `none` and the
    caller's grant buys nothing."""
    own, checklist = _own_permissions(), _checklist_permissions()
    assert checklist, "could not parse the caller checklist's permissions block"
    assert own == checklist, (
        "the reusable workflow's own permissions and the caller checklist have "
        f"drifted — own={own} checklist={checklist}. An unlisted scope is "
        "`none`, not inherited, so a caller cannot make up the difference."
    )


def test_checks_read_is_requested_by_the_reusable_itself():
    """The specific scope v1.1.1 missed. `gh pr checks` in the ci_gate step
    cannot see check-runs on a PRIVATE caller without it."""
    assert _own_permissions().get("checks") == "read"


# --- the check read must be able to fail LOUDLY, not hang silently -----------
#
# AP-MT PR #6's round 2 passed the review with zero findings and then died with
# nothing but `##[error]The operation was canceled`. `checks()` called
# `subprocess.run` with no `timeout=`, so a hung `gh` blocked the step
# silently — 17m53s with not one line of output, because the code only logs
# once it HAS a result.
#
# That defect was never reachable by running the workflow on a healthy repo,
# which is why it survived three copies and two releases. It is guarded here,
# with a mutation test below proving the guard can actually fail.
#
# Its sibling — the job's `timeout-minutes` equalling the gate's own 20-minute
# deadline, so the job died before the gate could print its diagnostic — no
# longer has a defect to guard: v1.3.0 deleted the deadline along with the
# poll, so there are no longer two budgets that could overlap.
# `test_the_twenty_minute_poll_is_gone` above is what replaced that assertion,
# and it guards the stronger property.


def _checks_call_timeout(text: str) -> str:
    """The ``timeout=`` seconds on the ``gh pr checks`` call, read from
    ``text``; ``""`` if that call is unbounded.

    A function of the text, not of the file, so the vacuity guard below can
    hand it a deliberately broken copy and assert this reports the break.
    Scoped to ``subprocess.run``'s own argument list: a ``timeout=`` anywhere
    else in the function — or in a comment — would not bound THIS call.
    """
    body = _code_only(_extract_heredoc(_MERGE_STEP, text))
    body = body[body.index("def checks():"):]
    m = re.search(r"\btimeout=(\d+)", _call_args(body, "subprocess.run"))
    return m.group(1) if m else ""


def test_the_checks_call_is_bounded_by_a_timeout():
    """An unbounded `gh` call makes a hang indistinguishable from a slow CI,
    and emits nothing while it happens."""
    assert _checks_call_timeout(WORKFLOW.read_text()), (
        "gh pr checks must not be called unbounded"
    )
    assert "TimeoutExpired" in _code_only(_extract_heredoc(_MERGE_STEP)), (
        "a timeout that raises instead of returning None would crash the step "
        "rather than retry it"
    )


def test_the_api_reads_are_bounded_too():
    """`api()` is new in v1.3.0 and makes four reads the old code did not —
    including the PR resolution that the whole workflow_run entry point hangs
    off. An unbounded hang in any of them is the same invisible stall."""
    body = _code_only(_extract_heredoc(_MERGE_STEP))
    body = body[body.index("def api("):]
    assert re.search(r"\btimeout=\d+", _call_args(body, "subprocess.run")), (
        "gh api must not be called unbounded"
    )


def test_the_timeout_guard_is_not_vacuous():
    """Mutation guard: delete the real kwarg and the guard above MUST fail.

    Its first version scanned a 2000-char slice for the bare string
    ``timeout=``, which the explanatory comment directly above the call
    already contained — so it passed with the kwarg deleted and reported
    coverage it did not have. A guard that cannot fail is worse than no
    guard, because it stops anyone looking. This test is what makes the one
    above mean something, and it is the only assertion here that would have
    caught the original defect.

    Mutation testing found this; neither review nor a green suite did. The
    other four guards in this file were mutated the same way and all failed
    correctly, so this is the single instance rather than a pattern.
    """
    real = WORKFLOW.read_text()
    # Mutate only inside `checks()`. `api()` in the same script is bounded
    # with the identical kwarg, and a plain `.replace(..., 1)` hit THAT one
    # instead — leaving the guard reading an untouched `checks()` and passing.
    # Which is this test's own failure mode, caught by this test failing.
    anchor = real.index("def checks():")
    broken = real[:anchor] + real[anchor:].replace(
        "text=True, timeout=60)", "text=True)", 1
    )
    assert broken != real, (
        "mutation anchor is stale — re-point it at the real call, or this "
        "guard silently stops mutating anything"
    )
    assert not _checks_call_timeout(broken), (
        "the bounded-call guard still passes on a copy with the kwarg "
        "removed, so it is not guarding the call"
    )


# --------------------------------------------------------------------------
# provenance-gate — the AW-Run-Id trailer check and its live status
# --------------------------------------------------------------------------

_PROVENANCE_STEP = "- name: Check for a stamped AW-Run-Id trailer and record it on the PR"


@pytest.fixture(scope="module")
def provenance_script(tmp_path_factory) -> Path:
    src = _extract_heredoc(_PROVENANCE_STEP)
    assert "AW-Run-Id" in src, "extracted the wrong block — no trailer check in it"
    path = tmp_path_factory.mktemp("wf") / "provenance.py"
    path.write_text(src)
    return path


@pytest.fixture(scope="module")
def provenance_gh(tmp_path_factory) -> Path:
    """A `gh` that answers labels/commits reads from `$FAKE_GH_DIR` fixtures
    and records every call (including the `statuses` POST) to `calls.log`,
    the same shape as `merge_gh` above."""
    bin_dir = tmp_path_factory.mktemp("provbin")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        'd = os.environ["FAKE_GH_DIR"]\n'
        'with open(os.path.join(d, "calls.log"), "a") as f:\n'
        '    f.write(" ".join(args) + "\\n")\n'
        'path = next((a for a in args[1:] if a.startswith("repos/")), "")\n'
        "def emit(name):\n"
        "    p = os.path.join(d, name)\n"
        "    if not os.path.exists(p):\n"
        "        print('gh: no fixture ' + name, file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "    sys.stdout.write(open(p).read())\n"
        "    sys.exit(0)\n"
        "if args[:1] == ['api']:\n"
        "    if path.endswith('/labels'):\n"
        "        emit('labels.json')\n"
        "    if path.endswith('/commits'):\n"
        "        emit('commits.json')\n"
        "    if '/statuses/' in path:\n"
        "        sys.exit(0)\n"
        "print('gh: unexpected call ' + repr(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    gh.chmod(0o755)
    return bin_dir


REAL_RUN_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _commit(message, sha="deadbee"):
    return {"sha": sha, "commit": {"message": message}}


def _run_provenance(
    provenance_script, provenance_gh, tmp_path, *, labels=None, commits=None,
    label_pages=None,
):
    d = tmp_path / "ghfix"
    d.mkdir(exist_ok=True)
    for leftover in ("calls.log",):
        if (d / leftover).exists():
            (d / leftover).unlink()
    if label_pages is not None:
        # what `gh api --paginate` prints: one array per page, back to back
        (d / "labels.json").write_text("".join(json.dumps(p) for p in label_pages))
    else:
        (d / "labels.json").write_text(json.dumps(labels if labels is not None else []))
    (d / "commits.json").write_text(json.dumps(commits if commits is not None else []))
    out = tmp_path / "gh_output"
    out.write_text("")
    env = {
        **os.environ,
        "PATH": f"{provenance_gh}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DIR": str(d),
        "GH_TOKEN": "fake",
        "REPO": "tekflox/aw-app-template",
        "PR_NUMBER": "42",
        "HEAD_SHA": "b" * 40,
        "RUN_URL": "https://example.invalid/run/1",
        "GITHUB_OUTPUT": str(out),
    }
    proc = subprocess.run(
        [sys.executable, str(provenance_script)], capture_output=True, text=True, env=env
    )
    outputs = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )
    calls = (d / "calls.log").read_text() if (d / "calls.log").exists() else ""
    return proc, outputs, calls


def test_a_well_formed_trailer_clears_the_gate(
    provenance_script, provenance_gh, tmp_path
):
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        commits=[_commit(f"fix: thing\n\nAW-Run-Id: {REAL_RUN_ID}\n"
                          f"AW-Session-Id: sess-1\nAW-Card: card-1\n")],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs["run_id"] == REAL_RUN_ID
    assert outputs["session_id"] == "sess-1"
    assert outputs["card_id"] == "card-1"
    assert "devteam/provenance" in calls and "state=success" in calls


def test_no_trailer_on_any_commit_warns_without_blocking(
    provenance_script, provenance_gh, tmp_path
):
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        commits=[_commit("just a normal commit, no trailer")],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::warning::no commit on PR" in proc.stdout
    assert "run_id" not in outputs
    assert "devteam/provenance" in calls and "state=success" in calls


def test_a_malformed_run_id_does_not_count(provenance_script, provenance_gh, tmp_path):
    """Not 32 hex characters — a human-typed look-alike, or a truncated
    id — must not pass as a real stamp."""
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        commits=[_commit("AW-Run-Id: not-a-real-run-id\n")],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::warning::no commit on PR" in proc.stdout
    assert "run_id" not in outputs


def test_the_provenance_exempt_label_skips_the_gate_without_reading_commits(
    provenance_script, provenance_gh, tmp_path
):
    """The human escape hatch — checked BEFORE any commit is read, so it
    works even on a PR whose commits the gate would otherwise reject."""
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        labels=[{"name": "provenance-exempt"}],
        commits=[],  # irrelevant — the commits endpoint must never be called
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "devteam/provenance" in calls and "state=success" in calls
    assert "repos/tekflox/aw-app-template/pulls/42/commits" not in calls


def test_the_provenance_exempt_label_on_page_two_is_honoured(
    provenance_script, provenance_gh, tmp_path
):
    """Labels past the first 30 live on page 2; the lookup must paginate."""
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        label_pages=[
            [{"name": f"l{i}"} for i in range(30)],
            [{"name": "provenance-exempt"}],
        ],
        commits=[_commit(f"x\n\nAW-Run-Id: {REAL_RUN_ID}")],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--paginate" in calls
    assert "state=success" in calls
    assert "run_id" not in outputs
    assert "repos/tekflox/aw-app-template/pulls/42/commits" not in calls


def test_zero_commits_does_not_fail_the_gate(
    provenance_script, provenance_gh, tmp_path
):
    """A draft/empty PR has nothing to stamp yet — this must exit clean, not
    red, and must not publish `devteam/provenance` either (nothing to merge
    from this state, the draft/empty guard belongs to merge-attempt)."""
    proc, outputs, calls = _run_provenance(
        provenance_script, provenance_gh, tmp_path, commits=[]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "devteam/provenance" not in calls


def test_the_newest_commit_with_a_trailer_wins(
    provenance_script, provenance_gh, tmp_path
):
    """Two pushes, two runs: the later one's ids are what the PR should
    carry, even though the earlier commit also has a (now-stale) trailer."""
    older_id = "1" * 32
    newer_id = "2" * 32
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        commits=[
            _commit(f"AW-Run-Id: {older_id}\n", sha="aaaaaaa"),
            _commit("no trailer here", sha="bbbbbbb"),
            _commit(f"AW-Run-Id: {newer_id}\n", sha="ccccccc"),
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs["run_id"] == newer_id


def test_the_agent_slug_trailer_is_extracted(
    provenance_script, provenance_gh, tmp_path
):
    """The fourth trailer, self-stamped by the same hook as the other
    three (agents-platform-runners' prepare-commit-msg) — extracted the
    same way, not looked up via any API."""
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        commits=[_commit(f"fix: thing\n\nAW-Run-Id: {REAL_RUN_ID}\n"
                          f"AW-Agent-Slug: coder-sonnet\n")],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs["agent_slug"] == "coder-sonnet"


def test_a_missing_agent_slug_trailer_is_simply_absent(
    provenance_script, provenance_gh, tmp_path
):
    """No API fallback exists for this field — an older runner/AP-MT
    version, or a not-yet-recycled warm container, just means the field is
    empty, same as a run with no card."""
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        commits=[_commit(f"AW-Run-Id: {REAL_RUN_ID}\n")],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs["agent_slug"] == ""


# --------------------------------------------------------------------------
# provenance-gate's footer step — idempotent PR body only
# --------------------------------------------------------------------------

_FOOTER_STEP = "- name: Record the provenance footer on the PR body"


@pytest.fixture(scope="module")
def footer_script(tmp_path_factory) -> Path:
    src = _extract_heredoc(_FOOTER_STEP)
    assert "devteam:provenance:start" in src, "extracted the wrong block"
    path = tmp_path_factory.mktemp("wf") / "footer.py"
    path.write_text(src)
    return path


@pytest.fixture(scope="module")
def footer_gh(tmp_path_factory) -> Path:
    """A stateful fake `gh` that plays a PR body and rejects comment calls."""
    bin_dir = tmp_path_factory.mktemp("footerbin")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        'd = os.environ["FAKE_GH_DIR"]\n'
        'sf = os.path.join(d, "state.json")\n'
        'state = json.load(open(sf)) if os.path.exists(sf) else {"pr_body": "", "comments": []}\n'
        "def save():\n"
        "    json.dump(state, open(sf, 'w'))\n"
        "def body_arg():\n"
        "    for a in args:\n"
        "        if a.startswith('body='):\n"
        "            return a[len('body='):]\n"
        "    return None\n"
        "rest = args[1:]\n"
        "is_patch = rest[:2] == ['-X', 'PATCH']\n"
        "path = rest[2] if is_patch else rest[0]\n"
        "if path.endswith('/pulls/42') and not is_patch:\n"
        "    print(json.dumps({'body': state['pr_body']})); sys.exit(0)\n"
        "if path.endswith('/pulls/42') and is_patch:\n"
        "    state['pr_body'] = body_arg(); save(); sys.exit(0)\n"
        "print('gh: unexpected call ' + repr(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    gh.chmod(0o755)
    return bin_dir


def _run_footer(
    footer_script, footer_gh, tmp_path, *, run_id, session_id="", card_id="",
    agent_slug="", initial_comments=None,
):
    d = tmp_path / "ghfix"
    d.mkdir(exist_ok=True)
    state_path = d / "state.json"
    if not state_path.exists() and initial_comments is not None:
        state_path.write_text(json.dumps({"pr_body": "", "comments": initial_comments}))
    env = {
        **os.environ,
        "PATH": f"{footer_gh}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DIR": str(d),
        "GH_TOKEN": "fake",
        "REPO": "tekflox/aw-app-template",
        "PR_NUMBER": "42",
        "RUN_ID": run_id,
        "SESSION_ID": session_id,
        "CARD_ID": card_id,
        "AGENT_SLUG": agent_slug,
    }
    proc = subprocess.run(
        [sys.executable, str(footer_script)], capture_output=True, text=True, env=env
    )
    state = json.loads((d / "state.json").read_text()) if (d / "state.json").exists() else None
    return proc, state


def test_the_footer_is_appended_once(footer_script, footer_gh, tmp_path):
    proc, state = _run_footer(footer_script, footer_gh, tmp_path, run_id="r" * 32)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["pr_body"].count("<!-- devteam:provenance:start -->") == 1
    assert "r" * 32 in state["pr_body"]
    assert state["comments"] == []


def test_a_second_round_replaces_the_footer_in_place(footer_script, footer_gh, tmp_path):
    """Round 2 (or a later `/aw-review`) replaces the body footer in place."""
    _run_footer(footer_script, footer_gh, tmp_path, run_id="r" * 32)
    proc, state = _run_footer(footer_script, footer_gh, tmp_path, run_id="s" * 32)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["pr_body"].count("<!-- devteam:provenance:start -->") == 1
    assert "r" * 32 not in state["pr_body"]
    assert "s" * 32 in state["pr_body"]
    assert state["comments"] == []


def test_the_footer_does_not_create_or_modify_comments(
    footer_script, footer_gh, tmp_path
):
    historical = [{"id": 7, "body": "existing substantive review comment"}]
    proc, state = _run_footer(
        footer_script,
        footer_gh,
        tmp_path,
        run_id="r" * 32,
        initial_comments=historical,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["comments"] == historical


def test_the_footer_includes_the_agent_line_when_resolved(
    footer_script, footer_gh, tmp_path
):
    proc, state = _run_footer(
        footer_script, footer_gh, tmp_path, run_id="r" * 32, agent_slug="coder-sonnet"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "**Agent:** `coder-sonnet`" in state["pr_body"]


def test_the_footer_omits_the_agent_line_when_absent(footer_script, footer_gh, tmp_path):
    """Matches the existing Session/Card treatment — absence is silent, not
    a placeholder line."""
    proc, state = _run_footer(footer_script, footer_gh, tmp_path, run_id="r" * 32)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "**Agent:**" not in state["pr_body"]


# --------------------------------------------------------------------------
# the label-apply step — trailer-derived PR labels, never allowed to fail
# --------------------------------------------------------------------------

_LABELS_STEP = "- name: Apply provenance labels"


@pytest.fixture(scope="module")
def provenance_labels_script(tmp_path_factory) -> Path:
    src = _extract_heredoc(_LABELS_STEP)
    assert "aw-run" in src and "aw-card" in src and "aw-agent" in src, (
        "extracted the wrong block — no label mapping in it"
    )
    path = tmp_path_factory.mktemp("wf") / "labels.py"
    path.write_text(src)
    return path


@pytest.fixture(scope="module")
def provenance_label_gh(tmp_path_factory) -> Path:
    """A stateful fake `gh` that plays both the repo's label list and a
    PR's applied labels, persisted in `$FAKE_GH_DIR/state.json` so the same
    tmp_path can drive the script twice (replace-by-prefix, already-applied).
    `FAKE_GH_ALWAYS_FAIL=1` makes every call fail, for the never-fails test.
    """
    bin_dir = tmp_path_factory.mktemp("labelbin")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, urllib.parse\n"
        "args = sys.argv[1:]\n"
        'd = os.environ["FAKE_GH_DIR"]\n'
        'with open(os.path.join(d, "calls.log"), "a") as f:\n'
        '    f.write(" ".join(args) + "\\n")\n'
        'if os.environ.get("FAKE_GH_ALWAYS_FAIL"):\n'
        '    print("gh: simulated hard failure", file=sys.stderr)\n'
        '    sys.exit(1)\n'
        'sf = os.path.join(d, "state.json")\n'
        'state = json.load(open(sf)) if os.path.exists(sf) else '
        '{"pr_labels": [], "repo_labels": []}\n'
        "def save():\n"
        "    json.dump(state, open(sf, 'w'))\n"
        "rest = args[1:]\n"
        "if rest[:2] == ['-X', 'DELETE']:\n"
        "    name = urllib.parse.unquote(rest[2].rsplit('/', 1)[-1])\n"
        "    state['pr_labels'] = [n for n in state['pr_labels'] if n != name]\n"
        "    save(); sys.exit(0)\n"
        "if rest[:2] == ['-X', 'POST']:\n"
        "    name = next(a.split('=', 1)[1] for a in rest if a.startswith('name='))\n"
        "    if name in state['repo_labels']:\n"
        "        print('already_exists (HTTP 422)', file=sys.stderr); sys.exit(1)\n"
        "    state['repo_labels'].append(name); save(); sys.exit(0)\n"
        "if '--paginate' in rest:\n"
        "    print(json.dumps([{'name': n} for n in state['pr_labels']])); sys.exit(0)\n"
        "if os.environ.get('FAKE_GH_FAIL_ADD'):\n"
        "    print('HTTP 503', file=sys.stderr); sys.exit(1)\n"
        "name = next(a.split('=', 1)[1] for a in rest if a.startswith('labels[]='))\n"
        "if name not in state['pr_labels']:\n"
        "    state['pr_labels'].append(name)\n"
        "save(); sys.exit(0)\n"
    )
    gh.chmod(0o755)
    return bin_dir


def _run_provenance_labels(
    provenance_labels_script, provenance_label_gh, tmp_path, *, run_id="", card_id="", agent_slug="",
    existing_pr_labels=None, existing_repo_labels=None, always_fail=False,
    fail_add=False,
):
    d = tmp_path / "ghfix"
    d.mkdir(exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "pr_labels": existing_pr_labels or [],
        "repo_labels": existing_repo_labels or [],
    }))
    env = {
        **os.environ,
        "PATH": f"{provenance_label_gh}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DIR": str(d),
        "GH_TOKEN": "fake",
        "REPO": "tekflox/aw-app-template",
        "PR_NUMBER": "42",
        "RUN_ID": run_id,
        "CARD_ID": card_id,
        "AGENT_SLUG": agent_slug,
    }
    if always_fail:
        env["FAKE_GH_ALWAYS_FAIL"] = "1"
    if fail_add:
        env["FAKE_GH_FAIL_ADD"] = "1"
    proc = subprocess.run(
        [sys.executable, str(provenance_labels_script)], capture_output=True, text=True, env=env
    )
    state = json.loads((d / "state.json").read_text())
    calls = (d / "calls.log").read_text() if (d / "calls.log").exists() else ""
    return proc, state, calls


def test_all_three_labels_are_applied_when_everything_resolves(
    provenance_labels_script, provenance_label_gh, tmp_path
):
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path,
        run_id="r" * 32, card_id="card1", agent_slug="coder-sonnet",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"aw-run:{'r' * 32}" in state["pr_labels"]
    assert "aw-card:card1" in state["pr_labels"]
    assert "aw-agent:coder-sonnet" in state["pr_labels"]


@pytest.mark.parametrize("run_id,expected", [
    # bare 32-hex trailer value -> dashed 8-4-4-4-12 (what AP-MT looks up)
    ("f582d363e34e47cbb908ad38d6d9b10a", "f582d363-e34e-47cb-b908-ad38d6d9b10a"),
    # already dashed -> unchanged
    ("f582d363-e34e-47cb-b908-ad38d6d9b10a", "f582d363-e34e-47cb-b908-ad38d6d9b10a"),
    # junk: wrong length / non-hex -> unchanged
    ("not-a-run-id", "not-a-run-id"),
    ("f582d363e34e47cbb908ad38d6d9b10", "f582d363e34e47cbb908ad38d6d9b10"),
    ("g" * 32, "g" * 32),
])
def test_aw_run_label_is_normalised_to_the_dashed_form(
    provenance_labels_script, provenance_label_gh, tmp_path, run_id, expected
):
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path,
        run_id=run_id, card_id="4205bf3b-9510-818d-bc3a-c441daadd282",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"aw-run:{expected}" in state["pr_labels"]
    assert len(f"aw-run:{expected}") <= 50
    # aw-card keeps its existing dash-stripping behaviour
    assert "aw-card:4205bf3b9510818dbc3ac441daadd282" in state["pr_labels"]


def test_a_failed_add_keeps_the_existing_bare_aw_run_label(
    provenance_labels_script, provenance_label_gh, tmp_path
):
    """Upgrading a PR from a bare to a dashed aw-run label must not strip the
    old label before the new one is confirmed on the PR."""
    bare = "f582d363e34e47cbb908ad38d6d9b10a"
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path,
        run_id=bare, existing_pr_labels=[f"aw-run:{bare}"], fail_add=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["pr_labels"] == [f"aw-run:{bare}"]
    assert "-X DELETE" not in calls


def test_a_successful_add_replaces_the_bare_aw_run_label(
    provenance_labels_script, provenance_label_gh, tmp_path
):
    bare = "f582d363e34e47cbb908ad38d6d9b10a"
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path,
        run_id=bare, existing_pr_labels=[f"aw-run:{bare}"],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["pr_labels"] == ["aw-run:f582d363-e34e-47cb-b908-ad38d6d9b10a"]


def test_an_empty_value_produces_no_label_for_that_prefix(
    provenance_labels_script, provenance_label_gh, tmp_path
):
    """A run with no card (today's reality for a card-less run) must not
    become a literal `aw-card:` label — same silent-absence treatment as
    the footer's Card/Agent lines."""
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path, run_id="r" * 32,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(n.startswith("aw-run:") for n in state["pr_labels"])
    assert not any(n.startswith("aw-card:") for n in state["pr_labels"])
    assert not any(n.startswith("aw-agent:") for n in state["pr_labels"])


def test_the_card_id_has_its_dashes_stripped(provenance_labels_script, provenance_label_gh, tmp_path):
    """Notion page ids are dashed; the label-name convention strips them."""
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path, run_id="r" * 32,
        card_id="4175bf3b-9510-81f6-978c-ccf444809ccb",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "aw-card:4175bf3b951081f6978cccf444809ccb" in state["pr_labels"]


def test_a_stale_same_prefix_label_is_replaced(provenance_labels_script, provenance_label_gh, tmp_path):
    """Round 2 (or a later `/aw-review`) can resolve a different agent slug
    than round 1 did — the old `aw-agent:` label must not linger alongside
    the new one."""
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path, run_id="r" * 32, agent_slug="coder-opus",
        existing_pr_labels=["aw-run:" + "r" * 32, "aw-agent:coder-sonnet"],
        existing_repo_labels=["aw-run:" + "r" * 32, "aw-agent:coder-sonnet"],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "aw-agent:coder-opus" in state["pr_labels"]
    assert "aw-agent:coder-sonnet" not in state["pr_labels"]
    assert "DELETE" in calls and "aw-agent" in calls


def test_an_already_applied_label_is_left_alone(provenance_labels_script, provenance_label_gh, tmp_path):
    """No redundant create/add call when the exact label is already on the
    PR — idempotent across every push and `/aw-review` round."""
    existing = ["aw-run:" + "r" * 32]
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path, run_id="r" * 32,
        existing_pr_labels=existing, existing_repo_labels=existing,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["pr_labels"] == existing
    assert "-X POST" not in calls, "re-created or re-added an unchanged label"


def test_a_label_create_422_already_exists_is_treated_as_success(
    provenance_labels_script, provenance_label_gh, tmp_path
):
    """The repo-level label already exists (another PR created it first) —
    that 422 must not stop this PR from getting it applied."""
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path, run_id="r" * 32,
        existing_repo_labels=["aw-run:" + "r" * 32],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"aw-run:{'r' * 32}" in state["pr_labels"]


def test_an_oversized_label_is_skipped_not_truncated(provenance_labels_script, provenance_label_gh, tmp_path):
    """GitHub caps label names at 50 chars. A label that would exceed it is
    dropped with a warning, never silently truncated into a lie."""
    long_slug = "a" * 45  # "aw-agent:" + 45 chars = 54, over the limit
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path, run_id="r" * 32, agent_slug=long_slug,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(n.startswith("aw-agent:") for n in state["pr_labels"])
    assert "::warning::" in proc.stdout
    assert f"aw-run:{'r' * 32}" in state["pr_labels"], "the oversized field must not block the others"


def test_the_step_never_exits_nonzero_even_when_gh_hard_fails(
    provenance_labels_script, provenance_label_gh, tmp_path
):
    """`merge-attempt` needs `provenance-gate` to succeed — a hiccup in this
    purely-cosmetic enrichment step must never turn into a blocked merge."""
    proc, state, calls = _run_provenance_labels(
        provenance_labels_script, provenance_label_gh, tmp_path, run_id="r" * 32, card_id="card1",
        agent_slug="coder-sonnet", always_fail=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::warning::" in proc.stdout


# --------------------------------------------------------------------------
# the merge itself — `merge-attempt`, both entry points, one script
# --------------------------------------------------------------------------
#
# This is the half of v1.3.0 that can go wrong in the expensive direction.
# The old `ci_gate` + `Merge if eligible` pair could only ever be reached
# after the gate and the review had both run in the same workflow run, so
# "which PR is this" and "did the review clear this SHA" were answered by
# job outputs. The `workflow_run` entry point has neither: it arrives with a
# CI run's head SHA and nothing else, carrying the caller's full secrets,
# and has to re-derive all of it from the live API.
#
# So the tests below drive the real script against a fake `gh` and assert on
# what it MERGED, not on what it printed. Four of them cover cases that
# cannot be reached by running the workflow on a healthy repo at all.


@pytest.fixture(scope="module")
def merge_script(tmp_path_factory) -> Path:
    src = _extract_heredoc(_MERGE_STEP)
    assert "ELIGIBLE_CONTEXT" in src, "extracted the wrong block — no merge logic"
    path = tmp_path_factory.mktemp("wf") / "merge.py"
    path.write_text(src)
    return path


@pytest.fixture(scope="module")
def merge_gh(tmp_path_factory) -> Path:
    """A `gh` that answers from JSON fixture files in `$FAKE_GH_DIR`.

    Every call the merge script makes is a `gh api` read or the one `gh pr
    merge` write, so faking `gh` fakes the whole GitHub side without a
    network or a monkeypatch. `merged.txt` appearing in the fixture dir is
    the assertion the tests actually care about: it exists iff the script
    decided to merge.
    """
    bin_dir = tmp_path_factory.mktemp("mergebin")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        'd = os.environ["FAKE_GH_DIR"]\n'
        "def emit(name):\n"
        "    p = os.path.join(d, name)\n"
        "    if not os.path.exists(p):\n"
        "        print('gh: no fixture ' + name, file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "    sys.stdout.write(open(p).read())\n"
        "    sys.exit(0)\n"
        "if args[:1] == ['api']:\n"
        "    path = args[1]\n"
        "    if '/commits/' in path and path.endswith('/pulls'):\n"
        "        emit('commits_pulls.json')\n"
        "    if '/commits/' in path and path.endswith('/status'):\n"
        "        emit('status.json')\n"
        "    if '/pulls/' in path:\n"
        # The script re-reads the PR after a failed merge to tell
        # "already merged by the other entry point" from a real failure, so
        # the second read can legitimately differ from the first.
        "        n = os.path.join(d, 'pull_reads')\n"
        "        seen = int(open(n).read()) if os.path.exists(n) else 0\n"
        "        open(n, 'w').write(str(seen + 1))\n"
        "        if seen and os.path.exists(os.path.join(d, 'pull_after.json')):\n"
        "            emit('pull_after.json')\n"
        "        emit('pull.json')\n"
        "if args[:2] == ['pr', 'checks']:\n"
        "    emit('checks.json')\n"
        "if args[:2] == ['pr', 'merge']:\n"
        "    open(os.path.join(d, 'merged.txt'), 'w').write(' '.join(args))\n"
        "    rc = int(os.environ.get('FAKE_MERGE_RC', '0'))\n"
        "    if rc:\n"
        "        print('gh: pull request is not mergeable', file=sys.stderr)\n"
        "    sys.exit(rc)\n"
        "print('gh: unexpected call ' + repr(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    gh.chmod(0o755)
    return bin_dir


REVIEWED_SHA = "b" * 40
OTHER_SHA = "c" * 40
# Sentinel: write no fixture file at all, so the fake `gh` fails that call the
# way a real 403 from a missing `checks: read` does.
_MISSING = object()
_REVIEW_URL = "https://example.invalid/actions/runs/42/attempts/1"
_REVIEW_CLEARANCE = [
    {"context": "devteam/merge-eligible", "state": "success", "target_url": _REVIEW_URL},
    {"context": "devteam/code-review", "state": "success", "target_url": _REVIEW_URL},
]
_ELIGIBLE = {"statuses": [
    *_REVIEW_CLEARANCE,
    {"context": "devteam/provenance", "state": "success"},
]}
_NOT_ELIGIBLE: dict = {"statuses": []}
_PROVENANCE_NOT_CLEARED = {"statuses": _REVIEW_CLEARANCE}
_GREEN = [{"name": "coverage", "state": "SUCCESS", "bucket": "pass"}]


def _open_pr(sha=REVIEWED_SHA, **over):
    pr = {
        "number": 42,
        "state": "open",
        "merged": False,
        "draft": False,
        "head": {"sha": sha, "repo": {"full_name": "tekflox/aw-app-template"}},
    }
    pr.update(over)
    return pr


def _run_merge(
    merge_script,
    merge_gh,
    tmp_path,
    *,
    event="workflow_run",
    pull=None,
    status=None,
    checks=None,
    commits_pulls=None,
    pull_after=None,
    event_sha=REVIEWED_SHA,
    merge_rc=0,
    merge_token="pat-xxx",
):
    d = tmp_path / "ghfix"
    d.mkdir(exist_ok=True)
    # Per-run state the fake `gh` keeps; stale copies would make a second
    # _run_merge in the same tmp_path answer from the previous one's fixtures.
    for leftover in ("merged.txt", "pull_reads", "pull_after.json"):
        if (d / leftover).exists():
            (d / leftover).unlink()
    fixtures = {
        "pull.json": _open_pr() if pull is None else pull,
        "status.json": _ELIGIBLE if status is None else status,
        "checks.json": _GREEN if checks is None else checks,
        "commits_pulls.json": (
            [_open_pr()] if commits_pulls is None else commits_pulls
        ),
    }
    if pull_after is not None:
        fixtures["pull_after.json"] = pull_after
    for name, value in fixtures.items():
        path = d / name
        if value is _MISSING:
            if path.exists():
                path.unlink()
            continue
        path.write_text(json.dumps(value))
    env = {
        **os.environ,
        "PATH": f"{merge_gh}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DIR": str(d),
        "FAKE_MERGE_RC": str(merge_rc),
        "GH_TOKEN": "fake-github-token",
        "MERGE_TOKEN": merge_token,
        "REPO": "tekflox/aw-app-template",
        "EVENT_NAME": event,
        "CI_CHECK_NAME": "coverage",
        "PR_NUMBER": "42" if event != "workflow_run" else "",
        "HEAD_SHA": event_sha if event != "workflow_run" else "",
        "WORKFLOW_RUN_HEAD_SHA": event_sha if event == "workflow_run" else "",
        "WORKFLOW_RUN_NAME": "Test",
        "WORKFLOW_RUN_CONCLUSION": "success",
    }
    proc = subprocess.run(
        [sys.executable, str(merge_script)], capture_output=True, text=True, env=env
    )
    return proc, (d / "merged.txt").exists()


def test_a_cleared_green_pr_merges_from_the_workflow_run_entry_point(
    merge_script, merge_gh, tmp_path
):
    """The release's reason for existing: CI finishing AFTER the verdict now
    merges, instead of the review job having burned 20 minutes waiting and
    given up."""
    proc, merged = _run_merge(merge_script, merge_gh, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert merged, proc.stdout + proc.stderr
    assert "squash-merged #42" in proc.stdout


def test_a_cleared_green_pr_merges_from_the_same_run_entry_point(
    merge_script, merge_gh, tmp_path
):
    """Same script, the other door — the happy path where CI was already
    green when the verdict landed. Both entry points must reach the same
    merge, or the two would drift exactly the way the three copies of this
    file drifted."""
    proc, merged = _run_merge(merge_script, merge_gh, tmp_path, event="pull_request")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert merged


def test_a_workflow_run_with_no_eligible_status_exits_neutral(
    merge_script, merge_gh, tmp_path
):
    """Entry point (b) fires on EVERY completion of the caller's CI workflow,
    on every branch. The overwhelming majority are PRs the review has not
    cleared — those must exit 0 without merging. A non-zero exit here would
    put a red X on normal, healthy CI runs, which is how a workflow gets
    muted and then ignored when it matters."""
    proc, merged = _run_merge(
        merge_script, merge_gh, tmp_path, status=_NOT_ELIGIBLE
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not merged
    assert "has not cleared this head" in proc.stdout


@pytest.mark.parametrize(("review_state", "review_url"), [
    ("pending", _REVIEW_URL),
    ("failure", _REVIEW_URL),
    ("success", "https://example.invalid/actions/runs/42/attempts/2"),
])
def test_merge_requires_success_and_clearance_from_the_same_attempt(
    merge_script, merge_gh, tmp_path, review_state, review_url,
):
    statuses = [dict(status) for status in _ELIGIBLE["statuses"]]
    statuses[1].update(state=review_state, target_url=review_url)
    proc, merged = _run_merge(
        merge_script, merge_gh, tmp_path, status={"statuses": statuses},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not merged
    assert "latest code review attempt" in proc.stdout


def test_eligible_pr_with_no_provenance_status_does_not_merge(
    merge_script, merge_gh, tmp_path
):
    """The review cleared this SHA, but no commit on the PR ever carried a
    verified `AW-Run-Id` trailer and no human applied `provenance-exempt` —
    `provenance-gate` never wrote `devteam/provenance`, and that absence must
    block the merge exactly like a missing `devteam/merge-eligible` does."""
    proc, merged = _run_merge(
        merge_script, merge_gh, tmp_path, status=_PROVENANCE_NOT_CLEARED
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not merged
    assert "devteam/provenance" in proc.stdout


def test_no_open_pr_for_the_event_sha_exits_neutral(merge_script, merge_gh, tmp_path):
    """A push straight to the default branch runs CI too, and its completion
    reaches this job with a SHA no open PR owns."""
    proc, merged = _run_merge(merge_script, merge_gh, tmp_path, commits_pulls=[])
    assert proc.returncode == 0
    assert not merged
    assert "nothing to merge" in proc.stdout


def test_the_merge_refuses_when_the_head_moved_since_the_review(
    merge_script, merge_gh, tmp_path
):
    """The race the polling version had wide open: it merged "the PR", not
    "the SHA the review passed", so a push landing between the verdict and
    the merge shipped an unreviewed head under a review that never saw it.
    The window is much WIDER now, not narrower — the workflow_run hop can
    arrive an hour after the verdict — so this guard is load-bearing, not
    belt-and-braces."""
    proc, merged = _run_merge(
        merge_script,
        merge_gh,
        tmp_path,
        pull=_open_pr(sha=OTHER_SHA),
        commits_pulls=[_open_pr(sha=OTHER_SHA)],
        event_sha=REVIEWED_SHA,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not merged, "merged a head SHA the review never cleared"
    assert "head moved" in proc.stdout


@pytest.mark.parametrize(
    "pull,needle",
    [
        (_open_pr(draft=True), "draft"),
        (_open_pr(state="closed"), "closed"),
        (
            _open_pr(head={"sha": REVIEWED_SHA,
                           "repo": {"full_name": "someone/fork"}}),
            "fork",
        ),
    ],
    ids=["draft", "closed", "fork"],
)
def test_the_live_guards_each_refuse_a_cleared_pr(
    merge_script, merge_gh, tmp_path, pull, needle
):
    """All three re-read live, because all three can change AFTER the review
    cleared the SHA — and the draft one specifically is the moment an author
    most means "do not merge this". The fork guard is independent of the
    gate's: entry point (b) never passes through the gate, and a
    workflow_run run has the caller's secrets regardless of where the PR
    came from."""
    proc, merged = _run_merge(merge_script, merge_gh, tmp_path, pull=pull)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not merged
    assert needle in proc.stdout


def test_an_already_merged_pr_is_a_success_not_a_failure(
    merge_script, merge_gh, tmp_path
):
    """Both entry points can fire for the same head SHA — a CI run completing
    as the verdict posts reaches this code twice. Whichever loses that race
    must not redden a PR that merged perfectly well."""
    proc, _ = _run_merge(
        merge_script, merge_gh, tmp_path, pull=_open_pr(merged=True)
    )
    assert proc.returncode == 0
    assert "already merged" in proc.stdout


def test_a_losing_merge_race_is_a_success(merge_script, merge_gh, tmp_path):
    """The same race one step later: the merge call itself fails because the
    other entry point got there first. The script re-reads the PR rather than
    trusting the exit code."""
    proc, merged = _run_merge(
        merge_script,
        merge_gh,
        tmp_path,
        merge_rc=1,
        pull_after=_open_pr(merged=True),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert merged, "the merge was attempted"
    assert "already merged" in proc.stdout


def test_a_genuinely_failed_merge_is_an_error(merge_script, merge_gh, tmp_path):
    """And the inverse: a merge that failed for a real reason and left the PR
    open must go red. Treating every failure as "probably a race" is how a
    permanently unmergeable PR looks healthy."""
    proc, _ = _run_merge(merge_script, merge_gh, tmp_path, merge_rc=1)
    assert proc.returncode != 0
    assert "gh pr merge failed" in proc.stdout


@pytest.mark.parametrize(
    "checks,needle",
    [
        ([{"name": "coverage", "state": "IN_PROGRESS", "bucket": "pending"}],
         "still running"),
        ([{"name": "coverage", "state": "FAILURE", "bucket": "fail"}],
         "CI is not green"),
        ([], "have not reported a pass"),
        ([{"name": "ui-lint", "state": "SUCCESS", "bucket": "pass"}],
         "have not reported a pass"),
    ],
    ids=["pending", "failed", "no-checks", "required-absent"],
)
def test_ci_that_is_not_green_does_not_merge_and_does_not_go_red(
    merge_script, merge_gh, tmp_path, checks, needle
):
    """Three distinct not-yet states, all exit 0 without merging.

    `pending` exits neutral BY DESIGN and is the single biggest behaviour
    change in v1.3.0: the old gate slept 20 seconds and looped here. The
    retry is now the CI-completion event itself, which is both free and
    unbounded in time.

    `required-absent` is the rule that "absent must never read as fine" —
    `ci_check_name` must be PRESENT and passing, so a CI job that was
    renamed or deleted stops merges rather than silently re-opening the
    window.
    """
    proc, merged = _run_merge(merge_script, merge_gh, tmp_path, checks=checks)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not merged
    assert needle in proc.stdout


def test_the_loops_own_checks_never_block_the_merge(merge_script, merge_gh, tmp_path):
    """`merge-attempt` is PENDING while this very code runs, and
    `devteam/merge-eligible` is a status on the same SHA. Either one missing
    from the advisory set means the single read always sees something pending
    and nothing ever merges again — the same self-deadlock the old gate had
    with `devteam/code-review`, one job over. A reusable workflow's jobs
    arrive as `<caller job> / <job>`, hence the prefixed forms."""
    proc, merged = _run_merge(
        merge_script,
        merge_gh,
        tmp_path,
        checks=_GREEN + [
            {"name": "review / merge-attempt", "state": "IN_PROGRESS",
             "bucket": "pending"},
            {"name": "review / code-review", "state": "IN_PROGRESS",
             "bucket": "pending"},
            {"name": "devteam/merge-eligible", "state": "SUCCESS",
             "bucket": "pass"},
            # A real `provenance-gate` run can also FAIL its own check-run
            # (no trailer this round, provenance-exempt not yet applied) —
            # that must not reach the generic "any non-advisory check
            # failing blocks" scan either: the actual gate is the
            # `devteam/provenance` status, not this raw check-run.
            {"name": "review / provenance-gate", "state": "FAILURE",
             "bucket": "fail"},
            {"name": "CodeRabbit", "state": "IN_PROGRESS", "bucket": "pending"},
            # v1.13.0: a red review-ceiling status (written on a REFUSED head,
            # which `gh pr checks` lists like any commit status) is advisory.
            {"name": "devteam/review-cap", "state": "FAILURE", "bucket": "fail"},
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert merged, "an advisory check blocked the merge"


def test_the_merge_refuses_to_fall_back_to_github_token(
    merge_script, merge_gh, tmp_path
):
    """A GITHUB_TOKEN-authored merge push does not trigger downstream
    workflows, so the caller's release/deploy/image-build would silently stop
    firing — green CI, nothing shipping. This must fail loudly, not quietly
    merge with the wrong token."""
    proc, merged = _run_merge(merge_script, merge_gh, tmp_path, merge_token="")
    assert proc.returncode != 0
    assert not merged
    assert "AW_DEVTEAM_MERGE_TOKEN is not set" in proc.stdout


def test_an_unreadable_checks_api_refuses_rather_than_merging(
    merge_script, merge_gh, tmp_path
):
    """The `checks: read` / `actions: read` failure mode, which cost AP-MT
    four consecutive review rounds: the read ERRORS instead of returning
    rows. After three attempts this must go RED — not merge, and not exit
    neutral either, because a scope gap that silently stopped merging would
    be indistinguishable from "nothing was eligible today"."""
    proc, merged = _run_merge(merge_script, merge_gh, tmp_path, checks=_MISSING)
    assert not merged
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "could not read #42's checks after 3 attempts" in proc.stdout


def test_a_failed_read_is_retried_three_times_and_no_more(
    merge_script, merge_gh, tmp_path
):
    """The retry budget covers a transient API error, NOT a slow CI run —
    that distinction is the whole release. Three bounded attempts with a few
    seconds between them, versus the 20-minute loop they replaced."""
    proc, _ = _run_merge(merge_script, merge_gh, tmp_path, checks=_MISSING)
    assert proc.stdout.count("checks read attempt") == 3


def test_every_long_running_job_still_has_a_ceiling():
    """A job with no `timeout-minutes` inherits GitHub's 6-hour default, and
    a stuck one then holds a runner (a SELF-HOSTED runner, on two of the four
    callers) for the rest of the afternoon. Both jobs that can block on
    something external must declare one, and the review job's must still
    clear the dispatch's own 900s = 15-minute server-side ceiling."""
    text = WORKFLOW.read_text()

    def job_timeout(job: str) -> int:
        block = text[text.index(f"\n  {job}:") :]
        m = re.search(r"timeout-minutes: (\d+)", block)
        assert m, f"the {job} job has no timeout-minutes"
        return int(m.group(1))

    review = job_timeout("code-review")
    assert review > 15, (
        f"the code-review job's {review}m budget does not clear the reviewer "
        f"dispatch's own 15m server-side ceiling"
    )
    assert job_timeout("merge-attempt") <= review, (
        "merge-attempt does nothing but bounded API calls; it must not be "
        "budgeted like a job that waits on a judge"
    )


# --------------------------------------------------------------------------
# v1.5.0: the close-gate feed — `cleared` event, and `merged` payload
# enrichment (pr_head_sha + unchecked_items). Neither wakes the coder; both
# exist so the close-gate (card merge-gate-close-loop-backlog-cards) has a
# durable row to look up and a merge-time PR body to read.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fake_curl(tmp_path_factory) -> Path:
    """A `curl` on PATH that never touches the network.

    Mimics the one invocation shape both event-POST steps use: `-o <path>`
    (where the response body lands) and `--data-binary @<path>` (the signed
    payload). Captures the posted payload to `$CURL_CAPTURE` when set, and
    answers with `$CURL_HTTP_CODE` (default 200) the way real curl's
    `-w "%{http_code}"` does — on stdout, no trailing newline, body routed
    to `-o` instead.
    """
    bin_dir = tmp_path_factory.mktemp("bin")
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        'out=""\n'
        'body_file=""\n'
        'prev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-o" ]; then out="$arg"; fi\n'
        '  case "$arg" in\n'
        "    @*) body_file=\"${arg#@}\" ;;\n"
        "  esac\n"
        '  prev="$arg"\n'
        "done\n"
        'if [ -n "${CURL_CAPTURE:-}" ] && [ -n "$body_file" ]; then cp "$body_file" "$CURL_CAPTURE"; fi\n'
        'if [ -n "$out" ]; then printf "%s" "${CURL_BODY:-{\\"ok\\":true}}" > "$out"; fi\n'
        'printf "%s" "${CURL_HTTP_CODE:-200}"\n'
    )
    curl.chmod(0o755)
    return bin_dir


@pytest.fixture(scope="module")
def report_round_script(tmp_path_factory) -> Path:
    """The `Report round outcome to the platform` step — the bash
    event-selection logic plus its inlined python HMAC-POST — extracted
    verbatim between its own `run: |` and the next job."""
    text = WORKFLOW.read_text()
    start = text.index("- name: Report round outcome to the platform")
    run_marker = text.index("run: |", start)
    body_start = text.index("\n", run_marker) + 1
    first_pyeof = text.index("PYEOF", run_marker)
    second_pyeof = text.index("PYEOF", first_pyeof + len("PYEOF"))
    end = second_pyeof + len("PYEOF")
    body = _dedent_block(text[body_start:end])
    assert "event=cleared" in body, "extracted the wrong block"
    path = tmp_path_factory.mktemp("wf") / "report_round.sh"
    path.write_text(body)
    return path


def _run_report_round(
    script: Path,
    tmp_path,
    fake_curl,
    *,
    findings: list,
    blocking: list,
    counts: dict,
    round_: str,
    secret: str = "s3cr3t",
) -> dict:
    """Drive the real step script and return the captured HTTP payload (or
    `{}` if nothing was posted)."""
    (tmp_path / "devteam_findings.json").write_text(json.dumps(findings))
    (tmp_path / "devteam_blocking.json").write_text(json.dumps(blocking))
    (tmp_path / "devteam_counts.json").write_text(json.dumps(counts))
    capture = tmp_path / "captured_payload.json"
    env = {
        **os.environ,
        "PATH": f"{fake_curl}{os.pathsep}{os.environ['PATH']}",
        "AW_DEVTEAM_EVENT_SECRET": secret,
        "EVENT_URL": "https://example.invalid",
        "REPO": "tekflox/aw-app-template",
        "PR_NUMBER": "42",
        "HEAD_SHA": "a" * 40,
        "ROUND": round_,
        "FINDINGS_COUNT": str(len(findings)),
        "BLOCKING_COUNT": str(len(blocking)),
        "ELIGIBLE": "true" if not blocking else "false",
        "PR_URL": "https://github.com/tekflox/aw-app-template/pull/42",
        "RUNNER_TEMP": str(tmp_path),
        "CURL_CAPTURE": str(capture),
    }
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"step script failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(capture.read_text()) if capture.exists() else {}


def test_a_round1_clean_pr_sends_cleared_with_an_empty_digest(
    report_round_script, tmp_path, fake_curl
):
    """The exact gap the close-gate design closes: today this is "nothing to
    report". Now it is a durable, empty row — proof the head was reviewed
    clean, not merely that nobody looked."""
    sent = _run_report_round(
        report_round_script, tmp_path, fake_curl,
        findings=[], blocking=[], counts={}, round_="1",
    )
    assert sent["event"] == "cleared"
    assert sent["findings"] == []
    assert sent["blocking"] == []
    assert sent["round"] == "1"
    assert sent["head_sha"] == "a" * 40


def test_an_unset_secret_still_skips_cleanly_without_posting(
    report_round_script, tmp_path, fake_curl
):
    """No regression on the existing short-circuit: an unset secret must
    still exit 0 with nothing posted, cleared event included."""
    (tmp_path / "devteam_findings.json").write_text("[]")
    (tmp_path / "devteam_blocking.json").write_text("[]")
    (tmp_path / "devteam_counts.json").write_text("{}")
    capture = tmp_path / "captured_payload.json"
    env = {
        **os.environ,
        "PATH": f"{fake_curl}{os.pathsep}{os.environ['PATH']}",
        "AW_DEVTEAM_EVENT_SECRET": "",
        "EVENT_URL": "https://example.invalid",
        "REPO": "tekflox/aw-app-template",
        "PR_NUMBER": "42",
        "HEAD_SHA": "a" * 40,
        "ROUND": "1",
        "FINDINGS_COUNT": "0",
        "BLOCKING_COUNT": "0",
        "ELIGIBLE": "true",
        "PR_URL": "https://github.com/tekflox/aw-app-template/pull/42",
        "RUNNER_TEMP": str(tmp_path),
        "CURL_CAPTURE": str(capture),
    }
    proc = subprocess.run(["bash", str(report_round_script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert not capture.exists(), "no secret means no POST, cleared included"


# --------------------------------------------------------------------------
# `merged-event` payload enrichment: pr_head_sha + unchecked_items
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def merged_event_script(tmp_path_factory) -> Path:
    """The `merged-event` job's inlined python — builds the `merged`
    payload, now including `pr_head_sha` and `unchecked_items`."""
    src = _extract_heredoc("- name: Report the merge to the platform")
    assert "unchecked_items" in src, "extracted the wrong block"
    path = tmp_path_factory.mktemp("wf") / "merged_event.py"
    path.write_text(src)
    return path


def _run_merged_event(
    script: Path, tmp_path, fake_curl, *, pr_body: str, secret: str = "s3cr3t"
) -> dict:
    capture = tmp_path / "captured_merged_payload.json"
    env = {
        **os.environ,
        "PATH": f"{fake_curl}{os.pathsep}{os.environ['PATH']}",
        "AW_DEVTEAM_EVENT_SECRET": secret,
        "EVENT_URL": "https://example.invalid",
        "REPO": "tekflox/aw-app-template",
        "PR_NUMBER": "42",
        "PR_URL": "https://github.com/tekflox/aw-app-template/pull/42",
        "MERGE_SHA": "b" * 40,
        "PR_HEAD_SHA": "c" * 40,
        "PR_BODY": pr_body,
        "ROUND": "1",
        "RUNNER_TEMP": str(tmp_path),
        "CURL_CAPTURE": str(capture),
    }
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"merged-event script failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(capture.read_text())


def test_merged_payload_keeps_merge_sha_and_adds_pr_head_sha(
    merged_event_script, tmp_path, fake_curl
):
    """The bug the Architect is fixing: `head_sha` has always been the squash
    merge commit, which a review digest (keyed on the PR's real HEAD) can
    never match. Both fields must be present — old callers keep reading
    `head_sha`, the close-gate lookup reads the new one."""
    sent = _run_merged_event(merged_event_script, tmp_path, fake_curl, pr_body="")
    assert sent["head_sha"] == "b" * 40
    assert sent["pr_head_sha"] == "c" * 40


def test_a_pr_body_with_zero_checklist_items_yields_an_empty_list(
    merged_event_script, tmp_path, fake_curl
):
    sent = _run_merged_event(
        merged_event_script, tmp_path, fake_curl,
        pr_body="Just a plain description, no checklist at all.\n\n- not a checkbox\n- [x] already checked",
    )
    assert sent["unchecked_items"] == []


def test_a_checklist_item_inside_an_html_comment_is_not_picked_up(
    merged_event_script, tmp_path, fake_curl
):
    """Commented-out PR-template boilerplate must not surface as a real
    unresolved item — only the live checklist item outside the comment
    should survive."""
    body = (
        "<!--\n"
        "Template boilerplate, never filled in:\n"
        "- [ ] this is commented out and must not appear\n"
        "-->\n"
        "## Checklist\n"
        "- [ ] this one is real and must appear\n"
        "- [x] this one is checked and must not appear\n"
    )
    sent = _run_merged_event(merged_event_script, tmp_path, fake_curl, pr_body=body)
    assert sent["unchecked_items"] == ["this one is real and must appear"]


def test_a_pathologically_long_body_is_truncated_not_crashed(
    merged_event_script, tmp_path, fake_curl
):
    """Bounded to 50 items of 300 chars each — truncate, never error."""
    lines = [f"- [ ] item {i} " + ("x" * 500) for i in range(200)]
    body = "\n".join(lines)
    sent = _run_merged_event(merged_event_script, tmp_path, fake_curl, pr_body=body)
    assert len(sent["unchecked_items"]) == 50
    assert all(len(item) <= 300 for item in sent["unchecked_items"])
    assert sent["unchecked_items"][0].startswith("item 0")


# --------------------------------------------------------------------------
# verdict-validation bypasses (independent review f6736d08)
# --------------------------------------------------------------------------


def _run_raw_content(script, tmp_path, fake_gh, content, round_="1"):
    """Like `_run_eligibility`, but the judge's message is `content` verbatim."""
    (tmp_path / "dispatch_response.json").write_text(
        json.dumps({"choices": [{"message": {"content": content}}]})
    )
    out = tmp_path / "gh_output"
    out.write_text("")
    env = {
        **os.environ,
        "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "REPO": "tekflox/aw-app-template", "PR_NUMBER": "42", "HEAD_SHA": "a" * 40,
        "ROUND": round_, "IS_DRAFT": "false", "GITHUB_OUTPUT": str(out),
        "GH_TOKEN": "fake", "RUNNER_TEMP": str(tmp_path),
        "RUN_URL": "https://example.invalid/run/1",
        "GH_LOG": str(tmp_path / "gh_calls.log"), "AGENT_SLUG": "code-reviewer-sonnet",
    }
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)


def _assert_rejected(proc, tmp_path):
    assert proc.returncode != 0, "a malformed verdict must fail the step"
    assert "invalid review verdict" in proc.stdout + proc.stderr
    assert not any("statuses" in c for c in _gh_calls(tmp_path)), "no status may be published"
    assert (tmp_path / "gh_output").read_text() == ""


@pytest.mark.parametrize("findings,counts", [
    ([], {"high": 1}),
    ([], {"low": 2}),
    ([MINOR], {}),
    ([MINOR], {"low": 2}),
    ([MINOR], {"high": 1}),
    ([MINOR], {"low": "1"}),
    ([MINOR], {"low": True}),
    ([MINOR], {"low": -1}),
])
@pytest.mark.parametrize("round_", ["1", "informational"])
def test_counts_that_contradict_findings_are_rejected(
    eligibility_script, tmp_path, fake_gh, findings, counts, round_,
):
    proc = _run_raw_content(
        eligibility_script, tmp_path, fake_gh,
        json.dumps({"findings": findings, "counts": counts}), round_=round_,
    )
    _assert_rejected(proc, tmp_path)


def test_a_valid_empty_verdict_with_zero_counts_stays_eligible(eligibility_script, tmp_path, fake_gh):
    for counts in ({}, {"high": 0, "low": 0}):
        got = _run_eligibility(
            eligibility_script, tmp_path, fake_gh, findings=[], counts=counts, round_="1"
        )
        assert got["eligible"] == "true"


@pytest.mark.parametrize("content", [
    '```json\n[{"findings": [], "counts": {}}]\n```',
    '[{"findings": [], "counts": {}}]',
    '```json\n{"findings": [], "counts": {}} trailing {"x": 1}\n```',
    '{"findings": [], "counts": {}} {"findings": [], "counts": {}}',
    '```json\nnot json {"findings": [], "counts": {}}\n```',
])
def test_the_verdict_must_be_one_complete_top_level_object(
    eligibility_script, tmp_path, fake_gh, content,
):
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, content)
    assert proc.returncode != 0
    assert not any("statuses" in c for c in _gh_calls(tmp_path))
    assert (tmp_path / "gh_output").read_text() == ""


def test_prose_before_a_fenced_object_still_parses(eligibility_script, tmp_path, fake_gh):
    proc = _run_raw_content(
        eligibility_script, tmp_path, fake_gh,
        'Here you go:\n```json\n{"findings": [], "counts": {}}\n```\nThanks',
    )
    assert proc.returncode == 0, proc.stderr
    assert any("merge-eligible" in c for c in _gh_calls(tmp_path))


def test_an_unfenced_bare_object_still_parses(eligibility_script, tmp_path, fake_gh):
    proc = _run_raw_content(
        eligibility_script, tmp_path, fake_gh, '{"findings": [], "counts": {}}'
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# the final status publisher, driven from a rejected verdict
# --------------------------------------------------------------------------


def _extract_run(step_name: str) -> str:
    """The bash `run: |` block of the step called `step_name`."""
    text = WORKFLOW.read_text()
    anchor = text.index(f"- name: {step_name}")
    run = text.index("        run: |\n", anchor)
    body_start = run + len("        run: |\n")
    # the block ends at the next line indented less than 10 spaces
    lines, out = text[body_start:].split("\n"), []
    for line in lines:
        if line.strip() and not line.startswith(" " * 10):
            break
        out.append(line)
    block = _dedent_block("\n".join(out))
    # GitHub expressions are expanded before bash ever sees them; stand in.
    return re.sub(r"\$\{\{[^}]*\}\}", "x", block)


def test_a_rejected_verdict_publishes_failure_and_never_eligibility(
    eligibility_script, tmp_path, fake_gh,
):
    # 1) the findings step rejects the verdict ...
    proc = _run_raw_content(
        eligibility_script, tmp_path, fake_gh,
        json.dumps({"findings": [], "counts": {"high": 1}}),
    )
    assert proc.returncode != 0
    # 2) ... so its outcome is `failure`; drive the real publisher with that.
    script = tmp_path / "publish.sh"
    script.write_text(_extract_run("Publish code-review status"))
    log = tmp_path / "publish.log"
    env = {
        **os.environ, "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(log), "GH_TOKEN": "fake",
        "DISPATCH_OUTCOME": "success", "FINDINGS_OUTCOME": "failure", "DESC": "",
    }
    subprocess.run(["bash", str(script)], check=True, capture_output=True, text=True, env=env)
    calls = log.read_text()
    assert "state=failure" in calls and "context=devteam/code-review" in calls
    assert "state=success" not in calls
    assert "merge-eligible" not in calls
    assert not any("merge-eligible" in c for c in _gh_calls(tmp_path))


@pytest.mark.parametrize("round_,context", [
    ("informational", "devteam/code-review-advisory"),
    ("1", "devteam/code-review"),
    ("bypass", "devteam/code-review"),
])
def test_an_informational_review_never_writes_the_merge_authorizing_context(
    tmp_path, fake_gh, round_, context,
):
    script = tmp_path / "publish.sh"
    script.write_text(_extract_run("Publish code-review status"))
    log = tmp_path / "publish.log"
    env = {
        **os.environ, "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(log), "GH_TOKEN": "fake", "ROUND": round_, "MODE": "review",
        "DISPATCH_OUTCOME": "success", "FINDINGS_OUTCOME": "success", "DESC": "clean",
    }
    subprocess.run(["bash", str(script)], check=True, capture_output=True, text=True, env=env)
    calls = log.read_text()
    assert f"context={context}" in calls
    if round_ == "informational":
        assert "context=devteam/code-review " not in calls and "context=devteam/code-review\n" not in calls


def test_the_pending_status_of_an_informational_run_is_advisory_too():
    text = WORKFLOW.read_text()
    step = text[text.index("      - name: Mark code review pending"):text.index("      # Agent-scoped Agents Platform ApiKey")]
    assert "round == 'informational' && 'devteam/code-review-advisory'" in step
    assert "devteam/code-review-advisory" in text[text.index("ADVISORY = {"):text.index("ADVISORY = {") + 400]


# --------------------------------------------------------------------------
# dispatch retry on transient failure
# --------------------------------------------------------------------------


@pytest.fixture()
def dispatch_env(tmp_path, fake_gh):
    """Run the real Dispatch step with a scripted `curl` and a recording `sleep`.

    `codes` is the HTTP code (or `CURLFAIL:<exit>`) each successive curl call
    yields; the last one repeats. An `E` prefix (`E200`) makes that call's
    body a successful run whose final message is empty.
    """
    bindir = tmp_path / "dbin"
    bindir.mkdir()
    (bindir / "sleep").write_text('#!/bin/bash\necho "$1" >> "$SLEEP_LOG"\n')
    (bindir / "curl").write_text(
        "#!/bin/bash\n"
        'n=$(cat "$CURL_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$CURL_COUNT"\n'
        'IFS=, read -ra c <<< "$CURL_CODES"\n'
        'i=$((n-1)); [ $i -ge ${#c[@]} ] && i=$((${#c[@]}-1)); code="${c[$i]}"\n'
        'out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        'if [[ "$code" == CURLFAIL:* ]]; then echo "curl: (7) refused" >&2; exit "${code#CURLFAIL:}"; fi\n'
        'msg=\'{}\'; if [[ "$code" == E* ]]; then code="${code#E}"; msg=\'\'; fi\n'
        'if [[ "$code" == N* ]]; then code="${code#N}"; [ -n "$out" ] && echo \'{"aw_status":"error","choices":[{"message":{"content":"{\\"output\\": \\"diag: connection refused\\", \\"errors\\": {\\"review\\": \\"no activity for 600s\\"}}"}}]}\' > "$out"; printf "%s" "$code"; exit 0; fi\n'
        'if [[ "$code" == U* ]]; then code="${code#U}"; [ -n "$out" ] && echo \'{"aw_status":"error","choices":[{"message":{"content":"{\\"errors\\": {\\"review\\": \\"upstream unreachable: dial tcp: connect: connection refused\\"}}"}}]}\' > "$out"; printf "%s" "$code"; exit 0; fi\n'
        '[ -n "$out" ] && jq -n --arg c "$msg" \'{aw_status: "success", choices: [{message: {content: $c}}]}\' > "$out"\n'
        'if [[ "$code" == *:* ]]; then printf "%s" "${code%%:*}"; exit "${code##*:}"; fi\n'
        'printf "%s" "$code"\n'
    )
    for f in ("sleep", "curl"):
        (bindir / f).chmod(0o755)
    script = tmp_path / "dispatch.sh"
    script.write_text(_extract_run("Dispatch Code Reviewer"))

    def run(codes: str):
        env = {
            **os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "CURL_CODES": codes, "CURL_COUNT": str(tmp_path / "n"),
            "SLEEP_LOG": str(tmp_path / "sleeps"), "RUNNER_TEMP": str(tmp_path),
            "AW_CODEREVIEW_V1_KEY": "k", "DISPATCH_URL": "https://example.invalid",
            "AGENT_SLUG": "code-reviewer-sonnet", "WORKFLOW_SLUG": "", "REPO": "o/r",
            "PR_NUMBER": "1", "HEAD_SHA": "a" * 40, "BASE_REF": "master",
            "MODE": "review", "ROUND": "1",
        }
        proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
        n = int((tmp_path / "n").read_text()) if (tmp_path / "n").exists() else 0
        sl = tmp_path / "sleeps"
        return proc, n, (sl.read_text().split() if sl.exists() else [])

    return run


def test_dispatch_retries_502_then_succeeds(dispatch_env):
    proc, n, sleeps = dispatch_env("502,503,200")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert n == 3 and sleeps == ["15", "30"]


def test_dispatch_retries_a_connection_failure_then_succeeds(dispatch_env):
    proc, n, sleeps = dispatch_env("CURLFAIL:7,200")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert n == 2 and sleeps == ["15"]


def test_dispatch_gives_up_after_four_retries_with_backoff(dispatch_env):
    proc, n, sleeps = dispatch_env("504")
    assert proc.returncode != 0
    assert n == 5 and sleeps == ["15", "30", "60", "90"]
    assert "dispatch failed" in proc.stdout + proc.stderr


@pytest.mark.parametrize("code", ["400", "401", "403", "404", "422"])
def test_dispatch_does_not_retry_a_4xx(dispatch_env, code):
    proc, n, sleeps = dispatch_env(code)
    assert proc.returncode != 0
    assert n == 1 and sleeps == []


def test_a_failed_dispatch_never_sets_success_or_eligibility(dispatch_env, tmp_path, fake_gh):
    proc, _, _ = dispatch_env("502")
    assert proc.returncode != 0
    script = tmp_path / "publish.sh"
    script.write_text(_extract_run("Publish code-review status"))
    log = tmp_path / "publish.log"
    env = {
        **os.environ, "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(log), "GH_TOKEN": "fake",
        "DISPATCH_OUTCOME": "failure", "FINDINGS_OUTCOME": "skipped", "DESC": "",
    }
    subprocess.run(["bash", str(script)], check=True, capture_output=True, text=True, env=env)
    calls = log.read_text()
    assert "state=failure" in calls and "state=success" not in calls


# --------------------------------------------------------------------------
# second re-review (run b7197ae0): five more fail-closed gaps
# --------------------------------------------------------------------------

_EMPTY = '{"findings": [], "counts": {}}'
_HIGH = json.dumps({"findings": [{**MINOR, "severity": "high"}], "counts": {"high": 1}})


def test_two_fenced_verdicts_are_rejected(eligibility_script, tmp_path, fake_gh):
    content = f"```json\n{_EMPTY}\n```\nand also\n```json\n{_HIGH}\n```"
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, content)
    assert proc.returncode != 0
    assert not any("statuses" in c for c in _gh_calls(tmp_path))


def test_a_single_fence_with_prose_around_it_still_parses(eligibility_script, tmp_path, fake_gh):
    content = f"Review done.\n```json\n{_EMPTY}\n```\nHope that helps."
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, content)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("content", [
    '{"findings": [], "counts": {}, "findings": []}',
    '{"findings": [], "counts": {"high": 0, "high": 0}}',
])
def test_duplicate_top_level_and_count_keys_are_rejected(
    eligibility_script, tmp_path, fake_gh, content,
):
    _assert_rejected(_run_raw_content(eligibility_script, tmp_path, fake_gh, content), tmp_path)


def _run_with_envelope(script, tmp_path, fake_gh, envelope):
    (tmp_path / "dispatch_response.json").write_text(json.dumps(envelope))
    out = tmp_path / "gh_output"
    out.write_text("")
    env = {
        **os.environ, "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
        "REPO": "o/r", "PR_NUMBER": "42", "HEAD_SHA": "a" * 40, "ROUND": "1",
        "IS_DRAFT": "false", "GITHUB_OUTPUT": str(out), "GH_TOKEN": "fake",
        "RUNNER_TEMP": str(tmp_path), "RUN_URL": "https://example.invalid/run/1",
        "GH_LOG": str(tmp_path / "gh_calls.log"), "AGENT_SLUG": "code-reviewer-sonnet",
    }
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)


def _envelope(**extra):
    return {"choices": [{"message": {"content": _EMPTY}}], **extra}


@pytest.mark.parametrize("extra", [
    {"aw_status": "error"}, {"aw_status": "cancelled"}, {"aw_status": None},
    {"aw_status": ""}, {"error": {"message": "boom"}},
])
def test_a_non_success_run_status_in_a_2xx_body_is_rejected(
    eligibility_script, tmp_path, fake_gh, extra,
):
    proc = _run_with_envelope(eligibility_script, tmp_path, fake_gh, _envelope(**extra))
    assert proc.returncode != 0
    assert "invalid review verdict" in proc.stdout + proc.stderr
    assert not any("statuses" in c for c in _gh_calls(tmp_path))
    assert (tmp_path / "gh_output").read_text() == ""


def test_an_explicit_success_status_is_accepted(eligibility_script, tmp_path, fake_gh):
    proc = _run_with_envelope(
        eligibility_script, tmp_path, fake_gh, _envelope(aw_status="success"))
    assert proc.returncode == 0, proc.stderr


def test_a_body_without_aw_status_is_accepted_until_the_door_deploys(
    eligibility_script, tmp_path, fake_gh,
):
    # Compatibility: AP-MT PR 108 adds the field; older doors omit it.
    proc = _run_with_envelope(eligibility_script, tmp_path, fake_gh, _envelope())
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("counts", [
    {"HIGH": 2, "high": 1},
    {"High": 1},
    {" high": 1},
    {"HIGH": 1},
])
def test_non_canonical_count_keys_are_rejected(eligibility_script, tmp_path, fake_gh, counts):
    high = {**HIGH_NON_BLOCKING}
    proc = _run_raw_content(
        eligibility_script, tmp_path, fake_gh,
        json.dumps({"findings": [high], "counts": counts}), round_="1",
    )
    _assert_rejected(proc, tmp_path)


def test_a_4xx_with_an_interrupted_body_is_not_retried(dispatch_env):
    proc, n, sleeps = dispatch_env("401:18,200")
    assert proc.returncode != 0
    assert n == 1 and sleeps == []


def test_a_5xx_with_an_interrupted_body_is_retried(dispatch_env):
    proc, n, sleeps = dispatch_env("502:18,200")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert n == 2 and sleeps == ["15"]


def test_an_interrupted_2xx_body_is_retried_not_accepted(dispatch_env):
    proc, n, sleeps = dispatch_env("200:18,200")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert n == 2 and sleeps == ["15"]


# --------------------------------------------------------------------------
# unfenced verdict wrapped in prose: extract first `{` .. last `}`
# --------------------------------------------------------------------------


@pytest.mark.parametrize("content", [
    'I reviewed the PR and found nothing.\n\n' + _EMPTY,
    _EMPTY + '\n\nLet me know if you want more detail.',
    'All tests passed.\n\n' + _EMPTY + '\n\nDone.',
])
def test_an_unfenced_verdict_wrapped_in_prose_parses(eligibility_script, tmp_path, fake_gh, content):
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, content)
    assert proc.returncode == 0, proc.stderr
    assert any("merge-eligible" in c for c in _gh_calls(tmp_path))


def test_an_unfenced_prose_wrapped_blocking_verdict_still_blocks(eligibility_script, tmp_path, fake_gh):
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, "Found issues.\n" + _HIGH + "\nBye")
    assert proc.returncode == 0, proc.stderr
    assert not any("merge-eligible" in c and "success" in c for c in _gh_calls(tmp_path))


@pytest.mark.parametrize("content", [
    'first ' + _EMPTY + ' then ' + _HIGH,
    'prose ' + _EMPTY + ' and {"x": 1}',
    'prose [' + _EMPTY + ']',
    'no verdict here',
    'prose {"findings": [], "counts": {}, "findings": []} end',
])
def test_prose_wrapped_extraction_stays_fail_closed(eligibility_script, tmp_path, fake_gh, content):
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, content)
    assert proc.returncode != 0
    assert not any("statuses" in c for c in _gh_calls(tmp_path))


# --------------------------------------------------------------------------
# PR #46 (runs 44542807965 / 44544164333): narration before a bare verdict,
# braces inside that narration, and an empty final message
# --------------------------------------------------------------------------

# Verbatim shape of what code-reviewer-pipeline returned on PR #46: two
# narration paragraphs, then the unfenced verdict (findings trimmed).
_PR46_REAL = (
    "I\u2019ll use the `aw-agent-code-reviewer` skill and check the round-one "
    "replies against the supplied commit.\n\nBoth round-one fixes passed the "
    "targeted tests. I\u2019m checking how the new retries interact with forced "
    "catalog refreshes.\n\n"
    + json.dumps({
        "findings": [{**HIGH_NON_BLOCKING, "severity": "medium",
                      "evidence": "src/apps/catalog.py:436\u2013448"}],
        "counts": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
        "research_runs_used": 0,
    }, indent=2)
)


@pytest.mark.parametrize("content", [
    _PR46_REAL,
    "I checked the `{key}` placeholder in f\"{x}\".\n\n" + _EMPTY,
    _EMPTY + "\n\nNote: `config{}` stays as-is.",
    "Returns `{}` or `[]` on a miss, `[1, 2]` otherwise.\n" + _EMPTY + "\n}",
])
def test_a_verdict_is_found_among_prose_braces(eligibility_script, tmp_path, fake_gh, content):
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, content, round_="1")
    assert proc.returncode == 0, proc.stderr
    assert "findings_count=" in (tmp_path / "gh_output").read_text()


def test_the_pr46_verdict_keeps_its_finding(eligibility_script, tmp_path, fake_gh):
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, _PR46_REAL, round_="1")
    assert proc.returncode == 0, proc.stderr
    out = dict(l.split("=", 1) for l in (tmp_path / "gh_output").read_text().splitlines() if "=" in l)
    assert out["findings_count"] == "1" and out["eligible"] == "false"


@pytest.mark.parametrize("content", [
    "Example: {\"findings\": [], \"counts\": {}} is clean.\n" + _HIGH,
    _HIGH + "\nFor reference: {\"a\": 1}",
])
def test_two_json_objects_among_prose_are_rejected(eligibility_script, tmp_path, fake_gh, content):
    _assert_rejected(_run_raw_content(eligibility_script, tmp_path, fake_gh, content), tmp_path)


@pytest.mark.parametrize("content", ["", "   \n\t"])
def test_an_empty_message_is_rejected_by_name(eligibility_script, tmp_path, fake_gh, content):
    proc = _run_raw_content(eligibility_script, tmp_path, fake_gh, content)
    _assert_rejected(proc, tmp_path)
    assert "empty message" in proc.stdout + proc.stderr


def test_an_empty_reviewer_message_is_redispatched_once(dispatch_env):
    proc, n, sleeps = dispatch_env("E200,200")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert n == 2 and sleeps == ["15"]


def test_a_runner_upstream_unreachable_200_is_retried(dispatch_env):
    proc, n, sleeps = dispatch_env("U200,U200,200")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert n == 3 and sleeps == ["15", "30"]


def test_an_idle_timeout_is_not_retried_even_if_other_output_says_refused(dispatch_env):
    proc, n, sleeps = dispatch_env("N200")
    assert proc.returncode == 0  # the findings step, not dispatch, rejects the failed run
    assert n == 1 and sleeps == []


def test_a_runner_unreachable_that_never_recovers_fails_the_dispatch(dispatch_env):
    proc, n, sleeps = dispatch_env("U200")
    assert proc.returncode != 0
    assert n == 5 and "runner-unreachable" in proc.stdout + proc.stderr


def test_a_second_empty_reviewer_message_fails_the_dispatch(dispatch_env):
    proc, n, sleeps = dispatch_env("E200")
    assert proc.returncode != 0
    assert n == 2 and sleeps == ["15"]
    assert "empty-reviewer-message" in proc.stdout + proc.stderr


# --------------------------------------------------------------------------
# review caps (v1.11.0): one automatic needs-human re-review, and verdict
# reuse for an unchanged (sha, round, mode). Drives the gate's real tail.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gate_tail_script(tmp_path_factory) -> Path:
    text = WORKFLOW.read_text()
    start = text.index("          # ---- a push means the coder paid its debt")
    end = text.index("      - name: Report review ceiling")
    body = (
        text[start:end]
        .replace("${{ github.repository }}", "tekflox/aw-app-template")
        .replace('"$GITHUB_OUTPUT"', '"$OUTF"')
        .replace("${{ github.event.action }}", "${ACTION}")
        .replace("${{ github.server_url }}", "https://github.com")
        .replace("${{ github.run_id }}", "1")
        .replace("${{ github.run_attempt }}", "1")
    )
    path = tmp_path_factory.mktemp("wf") / "gate_tail.sh"
    path.write_text(
        "set -uo pipefail\n"
        'is_trusted() { return 0; }\n'
        'has_label() { printf "%s\\n" "$LABELS" | grep -Fxq "$1"; }\n'
        'pr_number=42; sha=aaaa; author=x\n'
        + body
    )
    return path


@pytest.fixture(scope="module")
def cap_gh(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("capbin")
    gh = d / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$GH_LOG"\n'
        'if [[ "$*" == *"/statuses"* ]]; then\n'
        '  if [ "${STATUSES_FAIL:-0}" = "1" ]; then exit 1; fi\n'
        '  cat "$STATUSES_FILE"; exit 0\n'
        "fi\n"
        'if [[ "$*" == *"-X POST"* && "${LABEL_POST_FAIL:-0}" = "1" ]]; then exit 1; fi\n'
        "exit 0\n"
    )
    gh.chmod(0o755)
    return d


def _gate(script, cap_gh, tmp_path, *, round_, action="synchronize", mode="review",
          labels="", statuses=None, statuses_fail=False, label_post_fail=False):
    (tmp_path / "st.json").write_text(json.dumps(statuses or []))
    out = tmp_path / "out"
    out.write_text("")
    env = {
        **os.environ,
        "PATH": f"{cap_gh}{os.pathsep}{os.environ['PATH']}",
        "round": round_, "mode": mode, "ACTION": action, "LABELS": labels,
        "STATUSES_FILE": str(tmp_path / "st.json"),
        "STATUSES_FAIL": "1" if statuses_fail else "0",
        "LABEL_POST_FAIL": "1" if label_post_fail else "0",
        "GH_LOG": str(tmp_path / "calls.log"), "OUTF": str(out), "GH_TOKEN": "x",
        "GITHUB_STEP_SUMMARY": "/dev/null",
        "GITHUB_RUN_ID": "102", "GITHUB_RUN_ATTEMPT": "2",
    }
    proc = subprocess.run(["bash", "-c", f"round={round_}; mode={mode}; source {script}"],
                          capture_output=True, text=True, env=env)
    calls = (tmp_path / "calls.log").read_text().splitlines() if (tmp_path / "calls.log").exists() else []
    return proc, out.read_text(), calls


def _st(state, desc, ctx="devteam/code-review"):
    return {"context": ctx, "state": state, "description": desc}


def test_unchanged_sha_round_mode_reuses_the_verdict(gate_tail_script, cap_gh, tmp_path):
    _, out, _ = _gate(gate_tail_script, cap_gh, tmp_path, round_="1", action="created",
                      statuses=[_st("success", "2 findings [rk r=1 m=review]")])
    assert "allowed=false" in out and "allowed=true" not in out


def test_verdict_reuse_restores_a_missing_reviewed_label(gate_tail_script, cap_gh, tmp_path):
    _, out, calls = _gate(gate_tail_script, cap_gh, tmp_path, round_="1", action="created",
                          statuses=[_st("success", "1 finding [rk r=1 m=review]")])
    assert "allowed=false" in out
    assert any("-X POST" in c and "labels[]=devteam:reviewed" in c for c in calls), calls


@pytest.mark.parametrize("case", ["round", "mode", "failure", "pending", "eligible", "unstamped", "lookup"])
def test_reuse_never_applies_when_any_key_part_differs_or_lookup_is_unsafe(
    gate_tail_script, cap_gh, tmp_path, case
):
    sts = [_st("success", "x [rk r=1 m=review]")]
    kw = {}
    if case == "round":
        sts = [_st("success", "x [rk r=2 m=review]")]
    elif case == "mode":
        sts = [_st("success", "x [rk r=1 m=improve]")]
    elif case == "failure":
        sts = [_st("failure", "x [rk r=1 m=review]")]
    elif case == "pending":
        sts = [_st("pending", "Dev Team deep review in progress"), _st("success", "x [rk r=1 m=review]")]
    elif case == "eligible":
        sts = [_st("success", "x [rk r=1 m=review]"), _st("success", "cleared", "devteam/merge-eligible")]
    elif case == "unstamped":
        sts = [_st("success", "legacy verdict")]
    elif case == "lookup":
        kw["statuses_fail"] = True
    _, out, _ = _gate(gate_tail_script, cap_gh, tmp_path, round_="1", action="created",
                      statuses=sts, **kw)
    assert "allowed=true" in out, case


def test_reuse_log_names_the_reused_round(gate_tail_script, cap_gh, tmp_path):
    proc, out, _ = _gate(gate_tail_script, cap_gh, tmp_path, round_="1", action="created",
                         statuses=[_st("success", "x [rk r=1 m=review]")])
    assert "allowed=false" in out and "round=1" in proc.stdout, proc.stdout


# --------------------------------------------------------------------------
# per-PR review ceiling (v1.13.0): identity-free, counted from the timeline
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ceil_gh(tmp_path_factory) -> Path:
    """`gh` that emulates the PR timeline: every successful POST of a
    `devteam:review-N` label appends one `labeled` event line to
    $TIMELINE_FILE (as GitHub would, authored by github-actions[bot]);
    label DELETEs never remove events. `--paginate .../timeline` prints one
    id per event, which is what the script's own --jq produces."""
    d = tmp_path_factory.mktemp("ceilbin")
    gh = d / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$GH_LOG"\n'
        'if [[ "$*" == *"/timeline"* ]]; then\n'
        '  if [ "${TIMELINE_FAIL:-0}" = "1" ]; then exit 1; fi\n'
        '  cat "$TIMELINE_FILE"; exit 0\n'
        "fi\n"
        'if [[ "$*" == *"/statuses"* ]]; then echo "[]"; exit 0; fi\n'
        'if [[ "$*" == *"-X POST"* ]]; then\n'
        '  if [ "${LABEL_POST_FAIL:-0}" = "1" ]; then exit 1; fi\n'
        '  if [[ ( "$*" == *"labels[]=devteam:review-"* || "$*" == *"labels[]=review-count:"* || "$*" == *"labels[]=review-refund:"* || "$*" == *"labels[]=review-error:"* || "$*" == *"labels[]=review-ok"* ) && "$*" != *"review-capped"* && "$*" != *"review-exhausted"* ]]; then echo "$*" | sed \'s/.*labels\\[\\]=//\' | awk \'{print $1}\' >> "$TIMELINE_FILE"; fi\n'
        "fi\n"
        "exit 0\n"
    )
    gh.chmod(0o755)
    return d


def _ceil(script, ceil_gh, tmp_path, *, used=0, max_reviews="3", labels="", action="created",
          round_="1", mode="review", timeline_fail=False, label_post_fail=False,
          timeline=None, max_errored="3"):
    tl = tmp_path / "timeline"
    if timeline is not None:
        tl.write_text("".join(f"{n}\n" for n in timeline))
    elif not tl.exists():
        tl.write_text("".join(f"review-count:{i + 1}\n" for i in range(used)))
    out = tmp_path / "out"
    out.write_text("")
    env = {
        **os.environ,
        "PATH": f"{ceil_gh}{os.pathsep}{os.environ['PATH']}",
        "ACTION": action, "LABELS": labels, "MAX_REVIEWS": max_reviews,
        "MAX_ERRORED_REVIEWS": max_errored,
        "TIMELINE_FILE": str(tl), "TIMELINE_FAIL": "1" if timeline_fail else "0",
        "LABEL_POST_FAIL": "1" if label_post_fail else "0",
        "GH_LOG": str(tmp_path / "calls.log"), "OUTF": str(out), "GH_TOKEN": "x",
        "GITHUB_STEP_SUMMARY": "/dev/null",
        "GITHUB_RUN_ID": "102", "GITHUB_RUN_ATTEMPT": "2",
    }
    proc = subprocess.run(["bash", "-c", f"round={round_}; mode={mode}; source {script}"],
                          capture_output=True, text=True, env=env)
    calls = (tmp_path / "calls.log").read_text().splitlines() if (tmp_path / "calls.log").exists() else []
    return proc, dict(l.split("=", 1) for l in out.read_text().splitlines() if "=" in l), calls


def test_under_the_ceiling_dispatches_and_stamps_the_next_label(gate_tail_script, ceil_gh, tmp_path):
    proc, out, calls = _ceil(gate_tail_script, ceil_gh, tmp_path, used=2)
    assert out.get("allowed") == "true" and "capped" not in out, proc.stdout + proc.stderr
    stamp = [i for i, c in enumerate(calls) if "labels[]=review-count:3" in c]
    assert len(stamp) == 1, calls
    # DELETE-then-POST so a pre-seeded label cannot swallow the `labeled` event
    assert any("-X DELETE" in c and "review-count%3A3" in c for c in calls[: stamp[0]]), calls


def test_at_the_ceiling_every_trigger_is_refused_visibly(gate_tail_script, ceil_gh, tmp_path):
    proc, out, calls = _ceil(gate_tail_script, ceil_gh, tmp_path, used=3)
    assert out["allowed"] == "false" and out["capped"] == "true", proc.stdout + proc.stderr
    assert out["first_cap"] == "true" and out["review_count"] == "3" and out["max_reviews"] == "3"
    # the marker is stamped by the report step AFTER delivery, never by the gate
    assert not any("labels[]=devteam:review-exhausted" in c for c in calls)
    assert not any("labels[]=review-count:4" in c for c in calls)


def test_second_refusal_does_not_re_announce(gate_tail_script, ceil_gh, tmp_path):
    _, out, calls = _ceil(gate_tail_script, ceil_gh, tmp_path, used=3,
                          labels="devteam:review-exhausted")
    assert out["capped"] == "true" and out["first_cap"] == "false"
    assert not any("labels[]=devteam:review-exhausted" in c for c in calls)


def test_a_missing_dispatch_key_does_not_charge_the_budget(gate_tail_script, ceil_gh, tmp_path, monkeypatch):
    monkeypatch.setenv("HAS_KEY", "false")
    proc, out, calls = _ceil(gate_tail_script, ceil_gh, tmp_path, used=2)
    assert out["allowed"] == "true" and "capped" not in out
    assert not any("labels[]=review-count:3" in c for c in calls)
    assert "not charging" in proc.stdout + proc.stderr


def test_exhausted_marker_is_stamped_only_after_delivery():
    text = WORKFLOW.read_text()
    step = text[text.index("      - name: Report review ceiling"):text.index("\n  code-review:\n")]
    assert step.index("/comments") < step.index("labels[]=devteam:review-exhausted")
    assert step.index("context=devteam/review-cap") < step.index("labels[]=devteam:review-exhausted")


def test_the_ceiling_escalate_event_does_not_promise_a_nudge():
    # No nudge job exists any more. nudge_follows=true makes the platform only
    # STORE an escalate event, so nobody would ever be told the budget ran out.
    text = WORKFLOW.read_text()
    step = text[text.index("      - name: Report review ceiling"):text.index("\n  code-review:\n")]
    assert '"event": "escalate"' in step
    assert '"nudge_follows": False' in step and '"nudge_follows": True' not in step


@pytest.mark.parametrize("action,round_", [
    ("created", "1"),      # a coder/anyone running /aw-review
    ("synchronize", "1"),
])
def test_no_trigger_and_no_label_state_overrides_the_ceiling(
    gate_tail_script, ceil_gh, tmp_path, action, round_
):
    # labels all removed (what a resetting coder would do): still refused
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, used=7, labels="",
                      action=action, round_=round_)
    assert out["allowed"] == "false" and out["capped"] == "true"


def test_pr15_replay_yields_at_most_max_reviews_dispatches(gate_tail_script, ceil_gh, tmp_path):
    """Replay: 12 triggers (pushes and /aw-review), every devteam label
    stripped between each one. Dispatches must stop at the ceiling."""
    allowed = 0
    for i in range(12):
        action, round_ = ("created", "1") if i % 2 else ("synchronize", "1")
        (tmp_path / "calls.log").write_text("")
        _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, used=0, labels="",
                          action=action, round_=round_)
        allowed += out.get("allowed") == "true"
    assert allowed == 3, allowed


def test_a_hand_added_label_cannot_dodge_the_count(gate_tail_script, ceil_gh, tmp_path):
    """All three labels pre-seeded by a non-bot actor: the timeline holds no
    bot `labeled` events, so the gate still stamps (DELETE first, so GitHub
    emits a fresh event) rather than treating the PR as already counted."""
    seeded = "\n".join(f"devteam:review-{n}" for n in range(1, 4))
    _, out, calls = _ceil(gate_tail_script, ceil_gh, tmp_path, used=0, labels=seeded)
    assert out["allowed"] == "true"
    assert any("-X DELETE" in c and "review-count%3A1" in c for c in calls)
    assert (tmp_path / "timeline").read_text().count("review-count:") == 1


def test_unreadable_timeline_fails_open_loudly(gate_tail_script, ceil_gh, tmp_path):
    proc, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, used=9, timeline_fail=True)
    assert out["allowed"] == "true" and "capped" not in out
    assert "review ceiling not enforced" in proc.stdout + proc.stderr


def test_failed_label_write_fails_open(gate_tail_script, ceil_gh, tmp_path):
    proc, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, used=1, label_post_fail=True)
    assert out["allowed"] == "true"
    assert "reviewing anyway" in proc.stdout + proc.stderr


def test_zero_disables_the_ceiling_and_garbage_falls_back_to_three(gate_tail_script, ceil_gh, tmp_path):
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, used=50, max_reviews="0")
    assert out["allowed"] == "true"
    for bad in ("", "abc", "-1"):
        (tmp_path / "timeline").unlink()
        _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, used=3, max_reviews=bad)
        assert out.get("capped") == "true", bad


def test_the_ceiling_status_is_advisory_to_the_merge_job():
    text = WORKFLOW.read_text()
    adv = text[text.index("ADVISORY = {"):]
    adv = adv[: adv.index("}")]
    assert '"devteam/review-cap"' in adv


def test_report_step_runs_only_on_a_cap_and_marks_the_head():
    text = WORKFLOW.read_text()
    step = text[text.index("      - name: Report review ceiling"):text.index("\n  code-review:\n")]
    assert "steps.decide.outputs.capped == 'true'" in step
    assert "context=devteam/review-cap" in step and "state=failure" in step
    assert "review_budget_exhausted" in step
    assert '"first_cap=' not in step  # announced via the decide output, not recomputed here


@pytest.fixture()
def ceiling_gh(tmp_path):
    """cap_gh plus a PR timeline that reports three spent review-N labels."""
    d = tmp_path / "ceilbin"
    d.mkdir()
    gh = d / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$GH_LOG"\n'
        'if [[ "$*" == *"/timeline"* ]]; then printf "review-count:1\\nreview-count:2\\nreview-count:3\\n"; exit 0; fi\n'
        "exit 0\n"
    )
    gh.chmod(0o755)
    return d


@pytest.mark.parametrize("case", ["ceiling", "untrusted", "reused"])
def test_the_drop_happens_even_when_the_push_is_then_refused(
    gate_tail_script, cap_gh, ceiling_gh, tmp_path, case
):
    """A refused push must not keep a label saying the coder owes a push."""
    kw = {}
    script, gh_dir = gate_tail_script, cap_gh
    if case == "untrusted":
        script = tmp_path / "untrusted.sh"
        script.write_text(
            "allowlist=x\n" + gate_tail_script.read_text().replace("is_trusted() { return 0; }", "is_trusted() { return 1; }")
        )
    elif case == "reused":
        kw["statuses"] = [_st("success", "x [rk r=1 m=review]")]
    else:
        gh_dir = ceiling_gh
    proc, out, calls = _gate(script, gh_dir, tmp_path, round_="1", labels="devteam:awaiting-fixes", **kw)
    assert "allowed=false" in out, proc.stdout + proc.stderr + out
    assert any("-X DELETE" in c and "labels/devteam%3Aawaiting-fixes" in c for c in calls), calls


# --------------------------------------------------------------------------
# devteam-nudge (event-driven PR lifecycle, part B)
# --------------------------------------------------------------------------

NUDGE_WORKFLOW = WORKFLOW.parent / "devteam-nudge-reusable.yml"
_NUDGE_STEP = "- name: Post the PR state to the platform"
_NUDGE_SECRET = "s3cr3t-value"
_OPEN_VIEW = {
    "state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
    "isDraft": False, "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
    "headRefOid": "a" * 40, "url": "https://github.com/o/r/pull/7",
}


@pytest.fixture(scope="module")
def nudge_script(tmp_path_factory) -> Path:
    src = _extract_heredoc(_NUDGE_STEP, NUDGE_WORKFLOW.read_text())
    assert "devteam-pr-nudge" in src, "extracted the wrong block"
    path = tmp_path_factory.mktemp("nudge") / "nudge.py"
    path.write_text(src)
    return path


@pytest.fixture(scope="module")
def nudge_bin(tmp_path_factory) -> Path:
    """Fake `gh` and `curl`. State and logs live under $NUDGE_STATE."""
    d = tmp_path_factory.mktemp("nudgebin")
    (d / "gh").write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$NUDGE_STATE/gh.log"\n'
        'if [[ "$*" == *"/labels"* ]]; then printf "%s\\n" $FAKE_LABELS; exit 0; fi\n'
        'if [[ "$*" == *"/events"* ]]; then cat "$NUDGE_STATE/events" 2>/dev/null; exit 0; fi\n'
        'if [[ "$1 $2" == "pr view" ]]; then\n'
        '  n=$(cat "$NUDGE_STATE/view_n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$NUDGE_STATE/view_n"\n'
        '  f="$NUDGE_STATE/view_$n"; [ -f "$f" ] || f="$NUDGE_STATE/view_last"\n'
        '  cat "$f"; exit 0\n'
        "fi\n"
        "exit 0\n"
    )
    (d / "curl").write_text(
        "#!/bin/bash\n"
        'out=""; data=""; hdr=""; prev=""\n'
        'for a in "$@"; do\n'
        '  [ "$prev" = "-o" ] && out="$a"\n'
        '  [ "$prev" = "--data-binary" ] && data="${a#@}"\n'
        '  [ "$prev" = "-H" ] && [[ "$a" == X-Hub-Signature-256* ]] && hdr="$a"\n'
        '  prev="$a"\n'
        "done\n"
        'n=$(cat "$NUDGE_STATE/curl_n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$NUDGE_STATE/curl_n"\n'
        'cp "$data" "$NUDGE_STATE/posted_$n.json"; echo "$hdr" > "$NUDGE_STATE/sig_$n"\n'
        'printf "%s\\n" "$*" >> "$NUDGE_STATE/curl.log"\n'
        'code=$(sed -n "${n}p" "$NUDGE_STATE/codes" 2>/dev/null); code=${code:-200}\n'
        '[ "$code" = "fail" ] && { echo "curl: (7) refused" >&2; exit 7; }\n'
        'cat "$NUDGE_STATE/response" > "$out" 2>/dev/null\n'
        'printf "%s" "$code"\n'
    )
    for f in ("gh", "curl"):
        (d / f).chmod(0o755)
    return d


def _run_nudge(script, nudge_bin, tmp_path, *, kind="review_done", label="",
               labels=("aw-run:" + "r" * 32,), view=None, views=None, secret=_NUDGE_SECRET,
               codes=(), response='{"verdict": "woken", "reason": "chain idle"}',
               events="", actor="octo", actor_type="User", pr="7", dry_run="false"):
    state = tmp_path / "state"
    state.mkdir()
    for i, v in enumerate(views or [], 1):
        (state / f"view_{i}").write_text(json.dumps(v))
    (state / "view_last").write_text(json.dumps(view if view is not None else _OPEN_VIEW))
    (state / "codes").write_text("\n".join(codes))
    (state / "response").write_text(response)
    (state / "events").write_text(events)
    env = {
        **os.environ,
        "PATH": f"{nudge_bin}:{os.environ['PATH']}",
        "NUDGE_STATE": str(state), "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "NUDGE_SLEEP_SCALE": "0", "FAKE_LABELS": " ".join(labels),
        "REPO": "o/r", "PR_NUMBER": pr, "TRIGGER_KIND": kind, "TRIGGER_LABEL": label,
        "EVENT_URL": "https://ap.example/", "DRY_RUN": dry_run,
        "ACTOR": actor, "ACTOR_TYPE": actor_type, "AW_DEVTEAM_EVENT_SECRET": secret,
    }
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
    posts = sorted(state.glob("posted_*.json"), key=lambda p: int(p.stem.split("_")[1]))
    return proc, state, [json.loads(p.read_text()) for p in posts]


def _comments(state) -> list[str]:
    log = state / "gh.log"
    return [l for l in (log.read_text().splitlines() if log.exists() else [])
            if "/comments" in l and "-X POST" in l]


@pytest.mark.parametrize("kind", ["review_done", "push_unreviewed", "ci_done", "comment"])
def test_nudge_payload_shape_per_trigger_kind(nudge_script, nudge_bin, tmp_path, kind):
    proc, state, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, kind=kind,
                                    actor="alice", actor_type="User")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(posts) == 1
    p = posts[0]
    assert p["v"] == 1
    assert p["trigger"] == {"kind": kind, "actor": "alice", "actor_type": "User", "label": ""}
    assert p["repo"] == "o/r" and p["pr_number"] == 7
    assert p["pr_url"] == "https://github.com/o/r/pull/7"
    assert p["head_sha"] == "a" * 40
    assert p["labels"] == ["aw-run:" + "r" * 32]
    assert p["run_id"] == "r" * 32
    assert p["view"] == _OPEN_VIEW and p["needs_human_actor"] is None
    assert p["dry_run"] is False
    assert "/api/github/devteam-pr-nudge" in (state / "curl.log").read_text()


def test_nudge_label_trigger_carries_the_label_and_dry_run(nudge_script, nudge_bin, tmp_path):
    labels = ("aw-run:" + "r" * 32, "devteam:awaiting-fixes")
    proc, _, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, kind="label",
                                label="devteam:awaiting-fixes", labels=labels, dry_run="true")
    assert proc.returncode == 0 and len(posts) == 1, proc.stdout + proc.stderr
    assert posts[0]["trigger"]["label"] == "devteam:awaiting-fixes"
    assert posts[0]["dry_run"] is True


def test_nudge_run_id_is_empty_with_only_a_card_label(nudge_script, nudge_bin, tmp_path):
    _, _, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, labels=("aw-card:abc",))
    assert posts[0]["run_id"] == ""


@pytest.mark.parametrize("case", ["closed", "merged", "no_agent_label", "label_wrong",
                                  "label_gone", "no_pr", "no_secret"])
def test_nudge_skips_quietly_and_never_posts(nudge_script, nudge_bin, tmp_path, case):
    kw = {}
    if case == "closed":
        kw["view"] = {**_OPEN_VIEW, "state": "CLOSED"}
    elif case == "merged":
        kw["view"] = {**_OPEN_VIEW, "state": "MERGED"}
    elif case == "no_agent_label":
        kw["labels"] = ("bug",)
    elif case == "label_wrong":
        kw.update(kind="label", label="devteam:needs-human",
                  labels=("aw-run:" + "r" * 32, "devteam:needs-human"))
    elif case == "label_gone":
        kw.update(kind="label", label="devteam:awaiting-fixes")  # not in the live set
    elif case == "no_pr":
        kw["pr"] = ""
    elif case == "no_secret":
        kw["secret"] = ""
    proc, state, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, **kw)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert posts == [] and _comments(state) == []
    if case == "no_secret":
        assert "::warning::" in proc.stdout


def test_nudge_rereads_the_view_while_mergeable_is_unknown(nudge_script, nudge_bin, tmp_path):
    unknown = {**_OPEN_VIEW, "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
    proc, state, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, views=[unknown, unknown])
    assert proc.returncode == 0 and len(posts) == 1
    assert posts[0]["view"]["mergeable"] == "MERGEABLE"
    assert (state / "view_n").read_text().strip() == "3"


def test_nudge_gives_up_on_unknown_after_three_reads_and_still_posts(nudge_script, nudge_bin, tmp_path):
    unknown = {**_OPEN_VIEW, "mergeable": "UNKNOWN"}
    proc, state, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, view=unknown)
    assert proc.returncode == 0 and len(posts) == 1
    assert (state / "view_n").read_text().strip() == "3"
    assert posts[0]["view"]["mergeable"] == "UNKNOWN"


def test_nudge_signature_is_over_the_exact_posted_bytes(nudge_script, nudge_bin, tmp_path):
    import hashlib, hmac
    proc, state, _ = _run_nudge(nudge_script, nudge_bin, tmp_path)
    raw = (state / "posted_1.json").read_bytes()
    want = hmac.new(_NUDGE_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert (state / "sig_1").read_text().strip() == f"X-Hub-Signature-256: sha256={want}"
    assert _NUDGE_SECRET not in (state / "curl.log").read_text()  # never in argv
    assert _NUDGE_SECRET not in proc.stdout + proc.stderr


def test_nudge_404_is_a_warning_and_exit_zero(nudge_script, nudge_bin, tmp_path):
    proc, state, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, codes=["404"],
                                    response='{"detail": "Not Found"}')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(posts) == 1, "404 is final, no retry"
    assert "::warning::" in proc.stdout and "not deployed" in proc.stdout


def test_nudge_retries_5xx_and_transport_errors_then_succeeds(nudge_script, nudge_bin, tmp_path):
    proc, state, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, codes=["fail", "502", "200"])
    assert proc.returncode == 0 and len(posts) == 3
    assert posts[0] == posts[2]
    assert "woken" in (tmp_path / "summary.md").read_text()


def test_nudge_persistent_5xx_gives_up_after_three_and_exits_zero(nudge_script, nudge_bin, tmp_path):
    proc, _, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, codes=["503", "503", "503", "200"])
    assert proc.returncode == 0 and len(posts) == 3
    assert "::warning::" in proc.stdout


@pytest.mark.parametrize("code", ["400", "401", "422"])
def test_nudge_other_non_2xx_is_a_warning_without_retry(nudge_script, nudge_bin, tmp_path, code):
    proc, _, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, codes=[code])
    assert proc.returncode == 0 and len(posts) == 1
    assert f"HTTP {code}" in proc.stdout and "::warning::" in proc.stdout


@pytest.mark.parametrize("kind,verdict,comments", [
    ("label", "active", 1), ("comment", "recent", 1), ("label", "woken", 0),
    ("review_done", "active", 0), ("ci_done", "waiting_ci", 0), ("push_unreviewed", "recent", 0),
])
def test_nudge_comments_only_for_human_triggers_that_did_not_wake(
        nudge_script, nudge_bin, tmp_path, kind, verdict, comments):
    kw = {}
    if kind == "label":
        kw.update(label="devteam:awaiting-fixes",
                  labels=("aw-run:" + "r" * 32, "devteam:awaiting-fixes"))
    proc, state, _ = _run_nudge(
        nudge_script, nudge_bin, tmp_path, kind=kind,
        response=json.dumps({"verdict": verdict, "reason": "why not"}), **kw)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(_comments(state)) == comments
    if comments:
        assert verdict in _comments(state)[0] and "why not" in _comments(state)[0]
    assert verdict in (tmp_path / "summary.md").read_text()


def test_nudge_script_never_raises_even_on_garbage(nudge_script, nudge_bin, tmp_path):
    proc, _, _ = _run_nudge(nudge_script, nudge_bin, tmp_path, response="<html>oops</html>")
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- structure of the two workflows ---------------------------------------


def _job(text: str, name: str) -> str:
    start = text.index(f"\n  {name}:\n")
    rest = text[start + 1:]
    m = re.search(r"\n  [a-z][a-z-]*:\n", rest[1:])
    return rest if not m else rest[:m.start() + 1]


def test_nudge_workflow_is_continue_on_error_and_bounded_with_no_checkout():
    text = NUDGE_WORKFLOW.read_text()
    code = _code_only(text)
    assert "timeout-minutes: 5" in code
    assert code.count("continue-on-error: true") >= 2  # job and step
    assert "actions/checkout" not in code, "pull_request_target + secret: never check out PR code"
    assert "secrets.AW_DEVTEAM_EVENT_SECRET" in code
    assert "event_url" in code and "https://agents-platform.aw.tekflox.com" in code


def test_merge_attempt_exposes_the_pr_it_resolves_and_the_nudge_is_advisory():
    text = WORKFLOW.read_text()
    assert "pr_number: ${{ steps.merge.outputs.pr_number }}" in _job(text, "merge-attempt")
    src = _extract_heredoc(_MERGE_STEP)
    assert 'pr_number={pr_number}' in src and "GITHUB_OUTPUT" in src
    advisory = src[src.index("ADVISORY = {"):src.index("}", src.index("ADVISORY = {"))]
    assert '"nudge"' in advisory and '"devteam-nudge"' in advisory


def test_the_reference_label_caller_matches_the_design():
    caller = (WORKFLOW.parent / "devteam-nudge.yml").read_text()
    code = _code_only(caller)
    assert "pull_request_target:" in code and "types: [labeled]" in code
    # label name in the key: an unrelated label must not evict a pending nudge
    assert ("group: devteam-nudge-${{ github.event.pull_request.number }}"
            "-${{ github.event.label.name }}") in code
    assert "cancel-in-progress: false" in code
    assert "trigger_kind: label" in code
    assert "devteam-nudge-reusable.yml@master" in code
    assert "actions/checkout" not in code
    assert "github.event.label.name == 'devteam:awaiting-fixes'" in code


def test_legacy_review_n_events_carry_forward_without_double_charge(gate_tail_script, ceil_gh, tmp_path):
    """Two reviews were stamped as `devteam:review-N` before the rename: the
    timeline has 2 events. The next dispatch is the 3rd (`review-count:3`),
    and the one after that is refused: neither lost nor charged twice."""
    proc, out, calls = _ceil(gate_tail_script, ceil_gh, tmp_path, used=2)
    assert out.get("allowed") == "true", proc.stdout + proc.stderr
    assert any("labels[]=review-count:3" in c for c in calls)
    assert not any("labels[]=devteam:review-" in c and "capped" not in c and "exhausted" not in c for c in calls)
    proc, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, used=2)  # timeline now has 3
    assert out.get("allowed") == "false" and out.get("capped") == "true", proc.stdout + proc.stderr


def test_ceiling_jq_counts_both_label_families():
    src = (Path(__file__).resolve().parent.parent / ".github/workflows/code-review-reusable.yml").read_text()
    assert 'test("^((devteam:review-|review-count:|review-error:)[0-9]+(-[0-9]+)?|review-refund:[0-9]+(:[0-9]+(-[0-9]+)?)?|review-ok)$")' in src

# --------------------------------------------------------------------------
# QA rework (card 41e5bf3b): escalation delivery is tracked apart from the
# comment.
# --------------------------------------------------------------------------

def test_gate_reports_whether_the_escalation_was_already_delivered(gate_tail_script, ceil_gh, tmp_path):
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, used=3)
    assert out["escalated"] == "false"
    (tmp_path / "second").mkdir()
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path / "second", used=3,
                      labels="devteam:review-exhausted\ndevteam:review-escalated")
    assert out["escalated"] == "true"


@pytest.fixture(scope="module")
def report_script(tmp_path_factory) -> Path:
    text = WORKFLOW.read_text()
    i = text.index("      - name: Report review ceiling")
    seg = text[i:text.index("\n  code-review:\n")]
    body = seg[seg.index("        run: |\n") + len("        run: |\n"):]
    path = tmp_path_factory.mktemp("rep") / "report.sh"
    path.write_text(textwrap.dedent(body))
    return path


def _report(script, tmp_path, *, first="true", escalated="false", http="200", secret="s", rc=0):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "gh").write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$GH_LOG"\nexit 0\n')
    (d / "curl").write_text(
        '#!/bin/bash\nprintf "curl\\n" >> "$GH_LOG"\n'
        'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; echo "{}" > "$1"; }; shift; done\n'
        f'printf "{http}"\nexit {rc}\n'
    )
    for f in ("gh", "curl"):
        (d / f).chmod(0o755)
    env = {
        **os.environ, "PATH": f"{d}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(tmp_path / "calls.log"), "RUNNER_TEMP": str(tmp_path),
        "AW_DEVTEAM_EVENT_SECRET": secret, "EVENT_URL": "http://ev.invalid", "REPO": "o/r",
        "PR_NUMBER": "7", "HEAD_SHA": "abcdef1234", "ROUND": "1", "USED": "3",
        "MAX": "3", "FIRST": first, "ESCALATED": escalated, "PR_URL": "u", "RUN_URL": "r",
    }
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    calls = (tmp_path / "calls.log").read_text().splitlines()
    return proc, calls


def test_the_report_step_never_stamps_escalated_the_webhook_only_stores(report_script, tmp_path):
    # AP-MT answers 200 stored:true acted:false: not a human delivery.
    proc, calls = _report(report_script, tmp_path, http="200")
    assert "curl" in calls
    assert not any("review-escalated" in c for c in calls), calls


def test_a_failed_webhook_is_retried_next_run(report_script, tmp_path):
    proc, calls = _report(report_script, tmp_path, http="503")
    assert "HTTP 503" in proc.stdout and "will retry" in proc.stdout
    assert any("labels[]=devteam:review-exhausted" in c for c in calls)  # comment/status done
    (tmp_path / "n").mkdir()
    proc, calls = _report(report_script, tmp_path / "n", first="false", escalated="false", http="200")
    assert "curl" in calls and not any("/comments" in c for c in calls), calls


def test_a_delivered_escalation_is_never_resent(report_script, tmp_path):
    proc, calls = _report(report_script, tmp_path, first="false", escalated="true")
    assert "curl" not in calls and not any("/comments" in c for c in calls), calls
    assert "already delivered" in proc.stdout


def test_a_2xx_with_an_incomplete_transfer_is_not_marked_delivered(report_script, tmp_path):
    proc, calls = _report(report_script, tmp_path, http="200", rc=18)
    assert "curl" in calls
    assert not any("review-escalated" in c for c in calls), calls
    assert "will retry" in proc.stdout


# ── the nudge stamps `review-escalated` only after the human was asked ──────

def _stamped(state) -> bool:
    log = state / "gh.log"
    return log.exists() and "labels[]=devteam:review-escalated" in log.read_text()


_EXH = ("aw-run:" + "r" * 32, "devteam:review-exhausted")


def test_nudge_stamps_escalated_when_the_question_was_asked(nudge_script, nudge_bin, tmp_path):
    _, state, _ = _run_nudge(nudge_script, nudge_bin, tmp_path, labels=_EXH,
                             response='{"verdict": "asked", "acted": true, "reason": "review ceiling exhausted"}')
    assert _stamped(state)


@pytest.mark.parametrize("resp", [
    {"verdict": "ask_failed", "acted": False},
    {"verdict": "ask_deferred", "acted": False},
    {"verdict": "ask_unroutable", "acted": False},
    {"verdict": "woken", "acted": True},
    # AP-MT: simulated ASK (dry run) and an already-asked replay are verdict=asked, acted=false
    {"verdict": "asked", "acted": False, "result": {"dry_run": True}},
    {"verdict": "asked", "acted": False, "result": {"already_asked": True}},
    {"verdict": "asked"},
])
def test_nudge_does_not_stamp_escalated_when_nothing_was_asked(nudge_script, nudge_bin, tmp_path, resp):
    _, state, _ = _run_nudge(nudge_script, nudge_bin, tmp_path, labels=_EXH,
                             response=json.dumps(resp))
    assert not _stamped(state)


def test_nudge_does_not_stamp_escalated_without_the_exhausted_label(nudge_script, nudge_bin, tmp_path):
    _, state, _ = _run_nudge(nudge_script, nudge_bin, tmp_path,
                             response='{"verdict": "asked", "acted": true}')
    assert not _stamped(state)


# --------------------------------------------------------------------------
# lifecycle v2 (Kanban 41f5bf3b): one blocking review, later pushes merge
# --------------------------------------------------------------------------

def _run_eligibility_v2(script, tmp_path, fake_gh, findings, monkeypatch, round_="1", is_draft="false"):
    counts: dict = {}
    for f in findings:
        sev = str(f.get("severity", "info")).lower()
        counts[sev] = counts.get(sev, 0) + 1
    monkeypatch.setenv("BLOCK_SEVERITY", "medium")
    return _run_eligibility(script, tmp_path, fake_gh, findings=findings, counts=counts,
                            round_=round_, is_draft=is_draft)


def test_v2_review1_blocks_on_medium_and_above(eligibility_script, tmp_path, fake_gh, monkeypatch):
    got = _run_eligibility_v2(eligibility_script, tmp_path, fake_gh, [MEDIUM_BLOCKING], monkeypatch)
    assert got["eligible"] == "false" and got["blocking_count"] == "1"
    assert not any("devteam/merge-eligible" in c for c in _gh_calls(tmp_path))


@pytest.mark.parametrize("sev", ["high", "critical"])
def test_v2_review1_blocks_on_high_and_critical(eligibility_script, tmp_path, fake_gh, monkeypatch, sev):
    got = _run_eligibility_v2(eligibility_script, tmp_path, fake_gh,
                              [{**MINOR, "severity": sev}], monkeypatch)
    assert got["eligible"] == "false"


def test_v2_review1_low_and_info_findings_do_not_block(eligibility_script, tmp_path, fake_gh, monkeypatch):
    got = _run_eligibility_v2(eligibility_script, tmp_path, fake_gh, [MINOR, CROSS_CUTTING], monkeypatch)
    assert got["eligible"] == "true" and got["blocking_count"] == "0"
    assert got["findings_count"] == "2"
    assert any("devteam/merge-eligible" in c for c in _gh_calls(tmp_path))


def test_v2_review1_missing_or_unknown_severity_fails_closed(eligibility_script, tmp_path, fake_gh, monkeypatch):
    for sev in (None, "", "urgent"):
        f = {"title": "x", "body": "x", **({} if sev is None else {"severity": sev})}
        # the verdict validator lowercases a missing severity to "info"
        # for the tally, so give it a consistent counts object
        monkeypatch.setenv("BLOCK_SEVERITY", "medium")
        got = _run_eligibility(eligibility_script, tmp_path, fake_gh, findings=[f],
                               counts={str(f.get("severity", "info")).lower(): 1}, round_="1")
        if sev is None:
            # absent severity is tallied as "info" but the finding itself has none: blocking
            assert got["eligible"] == "false"
        else:
            assert got["eligible"] == "false", sev


def test_v2_a_draft_is_still_never_eligible(eligibility_script, tmp_path, fake_gh, monkeypatch):
    got = _run_eligibility_v2(eligibility_script, tmp_path, fake_gh, [], monkeypatch, is_draft="true")
    assert got["eligible"] == "false"


@pytest.mark.parametrize("findings,blocking", [(0, 0), (1, 0), (2, 1)])
def test_v2_every_completed_review1_writes_reviewed_and_nothing_else(
        labels_script, fake_gh, tmp_path, findings, blocking):
    out, calls = _run_labels(labels_script, fake_gh, tmp_path, round_="1",
                            findings_count=findings, blocking_count=blocking)
    posts = [c for c in calls if "-X POST" in c]
    assert len(posts) == 1 and "labels[]=devteam:reviewed" in posts[0], posts
    assert not any("awaiting-fixes" in c or "needs-human" in c for c in calls)


def test_v2_no_reviewed_label_when_the_round1_status_publish_failed(labels_script, fake_gh, tmp_path):
    os.environ["PUBLISH_OUTCOME"] = "failure"
    try:
        out, calls = _run_labels(labels_script, fake_gh, tmp_path, round_="1", findings_count=1, blocking_count=1)
    finally:
        del os.environ["PUBLISH_OUTCOME"]
    assert not [c for c in calls if "-X POST" in c], calls
    assert "labelled: devteam:reviewed" not in out


def test_labels_and_event_steps_run_on_review_1_only():
    """The label write and the platform event are gated on round 1 in the step
    `if:` (bypass and informational runs write nothing)."""
    code = _code_only(WORKFLOW.read_text())
    for name in ("- name: Set round labels", "- name: Report round outcome to the platform"):
        step = code[code.index(name):]
        cond = step[:step.index("env:")]
        assert "needs.gate.outputs.round == '1'" in cond, name
        assert "'2'" not in cond and "needs-human" not in cond, name


@pytest.fixture(scope="module")
def bypass_script(tmp_path_factory) -> Path:
    """The gate's v2 round derivation + bypass, extracted from `Decide`
    (round fragment through the reuse-verdict marker, minus the trusted check
    and the caps, which have their own tests)."""
    text = WORKFLOW.read_text()
    a = text.index("# ---- round, from the PR's LIVE labels ----")
    b = text.index("          # Round memory (see the DECIDE note above this step).")
    c = text.index("          # Bypass (see the DECIDE note above this step).")
    d = text.index("          # ---- (2) reuse the verdict")
    labels_fetch = text[a:b]
    reviewed = text[b:text.index("          if [ \"$mode\" = \"improve\" ]; then", b)]
    chain_start = text.index("          if [ \"$mode\" = \"improve\" ]; then", b)
    chain_end = text.index('          echo "round=$round', chain_start)
    chain = text[chain_start:chain_end]
    body = (labels_fetch + reviewed + chain + text[c:d])
    for k, v in {
        "${{ github.repository }}": "o/r", "${{ github.event_name }}": "${EVENT_NAME}",
        "${{ github.server_url }}": "https://github.com", "${{ github.run_id }}": "9",
        "${{ github.run_attempt }}": "1",
    }.items():
        body = body.replace(k, v)
    body = body.replace('"$GITHUB_OUTPUT"', '"$OUT"')
    path = tmp_path_factory.mktemp("wf") / "bypass.sh"
    path.write_text('set -euo pipefail\npr_number=42\nsha=deadbeef\nmode="${MODE_IN}"\n'
                    'is_draft="${DRAFT:-false}"\n' + body)
    return path


@pytest.fixture
def v2_gh(tmp_path):
    """fake gh: timeline -> N lines (FAKE_TIMELINE), `fail` -> exit 1;
    everything else is logged to GH_LOG."""
    bin_dir = tmp_path / "bin2"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$GH_LOG"\n'
        'if [[ "$*" == *"/timeline"* ]]; then\n'
        '  [ "${FAKE_TIMELINE:-0}" = "fail" ] && { echo boom >&2; exit 1; }\n'
        '  n=${FAKE_TIMELINE:-0}; for i in $(seq 1 $n); do echo devteam:reviewed; done; exit 0\n'
        "fi\n"
        'if [[ "$*" == *"/commits/"* ]]; then\n'
        '  [ "${FAKE_VERDICT:-0}" = "fail" ] && { echo boom >&2; exit 1; }\n'
        '  n=${FAKE_VERDICT:-0}; for i in $(seq 1 $n); do echo success; done; exit 0\n'
        "fi\n"
        'if [[ "$*" == *"/labels"* ]]; then exit 0; fi\n'
        "exit 0\n")
    gh.chmod(0o755)
    return bin_dir


def _run_bypass(script, v2_gh, tmp_path, *, timeline="0", event="pull_request", mode="review",
                draft="false", verdict="0"):
    out = tmp_path / "out"
    out.write_text("")
    log = tmp_path / "ghlog"
    env = {**os.environ, "PATH": f"{v2_gh}{os.pathsep}{os.environ['PATH']}",
           "GH_LOG": str(log), "FAKE_TIMELINE": timeline, "FAKE_VERDICT": verdict, "EVENT_NAME": event, "MODE_IN": mode,
           "DRAFT": draft, "OUT": str(out), "GH_TOKEN": "x"}
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    got = dict(l.split("=", 1) for l in out.read_text().splitlines() if "=" in l)
    return proc, got, (log.read_text().splitlines() if log.exists() else [])


def test_v2_a_push_after_a_completed_review1_is_bypassed_with_matching_statuses(
        bypass_script, v2_gh, tmp_path):
    proc, got, calls = _run_bypass(bypass_script, v2_gh, tmp_path, timeline="1")
    assert proc.returncode == 0, proc.stderr
    assert got.get("bypass") == "true" and got.get("allowed") == "false"
    posts = [c for c in calls if "/statuses/" in c]
    assert any("context=devteam/code-review" in c and "state=success" in c for c in posts), posts
    assert any("context=devteam/merge-eligible" in c and "state=success" in c for c in posts), posts
    urls = {re.search(r"target_url=(\S+)", c).group(1) for c in posts}
    assert len(urls) == 1, "merge-attempt needs ONE shared attempt URL on both statuses"


def test_v2_reopen_on_a_head_that_already_has_a_round1_verdict_is_refused(bypass_script, v2_gh, tmp_path):
    """Same SHA replayed (close+reopen / ready_for_review): the blocking review stands."""
    proc, got, calls = _run_bypass(bypass_script, v2_gh, tmp_path, timeline="1", verdict="1")
    assert proc.returncode == 0, proc.stderr
    assert got.get("bypass") != "true" and got.get("allowed") == "false"
    assert not [c for c in calls if "/statuses/" in c and "-X POST" in c]


def test_v2_an_unreadable_status_list_fails_closed_no_clearance(bypass_script, v2_gh, tmp_path):
    proc, got, calls = _run_bypass(bypass_script, v2_gh, tmp_path, timeline="1", verdict="fail")
    assert got.get("bypass") != "true"
    assert not [c for c in calls if "-X POST" in c and "/statuses/" in c]


def test_v2_without_a_completed_review1_the_push_is_reviewed_not_bypassed(bypass_script, v2_gh, tmp_path):
    proc, got, calls = _run_bypass(bypass_script, v2_gh, tmp_path, timeline="0")
    assert proc.returncode == 0, proc.stderr
    assert got.get("bypass") != "true"
    assert not [c for c in calls if "/statuses/" in c]


def test_v2_a_draft_gets_no_clearance(bypass_script, v2_gh, tmp_path):
    proc, got, calls = _run_bypass(bypass_script, v2_gh, tmp_path, timeline="1", draft="true")
    assert proc.returncode == 0 and got.get("bypass") != "true"
    assert not [c for c in calls if "/statuses/" in c]


def test_v2_manual_aw_review_after_review1_never_grants_eligibility(bypass_script, v2_gh, tmp_path):
    proc, got, calls = _run_bypass(bypass_script, v2_gh, tmp_path, timeline="1", event="issue_comment")
    assert proc.returncode == 0 and got.get("bypass") != "true"
    assert not [c for c in calls if "/statuses/" in c]


def test_v2_an_unreadable_timeline_fails_closed(bypass_script, v2_gh, tmp_path):
    proc, got, calls = _run_bypass(bypass_script, v2_gh, tmp_path, timeline="fail")
    assert proc.returncode != 0, "must not guess: no review, no bypass"
    assert not [c for c in calls if "/statuses/" in c]


def test_no_run_script_exceeds_githubs_21000_char_limit():
    """GitHub rejects a `run:` over 21,000 characters as a bare 'workflow file
    issue' (no job, no log): lifecycle v2's first cut put `Decide` at 21,496
    and every caller on @master went red at once. Keep headroom."""
    import yaml
    for path in WORKFLOW.parent.glob("*.yml"):
        doc = yaml.safe_load(path.read_text())
        for jname, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                assert len(step.get("run", "")) <= 20500, (path.name, jname, step.get("name"), len(step["run"]))


# ---- every PR label read must paginate (GitHub returns 30 per page) ----

def test_every_label_read_paginates():
    """The aw-run: label can sit on page 2; an unpaginated read yields an
    empty run_id and a partial labels list."""
    text = WORKFLOW.read_text()
    # A label READ is a /labels call that is followed by --jq (writes use -X/-f).
    reads = [m.start() for m in re.finditer(r"/labels[\"']", text)
             if "--jq" in text[m.start():m.start() + 120]
             and "--silent" not in text[m.start():m.start() + 120].split("--jq")[0]
             and "-f" not in text[m.start():m.start() + 120].split("--jq")[0].split()]
    assert len(reads) >= 5, reads
    for pos in reads:
        window = text[pos:pos + 120]
        assert window.index("--paginate") < window.index("--jq"), text[pos - 40:pos + 120]


# --------------------------------------------------------------------------
# ONE lifecycle (no opt-in): the `lifecycle` input is deprecated and ignored,
# `devteam:needs-human` is never written or read and a stray one is deleted.
# --------------------------------------------------------------------------


@pytest.fixture
def timeline_gh(tmp_path):
    """fake gh: `/labels` -> $FAKE_LABELS, `/timeline` -> N `devteam:reviewed`
    lines (the bot-written label events), everything else logged and exit 0."""
    bin_dir = tmp_path / "tlbin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "${GH_LOG:-/dev/null}"\n'
        'if [[ "$*" == *"/timeline"* ]]; then\n'
        '  n=${FAKE_TIMELINE:-0}; for i in $(seq 1 $n); do echo devteam:reviewed; done; exit 0\n'
        "fi\n"
        'if [[ "$*" == *"/labels"* && "$*" != *"-X "* ]]; then printf "%s\\n" $FAKE_LABELS; exit 0; fi\n'
        "exit 0\n"
    )
    gh.chmod(0o755)
    return bin_dir


def _round(script, gh_dir, *, labels="", timeline="0", event="pull_request", mode="review",
           extra_env=None):
    env = {
        **os.environ, **(extra_env or {}),
        "PATH": f"{gh_dir}{os.pathsep}{os.environ['PATH']}",
        "MODE_IN": mode, "FAKE_LABELS": labels, "FAKE_TIMELINE": timeline,
        "EVENT_NAME": event, "EVENT_ACTION": "synchronize", "GH_TOKEN": "x",
    }
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    got = [l.split("=", 1)[1] for l in proc.stdout.splitlines() if l.startswith("FINAL_ROUND=")]
    return proc.returncode, (got[0] if got else None)


@pytest.mark.parametrize("labels,timeline,event,mode,expected", [
    ("", "0", "pull_request", "review", "1"),
    ("bug", "0", "pull_request", "review", "1"),
    ("devteam:awaiting-fixes", "0", "pull_request", "review", "1"),
    # a stray needs-human is not state any more
    ("devteam:needs-human", "0", "pull_request", "review", "1"),
    ("devteam:needs-human", "1", "pull_request", "review", "bypass"),
    # hand-added round memory is not memory: only the bot's timeline event is
    ("devteam:fix-cycle", "0", "pull_request", "review", "1"),
    ("devteam:reviewed", "0", "pull_request", "review", "1"),
    ("", "1", "pull_request", "review", "bypass"),
    ("devteam:awaiting-fixes devteam:needs-human", "2", "pull_request", "review", "bypass"),
    ("", "1", "issue_comment", "review", "informational"),
    ("", "1", "issue_comment", "improve", "informational"),
    ("", "0", "issue_comment", "improve", "informational"),
])
def test_round_comes_only_from_the_bot_written_review_memory(
    round_script, timeline_gh, labels, timeline, event, mode, expected
):
    rc, round_ = _round(round_script, timeline_gh, labels=labels, timeline=timeline,
                        event=event, mode=mode)
    assert rc == 0 and round_ == expected


@pytest.mark.parametrize("value", ["", "v1", "v2", "garbage"])
def test_the_deprecated_lifecycle_input_is_ignored_by_the_gate(round_script, timeline_gh, value):
    """Whatever a caller still passes, the lifecycle is the same one."""
    for tl, expected in (("0", "1"), ("1", "bypass")):
        rc, round_ = _round(round_script, timeline_gh, timeline=tl,
                            extra_env={"LIFECYCLE": value})
        assert rc == 0 and round_ == expected, (value, tl)


def test_the_lifecycle_input_is_declared_but_deprecated_and_never_read():
    import yaml
    doc = yaml.safe_load(WORKFLOW.read_text())
    spec = doc[True]["workflow_call"]["inputs"]["lifecycle"]  # PyYAML parses `on` as True
    assert spec["type"] == "string" and not spec.get("required")
    assert "DEPRECATED" in spec["description"] and "IGNORED" in spec["description"]
    # Declared so `with: lifecycle: v2` is not an invalid-input error; but no
    # step, `if:` or env may branch on it: that would be a second lifecycle.
    code = _code_only(WORKFLOW.read_text())
    assert "LIFECYCLE" not in code
    assert len(re.findall(r"inputs\.lifecycle", code)) == 0
    assert "lifecycle" in code  # the declaration itself


def test_there_is_no_nudge_job_and_nothing_writes_needs_human():
    text = WORKFLOW.read_text()
    code = _code_only(text)
    assert not re.search(r"^  nudge:", text, re.M)
    assert "devteam-nudge-reusable.yml" not in code
    assert "labels[]=devteam:needs-human" not in code
    assert 'add_label "devteam:needs-human"' not in code
    assert '"needs-human"' not in code and "round == 'needs-human'" not in code
    assert "review-capped" not in code


@pytest.fixture(scope="module")
def stray_script(tmp_path_factory) -> Path:
    text = WORKFLOW.read_text()
    start = text.index("          # ---- a stray `devteam:needs-human` is ignored and deleted ----")
    end = text.index("          # ---- a push means the coder paid its debt")
    body = text[start:end].replace("${{ github.repository }}", "tekflox/aw-app-template")
    path = tmp_path_factory.mktemp("wf") / "stray.sh"
    path.write_text(
        'set -euo pipefail\npr_number=42\nlabels="$FAKE_LABELS"\n'
        'has_label() { printf "%s\\n" "$labels" | grep -Fxq "$1"; }\n' + body
    )
    return path


def _stray(script, gh_dir, labels, tmp_path):
    log = tmp_path / "gh.log"
    env = {**os.environ, "PATH": f"{gh_dir}{os.pathsep}{os.environ['PATH']}",
           "FAKE_LABELS": labels, "GH_LOG": str(log)}
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, [c for c in calls if "-X DELETE" in c]


def test_a_stray_needs_human_label_is_deleted(stray_script, timeline_gh, tmp_path):
    proc, deletes = _stray(stray_script, timeline_gh, "bug\ndevteam:needs-human", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(deletes) == 1 and "labels/devteam%3Aneeds-human" in deletes[0], deletes


@pytest.mark.parametrize("labels", ["", "bug", "devteam:awaiting-fixes", "devteam:reviewed",
                                    "devteam:needs-human-but-not-really"])
def test_nothing_is_deleted_when_there_is_no_stray_needs_human(stray_script, timeline_gh, tmp_path, labels):
    proc, deletes = _stray(stray_script, timeline_gh, labels, tmp_path)
    assert proc.returncode == 0 and deletes == []


def test_a_failed_stray_delete_is_a_warning_not_a_failure(stray_script, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/bash\nif [[ "$*" == *"-X DELETE"* ]]; then exit 1; fi\nexit 0\n')
    gh.chmod(0o755)
    proc, _ = _stray(stray_script, bin_dir, "devteam:needs-human", tmp_path)
    assert proc.returncode == 0
    assert "::warning::could not remove stray devteam:needs-human" in proc.stdout + proc.stderr


@pytest.mark.parametrize("labels", ["", "bug", "devteam:awaiting-fixes", "devteam:needs-human",
                                    "devteam:review-capped", "devteam:fix-cycle"])
def test_synchronize_is_never_refused_by_label_state(synchronize_script, fake_gh, tmp_path, labels):
    """No v1 round-1 synchronize guard: a push is either reviewed (review 1)
    or cleared (bypass), never silently skipped for lack of a label."""
    got = _decide_allowed(synchronize_script, fake_gh, tmp_path, labels=labels, action="synchronize")
    assert got["allowed"] == "true", got["_log"]


def test_a_stray_review_capped_label_is_ignored(gate_tail_script, cap_gh, tmp_path):
    """The v1 cap stamp is dead: neither read, written nor cleared."""
    _, out, calls = _gate(gate_tail_script, cap_gh, tmp_path, round_="1", action="synchronize",
                          labels="devteam:review-capped")
    assert "allowed=true" in out
    assert not any("review-capped" in c for c in calls if "-X " in c), calls


@pytest.mark.parametrize("kwargs", [
    {"findings": [MEDIUM_BLOCKING], "counts": {"medium": 1}, "round_": "1"},
    {"findings": [], "counts": {}, "round_": "informational"},
    {"findings": [], "counts": {}, "round_": "1", "is_draft": "true"},
], ids=["round1-blocking", "informational", "draft"])
def test_the_eligible_status_is_never_published_when_not_eligible(
    eligibility_script, tmp_path, fake_gh, kwargs
):
    got = _run_eligibility(eligibility_script, tmp_path, fake_gh, **kwargs)
    assert got["eligible"] == "false"
    assert _status_posts(tmp_path) == []


def test_round1_blocks_on_block_severity_not_on_blocks_merge(eligibility_script, tmp_path, fake_gh):
    """`blocks_merge` is a retired round-2 field: severity is the only gate."""
    low = _run_eligibility(eligibility_script, tmp_path, fake_gh,
                           findings=[{**MINOR, "blocks_merge": True}], counts={"low": 1}, round_="1")
    assert low["eligible"] == "true" and low["blocking_count"] == "0"
    (tmp_path / "second").mkdir()
    high = _run_eligibility(eligibility_script, tmp_path / "second", fake_gh,
                            findings=[{**HIGH_NON_BLOCKING}], counts={"high": 1}, round_="1")
    assert high["eligible"] == "false" and high["blocking_count"] == "1"


def test_a_failed_reviewed_write_fails_the_step(labels_script, tmp_path):
    bin_dir = _failing_gh(tmp_path, "labels[]=devteam:reviewed")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "ROUND": "1",
           "GH_TOKEN": "x", "GH_LOG": str(tmp_path / "l"), "GITHUB_OUTPUT": "/dev/null"}
    proc = subprocess.run(["bash", str(labels_script)], capture_output=True, text=True, env=env)
    assert proc.returncode != 0


@pytest.mark.parametrize("blocking,expected", [([MEDIUM_BLOCKING], "findings"), ([], "cleared")])
def test_the_event_is_findings_only_for_a_blocking_review_1(
    report_round_script, tmp_path, fake_curl, blocking, expected
):
    """A non-blocking digest (LOW/INFO) is `cleared`: stored, no wake. And the
    event says no nudge follows: the platform must wake the coder directly."""
    findings = blocking + [MINOR]
    sent = _run_report_round(
        report_round_script, tmp_path, fake_curl, findings=findings, blocking=blocking,
        counts={"low": 1, **({"medium": 1} if blocking else {})}, round_="1",
    )
    assert sent["event"] == expected
    assert sent["nudge_follows"] is False
    assert len(sent["findings"]) == len(findings)


def test_the_blocking_step_is_last_and_bypass_reaches_merge_attempt():
    code = _code_only(WORKFLOW.read_text())
    fail = code.index("- name: Fail the job on a blocking review 1")
    for earlier in ("- name: Publish code-review status", "- name: Set round labels",
                    "- name: Report round outcome to the platform"):
        assert code.index(earlier) < fail
    assert code.index("  provenance-gate:") > fail, "the red step must close the code-review job"
    ma = code[code.index("  merge-attempt:"):]
    assert "needs.gate.outputs.bypass == 'true'" in ma[:ma.index("steps:")]
    pg = code[code.index("  provenance-gate:"):]
    assert "needs.gate.outputs.bypass == 'true'" in pg[:pg.index("steps:")]


def test_the_check_status_stays_success_when_the_job_goes_red():
    """The job fails via a final step, never by writing a failure status:
    the gate's reuse check reads state==success to avoid a paid re-dispatch."""
    text = WORKFLOW.read_text()
    i = text.index("- name: Fail the job on a blocking review 1")
    step = text[i:i + 900]
    assert "statuses" not in step and "exit 1" in step


def test_only_a_bot_written_label_counts_as_review1_memory():
    text = WORKFLOW.read_text()
    i = text.index("# Round memory (see the DECIDE note above this step).")
    frag = text[i:i + 1800]
    assert 'actor.login=="github-actions[bot]"' in frag
    assert '"devteam:reviewed","devteam:fix-cycle"' in frag


def test_the_nudge_payload_no_longer_reads_the_needs_human_label(nudge_script, nudge_bin, tmp_path):
    events = '{"login": "frederico", "type": "User"}\n'
    _, state, posts = _run_nudge(nudge_script, nudge_bin, tmp_path, events=events,
                                 labels=("aw-run:" + "r" * 32, "devteam:needs-human"))
    assert posts[0]["needs_human_actor"] is None
    assert "/events" not in (state / "gh.log").read_text()


def test_code_review_runs_after_provenance_gate_without_being_blocked_by_it():
    """Labels must exist before the findings event reads them, but a failed or
    skipped provenance-gate must not block the review."""
    import yaml
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["code-review"]
    assert "provenance-gate" in job["needs"]
    cond = job["if"]
    assert "!cancelled()" in cond and "needs.gate.outputs.allowed == 'true'" in cond


def test_findings_event_retries_an_absent_aw_run_label_bounded():
    text = WORKFLOW.read_text()
    assert "for _attempt in range(4):" in text and "time.sleep(10)" in text


def test_label_step_runs_after_a_failed_footer_step():
    import yaml
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["provenance-gate"]["steps"]
    step = next(s for s in steps if s.get("name") == "Apply provenance labels")
    assert "!cancelled()" in step["if"] and "run_id != ''" in step["if"]


def test_only_findings_events_retry_the_label_read():
    assert 'if event != "findings" or any(x.startswith("aw-run:")' in WORKFLOW.read_text()


# --------------------------------------------------------------------------
# Parent run provenance + merged-event retry on an unresolved link
# --------------------------------------------------------------------------

PARENT_DASHED = "e001220b-8867-44f4-a276-9e3c0c7ac96f"


def test_parent_comes_from_the_commit_that_supplied_the_run_id(
    provenance_script, provenance_gh, tmp_path
):
    """A newer root-run commit (no parent trailer) must not inherit an older child's parent."""
    proc, outputs, _ = _run_provenance(
        provenance_script, provenance_gh, tmp_path,
        commits=[
            _commit(f"AW-Run-Id: {'a' * 32}\nAW-Parent-Run-Id: {PARENT_DASHED}\n", sha="aaaaaaa"),
            _commit(f"AW-Run-Id: {REAL_RUN_ID}\n", sha="bbbbbbb"),
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs["run_id"] == REAL_RUN_ID
    assert outputs["parent_run_id"] == ""


def test_parent_run_trailer_dashed_uuid_is_normalised_newest_wins(
    provenance_script, provenance_gh, tmp_path
):
    proc, outputs, _ = _run_provenance(
        provenance_script, provenance_gh, tmp_path,
        commits=[
            _commit(f"AW-Run-Id: {REAL_RUN_ID}\nAW-Parent-Run-Id: {'a' * 32}\n", sha="aaaaaaa"),
            _commit(f"AW-Run-Id: {REAL_RUN_ID}\nAW-Parent-Run-Id: {PARENT_DASHED}\n", sha="bbbbbbb"),
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs["parent_run_id"] == PARENT_DASHED.replace("-", "")


def test_absent_parent_run_trailer_is_empty(provenance_script, provenance_gh, tmp_path):
    proc, outputs, _ = _run_provenance(
        provenance_script, provenance_gh, tmp_path,
        commits=[_commit(f"AW-Run-Id: {REAL_RUN_ID}\n")],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs["parent_run_id"] == ""


def test_parent_run_renders_inside_the_managed_footer(footer_script, footer_gh, tmp_path):
    import os as _os
    _os.environ["PARENT_RUN_ID"] = "c" * 32
    try:
        proc, state = _run_footer(footer_script, footer_gh, tmp_path, run_id="r" * 32,
                                  agent_slug="coder")
    finally:
        del _os.environ["PARENT_RUN_ID"]
    assert proc.returncode == 0, proc.stdout + proc.stderr
    body = state["pr_body"]
    start = body.index("<!-- devteam:provenance:start -->")
    end = body.index("<!-- devteam:provenance:end -->")
    assert f"**Parent run:** `{'c' * 32}`" in body[start:end]
    assert "**Agent:** `coder`" in body[start:end]


def _run_merged_step(tmp_path, responses):
    """Run the merged-event heredoc with `gh` (label reads) and `curl`
    stubbed. `responses` is the list of bodies curl returns in order."""
    src = _extract_heredoc("- name: Report the merge to the platform")
    script = tmp_path / "merged.py"
    script.write_text(src.replace("time.sleep(5)", "pass"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "n"
    state.write_text("0")
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    (bin_dir / "gh").write_text(
        "#!/bin/sh\necho 'aw-run:" + "d" * 32 + "'\n")
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"p='{state}'; n=int(open(p).read()); open(p,'w').write(str(n+1))\n"
        f"r=json.load(open('{tmp_path}/responses.json'))[min(n,len(json.load(open('{tmp_path}/responses.json')))-1)]\n"
        "a=sys.argv; open(a[a.index('-o')+1],'w').write(json.dumps(r)); print('200', end='')\n")
    for f in ("gh", "curl"):
        (bin_dir / f).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path), "EVENT_URL": "https://ap.invalid",
        "AW_DEVTEAM_EVENT_SECRET": "s", "REPO": "o/r", "PR_NUMBER": "42",
        "PR_URL": "https://x/42", "MERGE_SHA": "a" * 40, "PR_HEAD_SHA": "b" * 40,
        "PR_BODY": "", "ROUND": "1",
    }
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
    return proc, int(state.read_text())


_UNRESOLVED = {"acted": False, "link_resolution": {"status": "unresolved"}}


def test_merged_event_retries_twice_on_unresolved_link(tmp_path):
    proc, posts = _run_merged_step(tmp_path, [_UNRESOLVED, _UNRESOLVED, {"acted": True}])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert posts == 3


def test_merged_event_retries_on_the_handlers_real_unresolved_response(tmp_path):
    real = {"acted": False, "reason": "no PR->coder link for this URL"}
    proc, posts = _run_merged_step(tmp_path, [real, {"acted": True}])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert posts == 2


def test_merged_event_does_not_retry_when_acted(tmp_path):
    proc, posts = _run_merged_step(tmp_path, [{"acted": True}])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert posts == 1


def test_merged_event_retry_is_bounded(tmp_path):
    proc, posts = _run_merged_step(tmp_path, [_UNRESOLVED])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert posts == 3


# --------------------------------------------------------------------------
# merge-attempt: a red required check wakes the opener coder (ci_done nudge)
# --------------------------------------------------------------------------

_CI_RED_STEP = "- name: Wake the opener coder on a red check"
_RED_SHA = "c" * 40
_RED_CHECKS = [
    {"name": "lint", "state": "FAILURE", "bucket": "fail",
     "link": "https://github.com/o/r/actions/runs/9/job/77"},
    {"name": "review / provenance-gate", "state": "FAILURE", "bucket": "fail", "link": ""},
]


@pytest.fixture(scope="module")
def ci_red_script(tmp_path_factory) -> Path:
    src = _extract_heredoc(_CI_RED_STEP)
    assert "ci_failure" in src, "extracted the wrong block"
    path = tmp_path_factory.mktemp("wf") / "ci_red.py"
    path.write_text(src)
    return path


@pytest.fixture(scope="module")
def ci_red_bin(tmp_path_factory) -> Path:
    """Fake `gh` (answers from `$FAKE_GH_DIR` JSON) and fake `curl` (records the
    POSTed body and answers `$FAKE_CURL_CODE`)."""
    b = tmp_path_factory.mktemp("cibin")
    gh = b / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "a = ' '.join(sys.argv[1:]); d = os.environ['FAKE_GH_DIR']\n"
        "def emit(n):\n"
        "    p = os.path.join(d, n)\n"
        "    if not os.path.exists(p): sys.exit(1)\n"
        "    sys.stdout.write(open(p).read()); sys.exit(0)\n"
        "if a.startswith('api') and a.endswith('/pulls') and '/commits/' in a: emit('pulls.json')\n"
        "if a.startswith('api') and '/labels' in a: emit('labels.txt')\n"
        "if a.startswith('pr checks'):\n"
        "    p = os.path.join(d, 'checks.json')\n"
        "    if not os.path.exists(p): sys.stderr.write('HTTP 502'); sys.exit(1)\n"
        "    sys.stdout.write(open(p).read()); sys.exit(8)\n"
        "if a.startswith('pr view'): emit('view.json')\n"
        "if a.startswith('run view'): emit('log.txt')\n"
        "sys.exit(1)\n")
    gh.chmod(0o755)
    curl = b / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "a = sys.argv[1:]; d = os.environ['FAKE_GH_DIR']\n"
        "body = open(a[a.index('--data-binary') + 1][1:]).read()\n"
        "open(os.path.join(d, 'posted.jsonl'), 'a').write(body + '\\n')\n"
        "import hashlib, hmac\n"
        "want = 'X-Hub-Signature-256: sha256=' + hmac.new(b's3cret', body.encode(), hashlib.sha256).hexdigest()\n"
        "open(os.path.join(d, 'sig_ok'), 'a').write('1' if want in a else '0')\n"
        "open(a[a.index('-o') + 1], 'w').write('{}')\n"
        "sys.stdout.write(os.environ.get('FAKE_CURL_CODE', '200'))\n")
    curl.chmod(0o755)
    return b


def _run_ci_red(ci_red_script, ci_red_bin, tmp_path, *, labels="aw-run:abc\n",
                checks=None, pulls=None, view=None, secret="s3cret",
                log="line1\nlint error: unused var\n", curl_code="200"):
    d = tmp_path / "cired"
    d.mkdir(exist_ok=True)
    for f in d.iterdir():
        f.unlink()
    pr = {"number": 7, "state": "open",
          "head": {"sha": _RED_SHA, "repo": {"full_name": "o/r"}}}
    (d / "pulls.json").write_text(json.dumps([pr] if pulls is None else pulls))
    if labels is not None:
        (d / "labels.txt").write_text(labels)
    if checks is not _MISSING:
        (d / "checks.json").write_text(json.dumps(_RED_CHECKS if checks is None else checks))
    (d / "view.json").write_text(json.dumps(
        {"state": "OPEN", "headRefOid": _RED_SHA, "url": "https://github.com/o/r/pull/7",
         "statusCheckRollup": []} if view is None else view))
    (d / "log.txt").write_text(log)
    env = {**os.environ, "PATH": f"{ci_red_bin}{os.pathsep}{os.environ['PATH']}",
           "FAKE_GH_DIR": str(d), "FAKE_CURL_CODE": curl_code, "GH_TOKEN": "t",
           "REPO": "o/r", "EVENT_URL": "https://ap.invalid", "AW_DEVTEAM_EVENT_SECRET": secret,
           "WORKFLOW_RUN_HEAD_SHA": _RED_SHA, "RUNNER_TEMP": str(d),
           "NUDGE_SLEEP_SCALE": "0"}
    proc = subprocess.run([sys.executable, str(ci_red_script)], capture_output=True,
                          text=True, env=env)
    posted = (d / "posted.jsonl").read_text().splitlines() if (d / "posted.jsonl").exists() else []
    return proc, [json.loads(x) for x in posted]


def test_a_red_required_check_posts_a_ci_done_nudge_with_job_url_and_log_tail(
    ci_red_script, ci_red_bin, tmp_path
):
    proc, posted = _run_ci_red(ci_red_script, ci_red_bin, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(posted) == 1, proc.stdout
    p = posted[0]
    assert p["trigger"]["kind"] == "ci_done" and p["trigger"]["actor_type"] == "Bot"
    assert p["head_sha"] == _RED_SHA and p["run_id"] == "abc" and p["pr_number"] == 7
    # only the non-advisory failing check is reported, with its job URL
    assert p["ci_failure"]["checks"] == [
        {"name": "lint", "url": "https://github.com/o/r/actions/runs/9/job/77"}]
    assert "unused var" in p["ci_failure"]["log_tail"]


def test_the_ci_nudge_is_signed_with_the_event_secret(ci_red_script, ci_red_bin, tmp_path):
    proc, posted = _run_ci_red(ci_red_script, ci_red_bin, tmp_path)
    assert posted, proc.stdout + proc.stderr
    # the fake curl recomputes the HMAC over the body it received
    assert (tmp_path / "cired" / "sig_ok").read_text() == "1"


@pytest.mark.parametrize("kw,why", [
    ({"labels": "bug\n"}, "no aw-run/aw-card label: opener unknown"),
    ({"checks": [{"name": "review / provenance-gate", "state": "FAILURE",
                  "bucket": "fail", "link": ""}]}, "only advisory checks are red"),
    ({"checks": [{"name": "lint", "state": "SUCCESS", "bucket": "pass", "link": ""}]},
     "nothing is red"),
    ({"pulls": []}, "no PR owns the sha (push to master)"),
    ({"pulls": [{"number": 7, "state": "open",
                 "head": {"sha": _RED_SHA, "repo": {"full_name": "fork/r"}}}]}, "fork PR"),
    ({"view": {"state": "OPEN", "headRefOid": "d" * 40, "url": "u"}}, "head moved"),
    ({"view": {"state": "MERGED", "headRefOid": _RED_SHA, "url": "u"}}, "not open"),
    ({"secret": ""}, "no secret configured"),
])
def test_the_ci_nudge_stays_silent_and_green_when_it_has_nothing_to_say(
    ci_red_script, ci_red_bin, tmp_path, kw, why
):
    proc, posted = _run_ci_red(ci_red_script, ci_red_bin, tmp_path, **kw)
    assert proc.returncode == 0, why + proc.stdout + proc.stderr
    assert posted == [], why


@pytest.mark.parametrize("kw", [{"curl_code": "500"}, {"curl_code": "404"}, {"log": ""}])
def test_the_ci_nudge_never_fails_the_pr(ci_red_script, ci_red_bin, tmp_path, kw):
    proc, _ = _run_ci_red(ci_red_script, ci_red_bin, tmp_path, **kw)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_ci_nudge_step_is_isolated_from_the_merge_and_review_flow():
    text = WORKFLOW.read_text()
    start = text.index(_CI_RED_STEP)
    step = text[start:text.index("\n  merged-event:", start)]
    code = _code_only(step)
    assert "continue-on-error: true" in step
    assert "workflow_run.conclusion == 'failure'" in step
    # never touches the merge or the review: no merge call, no /aw-review, no
    # review-event POST
    assert "pr merge" not in code and "aw-review" not in code
    assert "devteam-review-event" not in code
    assert "devteam-pr-nudge" in code
    # and the existing merge step is still the one the merge tests extract
    assert text.index(_MERGE_STEP) < start


def test_a_failed_checks_lookup_is_reported_not_read_as_nothing_failing(
    ci_red_script, ci_red_bin, tmp_path
):
    proc, posted = _run_ci_red(ci_red_script, ci_red_bin, tmp_path, checks=_MISSING)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert posted == []
    assert "could not read PR #7's checks" in proc.stdout
    assert "no non-advisory check is failing" not in proc.stdout


# --------------------------------------------------------------------------
# card 4215bf3b: errored reviews are refunded, same-head runs dedupe, errored
# review wakes the opener coder
# --------------------------------------------------------------------------


def test_refunded_attempts_do_not_count_toward_the_ceiling(gate_tail_script, ceil_gh, tmp_path):
    # 3 charged, 2 refunded -> 1 used (error streak 2 < 3): allowed, next slot is 2
    tl = ["review-count:1", "review-error:1", "review-refund:1", "review-count:2",
          "review-error:2", "review-refund:2", "review-count:3"]
    proc, out, calls = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl)
    assert out.get("allowed") == "true" and "capped" not in out, proc.stdout + proc.stderr
    assert out["charged"] == "true" and out["slot"] == "2"
    assert any("labels[]=review-count:2" in c for c in calls), calls


def test_run_scoped_refunds_count_and_each_charge_gets_a_unique_id(gate_tail_script, ceil_gh, tmp_path):
    # three charges, one numeric run-scoped refund -> 2 used: slot 3 is allowed
    tl = ["review-count:1", "review-count:2", "review-error:101", "review-refund:2:101-1",
          "review-count:3"]
    proc, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl)
    assert out.get("allowed") == "true" and "capped" not in out, proc.stdout + proc.stderr
    assert out["slot"] == "3"
    # the gate names this charge (run id + attempt) so a full rerun is distinct
    assert out["charge"] == "102-2"


def test_a_full_rerun_that_errors_again_refunds_its_own_new_charge(tmp_path):
    # attempt 1 charged+refunded slot 1; a full rerun (same run id, attempt 2)
    # re-charged slot 1 and errored: its refund must not be mistaken for attempt 1's.
    tl = "review-count:1\nreview-error:x-1\nreview-refund:1:x-1\nreview-count:1"
    proc, _, calls = _run_refund(tmp_path, outcome="failure", slot="1", charge="x-2", timeline=tl)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("labels[]=review-refund:1:x-2" in c for c in calls), calls
    # a failed-jobs rerun reuses the gate output (same charge id): no double refund
    _, _, calls = _run_refund(tmp_path, outcome="failure", slot="1", charge="x-2",
                              timeline=tl + "\nreview-error:x-x\nreview-refund:1:x-2")
    assert not any("-X POST" in c for c in calls), calls


def test_pr185_three_errored_reviews_with_zero_verdicts_is_not_the_budget_ceiling(
        gate_tail_script, ceil_gh, tmp_path):
    # The incident: 3 dispatches, all errored. Budget used is 0, so it is NOT
    # a budget refusal; the separate errored-streak bound is what speaks.
    tl = ["review-count:1", "review-error:11", "review-refund:1", "review-count:1",
          "review-error:12", "review-refund:1", "review-count:1", "review-error:13",
          "review-refund:1"]
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl)
    assert out["capped"] == "true" and out["cap_kind"] == "errors"
    assert out["review_count"] == "3" and out["max_reviews"] == "3"
    # with the streak bound off, the same PR is simply allowed: 0 of 3 used
    (tmp_path / "timeline").unlink()
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl, max_errored="0")
    assert out["allowed"] == "true" and out["slot"] == "1" and "capped" not in out


def test_a_completed_review_resets_the_error_streak(gate_tail_script, ceil_gh, tmp_path):
    tl = ["review-count:1", "review-error:11", "review-refund:1", "review-count:1",
          "review-error:12", "review-refund:1", "review-count:1", "review-ok",
          "review-count:2", "review-error:13", "review-refund:2"]
    proc, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl)
    assert out["allowed"] == "true" and "capped" not in out, proc.stdout + proc.stderr


def test_real_verdicts_still_hit_the_budget_ceiling(gate_tail_script, ceil_gh, tmp_path):
    tl = ["review-count:1", "review-ok", "review-count:2", "review-ok", "review-count:3", "review-ok"]
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl)
    assert out["capped"] == "true" and out["cap_kind"] == "budget" and out["review_count"] == "3"


def _fresh(minutes_ago):
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_fresh_pending_review_of_the_same_head_dedupes_a_second_trigger(
        gate_tail_script, cap_gh, tmp_path):
    pending = {**_st("pending", "Dev Team deep review in progress"), "updated_at": _fresh(1)}
    proc, out, calls = _gate(gate_tail_script, cap_gh, tmp_path, round_="1", action="opened",
                             statuses=[pending])
    assert "allowed=false" in out and "allowed=true" not in out, proc.stdout + proc.stderr
    assert "already in flight" in proc.stdout
    # refused before the budget: nothing was charged, no pending rewritten
    assert not any("labels[]=review-count" in c for c in calls), calls


def test_a_stale_pending_status_does_not_block_the_head(gate_tail_script, cap_gh, tmp_path):
    pending = {**_st("pending", "Dev Team deep review in progress"), "updated_at": _fresh(90)}
    _, out, _ = _gate(gate_tail_script, cap_gh, tmp_path, round_="1", action="opened",
                      statuses=[pending])
    assert "allowed=true" in out


def test_the_gate_claims_the_head_with_a_pending_status_when_it_admits_a_review(
        gate_tail_script, cap_gh, tmp_path):
    _, out, calls = _gate(gate_tail_script, cap_gh, tmp_path, round_="1", action="opened")
    assert "allowed=true" in out
    assert any("state=pending" in c and "context=devteam/code-review" in c for c in calls), calls


def test_the_error_streak_is_bounded_even_with_the_paid_ceiling_off_and_without_refunds(
        gate_tail_script, ceil_gh, tmp_path):
    # review finding: max_reviews=0 skipped the streak check, and an uncharged
    # error (missing key) left no event. Error events alone must stop dispatch.
    tl = ["review-error:11", "review-error:12", "review-error:13"]
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl, max_reviews="0")
    assert out["allowed"] == "false" and out["cap_kind"] == "errors", out
    (tmp_path / "timeline").unlink()
    _, out, calls = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl[:2], max_reviews="0")
    assert out["allowed"] == "true" and "charged" not in out
    assert not any("labels[]=review-count" in c for c in calls)


def test_the_gate_serialises_per_pr_for_pr_and_comment_events():
    text = WORKFLOW.read_text()
    gate = text[text.index("\n  gate:\n"):text.index("\n    steps:\n", text.index("\n  gate:\n"))]
    group = [l for l in gate.splitlines() if l.strip().startswith("group:")][0]
    # a comment event has no head sha: the key must not depend on it, or an
    # opened event and an /aw-review comment for one head get different locks
    assert "head.sha" not in group and "'comment'" not in group
    assert "issue.number" in group and "pull_request.number" in group


# ---- the refund + wake steps of the review job ----

_REFUND_STEP = "- name: Refund the review budget on an errored review, or reset the error streak"
_ERR_STEP = "- name: Tell the PR and wake the opener coder on an errored review"


def _run_refund(tmp_path, *, outcome, charged="true", slot="2", charge="x", gh_fail=False,
                timeline="", timeline_fail=False):
    d = tmp_path / "refund"
    d.mkdir(exist_ok=True)
    (d / "calls.log").write_text("")
    gh = d / "gh"
    gh.write_text("#!/bin/bash\n"
                  'printf "%s\\n" "$*" >> "' + str(d / "calls.log") + '"\n'
                  + ('if [[ "$*" == *"/timeline"* ]]; then ' + ("exit 1" if timeline_fail else
                     "printf '%s\\n' '" + timeline + "'; exit 0") + "; fi\n")
                  + ('if [[ "$*" == *"-X POST"* ]]; then exit 1; fi\n' if gh_fail else "")
                  + "exit 0\n")
    gh.chmod(0o755)
    out = d / "out"
    out.write_text("")
    script = d / "refund.sh"
    script.write_text(_extract_run(_REFUND_STEP.replace("- name: ", "")))
    env = {**os.environ, "PATH": f"{d}{os.pathsep}{os.environ['PATH']}", "GH_TOKEN": "t",
           "FINDINGS_OUTCOME": outcome, "CHARGED": charged, "SLOT": slot, "CHARGE": charge, "PR_NUMBER": "7",
           "GITHUB_OUTPUT": str(out)}
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    return proc, out.read_text(), (d / "calls.log").read_text().splitlines()


def test_an_errored_review_refunds_the_charged_slot(tmp_path):
    proc, out, calls = _run_refund(tmp_path, outcome="failure")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "errored=true" in out
    delete = [i for i, c in enumerate(calls) if "-X DELETE" in c and "review-refund%3A2" in c]
    post = [i for i, c in enumerate(calls) if "labels[]=review-refund:2" in c]
    assert delete and post and delete[0] < post[0], calls


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled"])
def test_every_no_verdict_outcome_is_refunded(tmp_path, outcome):
    _, out, calls = _run_refund(tmp_path, outcome=outcome)
    assert "errored=true" in out and any("review-refund:2" in c for c in calls)


def test_a_real_verdict_is_not_refunded_and_resets_the_streak(tmp_path):
    _, out, calls = _run_refund(tmp_path, outcome="success")
    assert "errored" not in out
    assert not any("review-refund" in c for c in calls)
    assert any("labels[]=review-ok" in c for c in calls)


def test_an_uncharged_attempt_has_nothing_to_refund_but_still_counts_as_errored(tmp_path):
    _, out, calls = _run_refund(tmp_path, outcome="failure", charged="")
    assert "errored=true" in out and not any("review-refund" in c for c in calls)
    # ...and still advances the gate's error streak (missing key, max_reviews=0)
    assert any("labels[]=review-error:" in c for c in calls), calls


def test_a_rerun_of_the_same_failed_run_does_not_refund_twice(tmp_path):
    # "Re-run failed jobs" reuses the gate's charged/slot outputs; the run's
    # first failure already recorded review-error:<run id> (+ the refund).
    proc, out, calls = _run_refund(tmp_path, outcome="failure",
                                   timeline="review-count:2\nreview-error:x-x\nreview-refund:2:x")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "errored=true" in out  # the wake path still has its own once-per-head guard
    assert not any("-X POST" in c for c in calls), calls
    assert "already recorded" in proc.stdout and "already refunded" in proc.stdout


def test_replay_after_unreadable_timeline_completes_the_missing_refund_once(tmp_path):
    # first attempt: timeline unreadable -> error marker written, no refund.
    # replay sees review-error:x-x but no review-refund:2 -> must refund, not re-record.
    proc, _, calls = _run_refund(tmp_path, outcome="failure",
                                 timeline="review-count:2\nreview-error:x-x")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [c for c in calls if "labels[]=review-refund:2:x" in c], calls
    assert not any("labels[]=review-error:" in c for c in calls), calls
    # replaying once more now sees both markers: nothing is written
    _, _, calls = _run_refund(tmp_path, outcome="failure",
                              timeline="review-count:2\nreview-error:x-x\nreview-refund:2:x")
    assert not any("-X POST" in c for c in calls), calls


def test_replay_after_failed_error_marker_write_records_it_without_double_refund(tmp_path):
    # marker POST failure: no refund in that run (a replay completes both)
    proc, _, calls = _run_refund(tmp_path, outcome="failure", gh_fail=True,
                                 timeline="review-count:2")
    assert proc.returncode == 0
    assert not any("labels[]=review-refund" in c for c in calls), calls


def test_an_earlier_runs_refund_of_a_reused_slot_does_not_suppress_this_runs(tmp_path):
    # run A errored on slot 2 and was refunded; run x was then charged slot 2 too
    proc, _, calls = _run_refund(tmp_path, outcome="failure",
                                 timeline="review-count:2\nreview-error:A-x\nreview-refund:2:A\nreview-count:2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("labels[]=review-error:" in c for c in calls), calls
    assert any("labels[]=review-refund:2:x" in c for c in calls), calls
    # and a replay of x (marker + its own refund after it) writes nothing
    _, _, calls = _run_refund(tmp_path, outcome="failure",
                              timeline="review-error:A-x\nreview-refund:2\nreview-count:2\nreview-error:x-x\nreview-refund:2:x")
    assert not any("-X POST" in c for c in calls), calls


def test_a_different_runs_later_refund_of_the_same_slot_does_not_hide_a_missing_refund(tmp_path):
    # QA rework: x (slot 3) errored, both of its writes after the marker failed;
    # run 11 (slot 2) was replayed and refunded, then run 13 took slot 3 and
    # refunded it. On a replay x must still refund its OWN charge.
    tl = ("review-count:1\nreview-ok\nreview-count:2\nreview-error:11\nreview-count:3\n"
          "review-error:x-x\nreview-refund:2:11\nreview-count:3\nreview-error:13\nreview-refund:3:13")
    proc, _, calls = _run_refund(tmp_path, outcome="failure", slot="3", timeline=tl)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("labels[]=review-refund:3:x" in c for c in calls), calls
    assert not any("labels[]=review-error:" in c for c in calls), calls
    # once x's own refund exists, a further replay writes nothing
    _, _, calls = _run_refund(tmp_path, outcome="failure", slot="3", timeline=tl + "\nreview-refund:3:x")
    assert not any("-X POST" in c for c in calls), calls


def test_refund_post_failure_after_error_marker_leaves_it_recoverable(tmp_path):
    # refund POST fails (gh_fail) after the marker: no crash; a replay with
    # marker-only timeline refunds (covered above)
    proc, _, _ = _run_refund(tmp_path, outcome="failure", gh_fail=True,
                             timeline="review-count:2\nreview-error:x-x")
    assert proc.returncode == 0 and "still counts" in proc.stdout + proc.stderr


def test_an_unreadable_timeline_records_the_error_but_never_refunds(tmp_path):
    proc, _, calls = _run_refund(tmp_path, outcome="failure", timeline_fail=True)
    assert proc.returncode == 0
    assert any("labels[]=review-error:" in c for c in calls)
    assert not any("review-refund" in c for c in calls), calls


def test_a_failed_refund_write_never_fails_the_job(tmp_path):
    proc, _, _ = _run_refund(tmp_path, outcome="failure", gh_fail=True)
    assert proc.returncode == 0
    assert "no refund was made" in proc.stdout + proc.stderr


def test_refund_and_wake_steps_are_best_effort_and_skip_advisory_runs():
    text = WORKFLOW.read_text()
    for name in (_REFUND_STEP, _ERR_STEP):
        step = text[text.index(name):]
        head = step[: step.index("        run: |")]
        assert "continue-on-error: true" in head and "always()" in head, name
    errhead = text[text.index(_ERR_STEP):]
    errhead = errhead[: errhead.index("        run: |")]
    assert "informational" in errhead  # /aw-improve never wakes the coder


@pytest.fixture(scope="module")
def review_error_script(tmp_path_factory) -> Path:
    src = _extract_heredoc(_ERR_STEP)
    assert "review_error" in src, "extracted the wrong block"
    path = tmp_path_factory.mktemp("wf") / "review_error.py"
    path.write_text(src)
    return path


def _run_review_error(review_error_script, ci_red_bin, tmp_path, *, response=None, labels="aw-run:abc\n",
                      head=_RED_SHA, comments="", curl_code="200", secret="s3cret"):
    d = tmp_path / "rerr"
    d.mkdir(exist_ok=True)
    for f in d.iterdir():
        f.unlink()
    (d / "labels.txt").write_text(labels)
    (d / "view.json").write_text(json.dumps(
        {"state": "OPEN", "headRefOid": head, "url": "https://github.com/o/r/pull/7",
         "statusCheckRollup": []}))
    (d / "comments.txt").write_text(comments)
    if response is not None:
        (d / "dispatch_response.json").write_text(json.dumps(response))
    # a gh that serves the three reads and records comment POSTs
    gb = tmp_path / "rerrbin"
    gb.mkdir(exist_ok=True)
    gh = gb / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "a = ' '.join(sys.argv[1:]); d = os.environ['FAKE_GH_DIR']\n"
        "if '-X POST' in a and '/comments' in a:\n"
        "    open(os.path.join(d, 'comment_posts.log'), 'a').write(a + '\\n'); sys.exit(0)\n"
        "if a.startswith('api') and a.endswith('--jq .[].body'): sys.stdout.write(open(os.path.join(d, 'comments.txt')).read()); sys.exit(0)\n"
        "if a.startswith('api') and '/labels' in a: sys.stdout.write(open(os.path.join(d, 'labels.txt')).read()); sys.exit(0)\n"
        "if a.startswith('pr view'): sys.stdout.write(open(os.path.join(d, 'view.json')).read()); sys.exit(0)\n"
        "sys.exit(1)\n")
    gh.chmod(0o755)
    env = {**os.environ,
           "PATH": f"{gb}{os.pathsep}{ci_red_bin}{os.pathsep}{os.environ['PATH']}",
           "FAKE_GH_DIR": str(d), "FAKE_CURL_CODE": curl_code, "GH_TOKEN": "t", "REPO": "o/r",
           "PR_NUMBER": "7", "HEAD_SHA": _RED_SHA, "EVENT_URL": "https://ap.invalid",
           "AW_DEVTEAM_EVENT_SECRET": secret, "RUNNER_TEMP": str(d), "NUDGE_SLEEP_SCALE": "0",
           "DISPATCH_OUTCOME": "success", "RUN_URL": "https://github.com/o/r/actions/runs/55"}
    proc = subprocess.run([sys.executable, str(review_error_script)], capture_output=True,
                          text=True, env=env)
    posted = (d / "posted.jsonl").read_text().splitlines() if (d / "posted.jsonl").exists() else []
    cp = ([x for x in (d / "comment_posts.log").read_text().splitlines() if x.startswith("api -X POST")]
          if (d / "comment_posts.log").exists() else [])
    return proc, [json.loads(x) for x in posted], cp


_TEMP_400 = {"aw_status": "error", "choices": [{"message": {"content":
             "Error code: 400 - Unsupported parameter: temperature is not supported with this model"}}]}


def test_an_errored_review_wakes_the_opener_with_the_error_text_and_comments(
        review_error_script, ci_red_bin, tmp_path):
    proc, posted, comments = _run_review_error(review_error_script, ci_red_bin, tmp_path,
                                               response=_TEMP_400)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(posted) == 1, proc.stdout
    p = posted[0]
    assert p["trigger"]["kind"] == "review_error" and p["head_sha"] == _RED_SHA
    assert p["run_id"] == "abc" and p["pr_number"] == 7
    assert "temperature is not supported" in p["review_error"]["text"]
    assert "aw_status=error" in p["review_error"]["text"]
    assert p["review_error"]["run_url"].endswith("/actions/runs/55")
    assert (tmp_path / "rerr" / "sig_ok").read_text() == "1"
    assert len(comments) == 1 and "review-error:" + _RED_SHA in comments[0]


def test_the_pr_comment_is_posted_once_per_head(review_error_script, ci_red_bin, tmp_path):
    proc, posted, comments = _run_review_error(
        review_error_script, ci_red_bin, tmp_path, response=_TEMP_400,
        comments=f"<!-- review-error:{_RED_SHA} -->\nold\n")
    assert comments == [] and len(posted) == 1  # no second comment; the platform dedupes the wake


@pytest.mark.parametrize("kw,why", [
    ({"labels": "bug\n"}, "not an agent PR"),
    ({"head": "d" * 40}, "head moved"),
    ({"secret": ""}, "SECRET"),
])
def test_no_wake_when_the_opener_is_unknown_or_the_head_moved(
        review_error_script, ci_red_bin, tmp_path, kw, why):
    proc, posted, _ = _run_review_error(review_error_script, ci_red_bin, tmp_path,
                                        response=_TEMP_400, **kw)
    assert proc.returncode == 0 and posted == [], why


def test_a_missing_response_file_still_reports_a_dispatch_failure(
        review_error_script, ci_red_bin, tmp_path):
    proc, posted, _ = _run_review_error(review_error_script, ci_red_bin, tmp_path, response=None)
    assert posted and "dispatch failed" in posted[0]["review_error"]["text"]


def test_the_wake_never_fails_the_pr_when_the_platform_is_down(
        review_error_script, ci_red_bin, tmp_path):
    proc, posted, _ = _run_review_error(review_error_script, ci_red_bin, tmp_path,
                                        response=_TEMP_400, curl_code="503")
    assert proc.returncode == 0 and "coder not woken" in proc.stdout + proc.stderr


def test_full_reruns_of_one_run_each_advance_the_error_streak(gate_tail_script, ceil_gh, tmp_path):
    # QA: three errored attempts of ONE run id wrote one marker, so the cap never tripped.
    tl = ["review-error:11-1", "review-error:11-2", "review-error:11-3"]
    _, out, _ = _ceil(gate_tail_script, ceil_gh, tmp_path, timeline=tl, max_reviews="0")
    assert out["allowed"] == "false" and out["cap_kind"] == "errors", out


def test_a_later_attempt_of_the_same_run_records_its_own_error(tmp_path):
    # attempt 1 of this run id is on the timeline; this attempt (x-x) must still be recorded
    _, _, calls = _run_refund(tmp_path, outcome="failure", timeline="review-count:2\nreview-error:x-1")
    assert any("labels[]=review-error:x-x" in c for c in calls), calls


def _run_recheck(tmp_path, timeline, max_errored="3"):
    d = tmp_path / "recheck"
    d.mkdir(exist_ok=True)
    gh = d / "gh"
    gh.write_text("#!/bin/bash\nprintf '%s\\n' '" + timeline + "'\n")
    gh.chmod(0o755)
    script = d / "recheck.sh"
    script.write_text(_extract_run("Recheck the error ceiling on a rerun"))
    env = {**os.environ, "PATH": f"{d}{os.pathsep}{os.environ['PATH']}", "GH_TOKEN": "t",
           "PR_NUMBER": "7", "MAX_ERRORED": max_errored}
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)


def test_a_failed_jobs_rerun_rechecks_the_error_ceiling_before_dispatching(tmp_path):
    # review finding: Re-run failed jobs keeps the gate's saved allowed=true
    p = _run_recheck(tmp_path, "review-error:11-1\nreview-error:11-2\nreview-error:11-3")
    assert p.returncode == 1 and "not dispatching" in p.stdout, p.stdout + p.stderr
    p = _run_recheck(tmp_path, "review-error:11-1\nreview-error:11-2")
    assert p.returncode == 0, p.stdout + p.stderr
    p = _run_recheck(tmp_path, "review-error:11-1\nreview-error:11-2\nreview-ok\nreview-error:12-1")
    assert p.returncode == 0, p.stdout + p.stderr


def test_the_recheck_step_runs_only_on_reruns_and_before_dispatch():
    text = WORKFLOW.read_text()
    i = text.index("- name: Recheck the error ceiling on a rerun")
    assert "github.run_attempt > 1" in text[i:i + 200]
    assert i < text.index("- name: Dispatch Code Reviewer")


def test_the_timeline_filter_keeps_attempt_scoped_error_labels():
    # review finding: the fake gh ignores --jq. Run the REAL filter from the gate.
    text = WORKFLOW.read_text()
    m = re.search(r"--jq '(\.\[\] \| select\(\.event==\"labeled\" and \.actor\.login==\"github-actions\[bot\]\" and \(\(\.label\.name\s*//\s*\"\"\)\s*\|\s*test\([^']*)'", text)
    assert m, "gate jq filter not found"
    events = [{"event": "labeled", "actor": {"login": "github-actions[bot]"}, "label": {"name": n}}
              for n in ["review-error:11-1", "review-error:11-2", "review-refund:1:11-1", "review-count:1",
                        "review-ok", "unrelated"]]
    out = subprocess.run(["jq", "-r", m.group(1)], input=json.dumps(events),
                         capture_output=True, text=True)
    assert out.stdout.split() == ["review-error:11-1", "review-error:11-2", "review-refund:1:11-1",
                                  "review-count:1", "review-ok"], out.stdout + out.stderr
