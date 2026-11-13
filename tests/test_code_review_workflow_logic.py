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


def test_informational_round_never_merges(eligibility_script, tmp_path, fake_gh):
    """`/aw-improve` suggestion runs are not a review round at all — they
    must never merge, whatever they found."""
    got = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[], counts={}, round_="informational"
    )
    assert got["eligible"] == "false"


def test_needs_human_round_merges_only_when_nothing_blocks(
    eligibility_script, tmp_path, fake_gh
):
    """An already-escalated PR is judged exactly like round 2, not treated
    as permanently informational: a clean verdict is eligible, a real
    `blocks_merge` finding still is not, and severity alone still isn't the
    gate."""
    clean = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[], counts={}, round_="needs-human"
    )
    assert clean["eligible"] == "true"

    still_blocking = _run_eligibility(
        eligibility_script,
        tmp_path,
        fake_gh,
        findings=[MEDIUM_BLOCKING],
        counts={"medium": 1},
        round_="needs-human",
    )
    assert still_blocking["eligible"] == "false"

    severity_is_not_the_gate = _run_eligibility(
        eligibility_script,
        tmp_path,
        fake_gh,
        findings=[HIGH_NON_BLOCKING],
        counts={"high": 1},
        round_="needs-human",
    )
    assert severity_is_not_the_gate["eligible"] == "true"


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


def test_the_eligible_status_is_published_when_an_escalated_pr_clears(
    eligibility_script, tmp_path, fake_gh
):
    """The merge half of the needs-human re-arm: a clean `needs-human`-round
    verdict must leave the SAME hand-off a clean round 1/2 would, or
    `merge-attempt` has nothing to read even though the label comes off."""
    got = _run_eligibility(
        eligibility_script, tmp_path, fake_gh, findings=[], counts={}, round_="needs-human"
    )
    assert got["eligible"] == "true"
    posts = _status_posts(tmp_path)
    assert len(posts) == 1, f"expected exactly one status post, got {posts}"
    assert f"statuses/{'a' * 40}" in posts[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"findings": [MINOR], "counts": {"low": 1}, "round_": "1"},
        {"findings": [MEDIUM_BLOCKING], "counts": {"medium": 1}, "round_": "2"},
        {"findings": [NO_BLOCKS_FIELD], "counts": {"low": 1}, "round_": "2"},
        {"findings": [MEDIUM_BLOCKING], "counts": {"medium": 1}, "round_": "needs-human"},
        {"findings": [], "counts": {}, "round_": "informational"},
        {"findings": [], "counts": {}, "round_": "1", "is_draft": "true"},
    ],
    ids=["round1-finding", "round2-blocking", "round2-missing-field",
         "needs-human-still-blocking", "informational", "draft"],
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
# `Set round labels` — the state machine's one write to the PR, now with a
# subtraction: a clean `needs-human`-round verdict removes the label itself.
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
    end = text.index("      - name: Report round outcome to the platform")
    body = (
        text[start:end]
        .replace("${{ github.repository }}", "tekflox/aw-app-template")
        .replace("${{ needs.gate.outputs.pr_number }}", "42")
    )
    assert "remove_label" in body, "extracted the wrong fragment"
    path = tmp_path_factory.mktemp("wf") / "labels.sh"
    path.write_text("set -euo pipefail\n" + body)
    return path


def _run_labels(
    script: Path, fake_gh, tmp_path, *, round_: str, findings_count: int = 0, blocking_count: int = 0
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
        "FAKE_LABELS": "",
    }
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"label step failed:\n{proc.stderr[-2000:]}"
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.stdout, calls


def test_round1_findings_add_awaiting_fixes(labels_script, fake_gh, tmp_path):
    out, calls = _run_labels(labels_script, fake_gh, tmp_path, round_="1", findings_count=1)
    assert "labelled: devteam:awaiting-fixes" in out
    assert any("-X POST" in c and "/labels" in c for c in calls)


def test_round2_blocking_adds_needs_human(labels_script, fake_gh, tmp_path):
    out, calls = _run_labels(labels_script, fake_gh, tmp_path, round_="2", blocking_count=1)
    assert "labelled: devteam:needs-human" in out
    assert any("-X POST" in c and "/labels" in c for c in calls)


