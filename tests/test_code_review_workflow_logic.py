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
    script: Path, tmp_path, fake_gh, *, findings, counts, round_: str, is_draft: str = "false"
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
    (tmp_path / "dispatch_response.json").write_text(
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "Here is my review:\n```json\n"
                            + json.dumps({"findings": findings, "counts": counts})
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


def test_round1_merges_only_on_zero_findings(eligibility_script, tmp_path, fake_gh):
    clean = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[], counts={}, round_="1"
    )
    assert clean["eligible"] == "true"
    assert clean["findings_count"] == "0"

    dirty = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[MINOR], counts={"low": 1}, round_="1"
    )
    assert dirty["eligible"] == "false", "any round-1 finding must wake the coder"
    assert dirty["findings_count"] == "1"


def test_round1_counts_a_cross_cutting_finding(eligibility_script, tmp_path, fake_gh):
    """A finding with no file/line is posted as a top-level comment; it still
    counts, or a purely architectural objection would merge straight through."""
    got = _run_eligibility(
        eligibility_script,
        tmp_path,
        fake_gh,
        findings=[CROSS_CUTTING],
        counts={"info": 1},
        round_="1",
    )
    assert got["eligible"] == "false"
    assert got["findings_count"] == "1"


def test_round2_gate_is_blocks_merge_and_not_severity(eligibility_script, tmp_path, fake_gh):
    """The core of the round-2 rule, in both directions."""
    high_but_fine = _run_eligibility(
        eligibility_script,
        tmp_path,
        fake_gh,
        findings=[HIGH_NON_BLOCKING],
        counts={"high": 1},
        round_="2",
    )
    assert high_but_fine["eligible"] == "true", "severity must not block round 2"
    assert high_but_fine["blocking_count"] == "0"

    medium_but_blocking = _run_eligibility(
        eligibility_script,
        tmp_path,
        fake_gh,
        findings=[HIGH_NON_BLOCKING, MEDIUM_BLOCKING],
        counts={"high": 1, "medium": 1},
        round_="2",
    )
    assert medium_but_blocking["eligible"] == "false"
    assert medium_but_blocking["blocking_count"] == "1"


def test_round2_missing_blocks_merge_fails_closed(eligibility_script, tmp_path, fake_gh):
    """Schema drift must escalate to a human, never silently merge over a
    finding nobody judged."""
    got = _run_eligibility(
        eligibility_script,
        tmp_path,
        fake_gh,
        findings=[NO_BLOCKS_FIELD],
        counts={"low": 1},
        round_="2",
    )
    assert got["eligible"] == "false"
    assert got["blocking_count"] == "1"
    assert got["missing_blocks_merge"] == "1"


def test_round2_clean_is_eligible(eligibility_script, tmp_path, fake_gh):
    got = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[], counts={}, round_="2"
    )
    assert got["eligible"] == "true"


@pytest.mark.parametrize("round_", ["needs-human", "informational"])
def test_terminal_and_informational_rounds_never_merge(
    eligibility_script, tmp_path, fake_gh, round_
):
    got = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[], counts={}, round_=round_
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
# findings, round 2 with a blocking finding, a draft PR, `needs-human` — all
# of them must leave the SHA with no status at all.


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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"findings": [MINOR], "counts": {"low": 1}, "round_": "1"},
        {"findings": [MEDIUM_BLOCKING], "counts": {"medium": 1}, "round_": "2"},
        {"findings": [NO_BLOCKS_FIELD], "counts": {"low": 1}, "round_": "2"},
        {"findings": [], "counts": {}, "round_": "needs-human"},
        {"findings": [], "counts": {}, "round_": "informational"},
        {"findings": [], "counts": {}, "round_": "1", "is_draft": "true"},
    ],
    ids=["round1-finding", "round2-blocking", "round2-missing-field",
         "needs-human", "informational", "draft"],
)
def test_the_eligible_status_is_never_published_when_not_eligible(
    eligibility_script, tmp_path, fake_gh, kwargs
):
    """Absence of the status IS the negative verdict — there is no failure
    counterpart for the merge job to read. So writing it on a head the review
    did not clear is not a cosmetic slip, it is a merge the review refused."""
    got = _run_eligibility(eligibility_script, tmp_path, fake_gh, **kwargs)
    assert got["eligible"] == "false"
    assert _status_posts(tmp_path) == [], (
        "devteam/merge-eligible was published for an INELIGIBLE review"
    )


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
    script: Path, fake_gh, *, labels: str, mode: str = "review", fetch_fails: bool = False
):
    env = {
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


@pytest.mark.parametrize(
    "labels,expected",
    [
        ("", "1"),  # no labels at all
        ("bug enhancement", "1"),  # unrelated labels
        ("devteam:awaiting-fixes", "2"),
        ("devteam:needs-human", "needs-human"),
        ("devteam:awaiting-fixes devteam:needs-human", "needs-human"),  # terminal wins
        ("devteam:awaiting-fixes-but-not-really", "1"),  # exact match only
    ],
)
def test_round_is_detected_from_live_labels(round_script, fake_gh, labels, expected):
    rc, round_, _ = _detect_round(round_script, fake_gh, labels=labels)
    assert rc == 0
    assert round_ == expected


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
# `synchronize` is round 2's automatic trigger, and ONLY round 2's
# --------------------------------------------------------------------------
#
# v1.3.0 adds `synchronize` to the callers' trigger surface so the coder's
# fix push enters round 2 by itself, replacing a manual `/aw-review`. The
# budget rationale that kept `synchronize` out for four releases has not
# changed though: one deep review is a judge run plus up to 3 research runs
# of real subscription budget, and PR-Agent already reviews every push. So
# the gate has to refuse a synchronize on anything that is not round 2 —
# round 1 (the author is still iterating before any review has run), and
# `needs-human` (terminal until a human clears the label; pushing more
# commits must not re-arm it).


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
    return parsed


@pytest.mark.parametrize(
    "labels,expected",
    [
        ("devteam:awaiting-fixes", "true"),          # round 2 — the whole point
        ("", "false"),                               # round 1, author iterating
        ("bug", "false"),
        ("devteam:needs-human", "false"),            # terminal, must stay terminal
        ("devteam:awaiting-fixes devteam:needs-human", "false"),
    ],
)
def test_synchronize_is_admitted_only_on_round_2(
    synchronize_script, fake_gh, tmp_path, labels, expected
):
    got = _decide_allowed(
        synchronize_script, fake_gh, tmp_path, labels=labels, action="synchronize"
    )
    assert got["allowed"] == expected, (
        f"synchronize on labels {labels!r} -> allowed={got['allowed']}\n{got['_log']}"
    )


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
_ELIGIBLE = {"statuses": [{"context": "devteam/merge-eligible", "state": "success"}]}
_NOT_ELIGIBLE: dict = {"statuses": []}
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
            {"name": "CodeRabbit", "state": "IN_PROGRESS", "bucket": "pending"},
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
