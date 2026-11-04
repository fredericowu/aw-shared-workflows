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
import pytest

WORKFLOW = (Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "code-review-reusable.yml")


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _dedent_block(block: str) -> str:
    """Strip the common leading indentation a YAML heredoc carries."""
    lines = block.split("\n")
    widths = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    pad = min(widths) if widths else 0
    return "\n".join(line[pad:] if len(line) >= pad else line for line in lines)


def _extract_heredoc(marker_before: str) -> str:
    """The body of the first ``python3 … <<'PYEOF' … PYEOF`` heredoc that
    appears after ``marker_before`` in the workflow."""
    text = WORKFLOW.read_text()
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
    fail-loud path below is driven.
    """
    bin_dir = tmp_path_factory.mktemp("bin")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/bash\n"
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
    }
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"step script failed:\n{proc.stderr[-2000:]}"
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


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
    releases."""
    text = WORKFLOW.read_text()
    merge_step = text[text.index("- name: Merge if eligible") :]
    merge_step = merge_step[: merge_step.index("- name: ", 10)]
    assert "AW_DEVTEAM_MERGE_TOKEN" in merge_step
    assert "secrets.GITHUB_TOKEN" not in merge_step


def test_the_merge_waits_on_the_repos_own_ci():
    """`--auto` only waits for checks branch protection marks required, and
    master has none — so the merge must gate on CI itself."""
    text = WORKFLOW.read_text()
    assert "- name: Wait for the repo's own CI to be green" in text
    merge_step = text[text.index("- name: Merge if eligible") :]
    merge_step = merge_step[: merge_step.index("run: |")]
    assert "steps.ci_gate.outcome == 'success'" in merge_step


# --- ci_gate's checks() reader ------------------------------------------------
#
# AP-MT PR #7 burned four consecutive /aw-review rounds on this one function.
# The caller's token lacked `checks: read`, so on that PRIVATE repo `gh pr
# checks` returned nothing at all — and `json.loads(out.stdout or "[]")` made
# that indistinguishable from "this PR legitimately has zero checks", so the
# gate failed instantly with `Saw: []` instead of retrying or timing out.


def _checks_reader(stdout: str, stderr: str):
    """Run the real `checks()` body from the ci_gate step against a faked
    `subprocess.run` result, and return whatever it decided."""
    src = _extract_heredoc("- name: Wait for the repo's own CI to be green")
    assert "def checks():" in src, "extracted the wrong block — no checks() in it"

    body = src[src.index("def checks():"):]
    body = body[: body.index("\n          while True:") if "\n          while True:" in body
                else body.index("\nwhile True:")]

    ns = {"json": json, "os": {}, "subprocess": None, "pr": "1", "repo": "o/r"}

    class _Out:
        pass

    out = _Out()
    out.stdout, out.stderr = stdout, stderr

    class _FakeSubprocess:
        @staticmethod
        def run(*a, **kw):
            return out

    ns["subprocess"] = _FakeSubprocess
    ns["os"] = os
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