def test_a_clean_needs_human_round_removes_its_own_label(labels_script, fake_gh, tmp_path):
    """The re-arm: an escalated PR that comes back clean has the label
    pulled by the review itself, the same re-arm a human removing it by
    hand would have produced."""
    out, calls = _run_labels(labels_script, fake_gh, tmp_path, round_="needs-human", blocking_count=0)
    assert "un-labelled: devteam:needs-human" in out
    deletes = [c for c in calls if "-X DELETE" in c]
    assert len(deletes) == 1, f"expected exactly one DELETE, got {calls}"
    assert "labels/devteam%3Aneeds-human" in deletes[0], (
        "the colon in the label name must be percent-encoded in the URL path "
        "or GitHub's API 404s on the literal string"
    )


def test_a_still_blocking_needs_human_round_leaves_the_label_alone(labels_script, fake_gh, tmp_path):
    out, calls = _run_labels(labels_script, fake_gh, tmp_path, round_="needs-human", blocking_count=1)
    assert "no label change" in out
    assert calls == [], f"no gh call should have been made, got {calls}"


def test_a_clean_round1_or_round2_makes_no_label_call(labels_script, fake_gh, tmp_path):
    for round_ in ("1", "2"):
        out, calls = _run_labels(labels_script, fake_gh, tmp_path, round_=round_)
        assert "no label change" in out
        assert calls == [], f"round {round_}: no gh call should have been made, got {calls}"


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
# `needs-human` (re-arming it still takes a clean `/aw-review` verdict or a
# human removing the label; pushing more commits must not re-trigger a paid
# run by itself).


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
        ("devteam:needs-human", "false"),            # no auto re-review from a push
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
    provenance_script, provenance_gh, tmp_path, *, labels=None, commits=None
):
    d = tmp_path / "ghfix"
    d.mkdir(exist_ok=True)
    for leftover in ("calls.log",):
        if (d / leftover).exists():
            (d / leftover).unlink()
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


def test_no_trailer_on_any_commit_fails_the_gate(
    provenance_script, provenance_gh, tmp_path
):
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        commits=[_commit("just a normal commit, no trailer")],
    )
    assert proc.returncode != 0
    assert "no commit on PR" in proc.stdout
    assert "run_id" not in outputs
    assert "devteam/provenance" not in calls


def test_a_malformed_run_id_does_not_count(provenance_script, provenance_gh, tmp_path):
    """Not 32 hex characters — a human-typed look-alike, or a truncated
    id — must not pass as a real stamp."""
    proc, outputs, calls = _run_provenance(
        provenance_script,
        provenance_gh,
        tmp_path,
        commits=[_commit("AW-Run-Id: not-a-real-run-id\n")],
    )
    assert proc.returncode != 0
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
# provenance-gate's footer step — idempotent PR body + sticky comment
# --------------------------------------------------------------------------

_FOOTER_STEP = "- name: Record the provenance footer on the PR body and a sticky comment"


@pytest.fixture(scope="module")
def footer_script(tmp_path_factory) -> Path:
    src = _extract_heredoc(_FOOTER_STEP)
    assert "devteam:provenance:start" in src, "extracted the wrong block"
    path = tmp_path_factory.mktemp("wf") / "footer.py"
    path.write_text(src)
    return path


@pytest.fixture(scope="module")
def footer_gh(tmp_path_factory) -> Path:
    """A stateful fake `gh` that plays PR body + comment store, so the same
    tmp_path can drive this script twice and prove the SECOND call replaces
    the first footer in place rather than appending a second one."""
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
        "if path.endswith('/comments') and '--paginate' in rest:\n"
        "    print(json.dumps(state['comments'])); sys.exit(0)\n"
        "if path.endswith('/comments') and not is_patch:\n"
        "    state['comments'].append({'id': len(state['comments']) + 1, 'body': body_arg()})\n"
        "    save(); sys.exit(0)\n"
        "if '/issues/comments/' in path and is_patch:\n"
        "    cid = int(path.rsplit('/', 1)[-1])\n"
        "    for c in state['comments']:\n"
        "        if c['id'] == cid:\n"
        "            c['body'] = body_arg()\n"
        "    save(); sys.exit(0)\n"
        "print('gh: unexpected call ' + repr(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    gh.chmod(0o755)
    return bin_dir


def _run_footer(
    footer_script, footer_gh, tmp_path, *, run_id, session_id="", card_id="",
    agent_slug="",
):
    d = tmp_path / "ghfix"
    d.mkdir(exist_ok=True)
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
    assert len(state["comments"]) == 1
    assert "r" * 32 in state["comments"][0]["body"]


def test_a_second_round_replaces_the_footer_in_place(footer_script, footer_gh, tmp_path):
    """Round 2 (or a later `/aw-review`) on the same PR must not pile up a
    second footer/comment — it must update the one already there."""
    _run_footer(footer_script, footer_gh, tmp_path, run_id="r" * 32)
    proc, state = _run_footer(footer_script, footer_gh, tmp_path, run_id="s" * 32)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["pr_body"].count("<!-- devteam:provenance:start -->") == 1
    assert "r" * 32 not in state["pr_body"]
    assert "s" * 32 in state["pr_body"]
    assert len(state["comments"]) == 1, "a second round created a second comment"
    assert "s" * 32 in state["comments"][0]["body"]


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
_ELIGIBLE = {"statuses": [
    {"context": "devteam/merge-eligible", "state": "success"},
    {"context": "devteam/provenance", "state": "success"},
]}
_NOT_ELIGIBLE: dict = {"statuses": []}
# Eligible per the review, but no commit ever carried a verified AW-Run-Id
# trailer (or the provenance-exempt label) — the case `provenance-gate`
# exists to create.
_PROVENANCE_NOT_CLEARED = {
    "statuses": [{"context": "devteam/merge-eligible", "state": "success"}]
}
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
        "MISSING_BLOCKS_MERGE": "0",
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


def test_a_round2_clean_pr_sends_cleared_with_the_leftover_digest(
    report_round_script, tmp_path, fake_curl
):
    """Round 2 clean (no finding blocks the merge) still carries whatever
    non-blocking findings survived, `blocks_merge` flags and all — that is
    precisely what the close-gate files as low/medium Backlog cards."""
    leftovers = [HIGH_NON_BLOCKING, MINOR]
    sent = _run_report_round(
        report_round_script, tmp_path, fake_curl,
        findings=leftovers, blocking=[], counts={"high": 1, "low": 1}, round_="2",
    )
    assert sent["event"] == "cleared"
    assert len(sent["findings"]) == 2
    assert {f["blocks_merge"] for f in sent["findings"] if "blocks_merge" in f} == {False}
    assert sent["blocking"] == []


@pytest.mark.parametrize(
    "round_,findings,blocking,expected_event",
    [
        ("1", [MINOR], [], "findings"),  # unchanged: round 1 with findings wakes the coder
        ("2", [MEDIUM_BLOCKING], [MEDIUM_BLOCKING], "escalate"),  # unchanged: round 2 blocking
    ],
)
def test_non_eligible_rounds_are_unaffected_by_the_cleared_change(
    report_round_script, tmp_path, fake_curl, round_, findings, blocking, expected_event
):
    sent = _run_report_round(
        report_round_script, tmp_path, fake_curl,
        findings=findings, blocking=blocking, counts={"x": 1}, round_=round_,
    )
    assert sent["event"] == expected_event


def test_needs_human_round_sends_cleared_when_it_clears(report_round_script, tmp_path, fake_curl):
    """The platform-event half of the re-arm: a clean `needs-human` round
    reports exactly like any other eligible round, so the close-gate's
    head_sha lookup finds it the same way."""
    sent = _run_report_round(
        report_round_script, tmp_path, fake_curl,
        findings=[], blocking=[], counts={}, round_="needs-human",
    )
    assert sent["event"] == "cleared"
    assert sent["round"] == "needs-human"


def test_needs_human_round_still_blocking_sends_no_new_event(
    report_round_script, tmp_path, fake_curl
):
    """`escalate` already fired on the round that set the label — a repeat
    push/`/aw-review` that is still blocking must not re-ping a human who
    already knows."""
    sent = _run_report_round(
        report_round_script, tmp_path, fake_curl,
        findings=[MEDIUM_BLOCKING], blocking=[MEDIUM_BLOCKING], counts={"medium": 1},
        round_="needs-human",
    )
    assert sent == {}, f"expected nothing posted, got {sent}"


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
        "MISSING_BLOCKS_MERGE": "0",
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
