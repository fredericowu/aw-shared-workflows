"""Regression tests for the executable logic and the load-bearing invariants
of ``.github/workflows/security-scan-reusable.yml``.

**Why this file exists.** That workflow is the Semgrep + Trivy + Gitleaks
scan, converged from 61 standalone copies that had already drifted into five
distinct variants before anyone noticed. Almost everything in it that can
break breaks *silently*, and a weekly cron is the worst possible place for a
silent break — nobody is watching at 00:11 on a Monday:

* a dropped ``security-events: write`` on either side is an explicit
  ``none`` rather than an error, and 403s every upload on a repo that *does*
  have Advanced Security — which looks exactly like the no-GHAS failure
  ``sarif_upload`` exists for, sending you to flip the wrong switch;
* a ``continue-on-error`` added to an upload step turns a reporting pipeline
  that reports nothing into a green check (one of the five variants did
  exactly this);
* losing ``fetch-depth: 0`` reduces the gitleaks job to a worse duplicate of
  Trivy's secret scanner, with no visible change at all;
* a renamed SARIF category silently splits a repo's finding history in two;
* and a ``pull_request`` trigger added here would make a fork PR arbitrary
  code execution on the bare-metal host, because the default ``runs_on`` is
  self-hosted.

None of those is reachable by running the workflow on a healthy repo, which
is the class of defect that survived three copies of
``code-review-reusable.yml`` and two of its releases.

**How it works.** Same pattern as the two sibling suites: the step scripts
are EXTRACTED from the workflow file by text-scanning it and then executed
directly, so these tests assert against the bytes CI actually runs. They
cannot drift from the workflow the way a hand-maintained copy of the logic
would — if someone renames a step, extraction fails loudly rather than
silently testing nothing.

Deliberately NOT parsed with PyYAML, for the same reason as the sibling
suites: adding a parser dependency to run a text scan would be incidental
coupling. pytest is the only requirement. (``test.yml`` does separately
``yaml.safe_load`` every workflow in ``.github/workflows/*.yml`` as a smoke
check — that catches "no longer a valid workflow", which is a different
failure from "the logic is wrong".)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

WORKFLOW = (Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "security-scan-reusable.yml")

JOBS = ("semgrep", "trivy", "gitleaks")


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


def _step_run_command(step_name: str) -> str:
    """The single-line ``run:`` value of the named step.

    A separate helper from ``_step_run_body`` on purpose: the gitleaks detect
    step is a one-liner (``run: ./gitleaks detect ... || true``) rather than a
    ``run: |`` block, exactly as it was in the 97-line original. Asserting on
    it with the block-scalar helper silently reads the NEXT step's script, so
    the two forms get two extractors and each fails loudly on the other.
    """
    text = WORKFLOW.read_text()
    anchor = text.index(f"- name: {step_name}")
    nxt = text.find("\n      - name: ", anchor + 1)
    step = text[anchor:nxt if nxt != -1 else len(text)]
    m = re.search(r"^\s*run:\s*(?!\|)(.+)$", step, re.MULTILINE)
    assert m, f"step {step_name!r} has no single-line `run:` value"
    return m.group(1)


def _uncommented_lines() -> list[str]:
    """Every line that is not a YAML comment.

    The caller checklist at the foot of the file is a commented-out example
    caller, which contains its own ``uses:`` line pinned to a TAG. A naive
    whole-file scan for ``uses:`` therefore 'finds' an unpinned action that
    does not exist, and — worse — would keep passing if a real action lost
    its SHA while the checklist still showed one.
    """
    return [ln for ln in WORKFLOW.read_text().splitlines()
            if not ln.lstrip().startswith("#")]


def _job_block(job: str) -> str:
    """The text of one job, bounded by the next job's key (or the trailing
    caller checklist for the last one)."""
    text = WORKFLOW.read_text()
    start = text.index(f"\n  {job}:\n")
    rest = text[start + 1:]
    ends = [rest.index(f"\n  {o}:\n") for o in JOBS if f"\n  {o}:\n" in rest]
    ends.append(rest.index("\n# ── CALLER CHECKLIST"))
    return rest[:min(ends)]


# --------------------------------------------------------------------------
# the gitleaks installer's arch mapping, actually executed
# --------------------------------------------------------------------------
#
# The only genuinely conditional logic in the whole workflow. It is also the
# only place a wrong answer produces a confusing failure rather than an
# obvious one: an unmapped `uname -m` builds a download URL for a release
# asset that does not exist, and `curl -sSfL` then fails the step with a bare
# 404 on a URL nobody reads. The two mappings are not cosmetic — gitleaks
# publishes `linux_x64` and `linux_arm64`, NOT `linux_x86_64`/`linux_aarch64`,
# so a passthrough on either would never download anything.
#
# Executed with `curl`, `tar` and `uname` shimmed onto PATH, so the real
# mapping runs for an architecture the test host does not have to be.


def _run_installer(tmp_path: Path, arch: str) -> str:
    """Execute the extracted installer with a fake `uname -m` and return the
    URL its `curl` was actually called with."""
    script = _step_run_body("Install Gitleaks")
    assert "GL_ARCH" in script, "extracted the wrong step — no arch mapping in it"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    url_log = tmp_path / "curl_url"

    (bin_dir / "uname").write_text(f'#!/bin/sh\necho "{arch}"\n')
    # Log every argument, then emit nothing: the pipe into `tar` still has to
    # succeed, so the installer's own exit status stays meaningful.
    (bin_dir / "curl").write_text(
        f'#!/bin/sh\nfor a in "$@"; do echo "$a" >> "{url_log}"; done\n'
    )
    (bin_dir / "tar").write_text('#!/bin/sh\ncat > /dev/null\nexit 0\n')
    for shim in ("uname", "curl", "tar"):
        (bin_dir / shim).chmod(0o755)

    path = tmp_path / "install.sh"
    path.write_text(script)
    proc = subprocess.run(
        ["bash", str(path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, (
        f"the installer failed for arch={arch}:\n{proc.stdout}{proc.stderr}"
    )
    args = url_log.read_text().splitlines() if url_log.exists() else []
    urls = [a for a in args if a.startswith("https://")]
    assert len(urls) == 1, f"expected exactly one download URL, got {urls}"
    return urls[0]


@pytest.mark.parametrize(
    "arch, expected_asset_arch",
    [
        ("x86_64", "x64"),
        ("aarch64", "arm64"),
    ],
)
def test_the_installer_maps_uname_to_the_published_asset_arch(
    tmp_path, arch, expected_asset_arch
):
    url = _run_installer(tmp_path, arch)
    assert f"linux_{expected_asset_arch}.tar.gz" in url, (
        f"uname -m = {arch} must download the linux_{expected_asset_arch} asset, "
        f"got {url}"
    )
    # Stated negatively too: the bug is a passthrough, and a passthrough URL
    # contains the raw uname value.
    assert f"linux_{arch}.tar.gz" not in url, (
        f"{arch} was passed through unmapped — gitleaks publishes no such asset"
    )


def test_an_unknown_arch_falls_through_to_its_raw_name(tmp_path):
    """The `*)` arm is a deliberate best-effort passthrough, not a mapping.
    It will 404 on a real release, which is the correct outcome: failing on an
    architecture nobody runs is better than silently installing the wrong
    binary. Pinned so the fallback is not "mistakenly" turned into x64."""
    url = _run_installer(tmp_path, "riscv64")
    assert "linux_riscv64.tar.gz" in url


def test_the_gitleaks_version_is_pinned_and_consistent():
    """An unpinned installer would silently change what the weekly scan runs.
    The version appears twice in one URL (path segment and filename) and both
    must move together, or the download 404s."""
    script = _step_run_body("Install Gitleaks")
    versions = set(re.findall(r"gitleaks[_/]v?(\d+\.\d+\.\d+)", script))
    assert versions == {"8.30.1"}, (
        f"expected a single pinned gitleaks version, found {versions}"
    )
    assert "releases/download/v8.30.1/" in script
    assert "gitleaks_8.30.1_linux_" in script


# --------------------------------------------------------------------------
# report-only: a finding must never fail the build
# --------------------------------------------------------------------------
#
# The whole posture of this pipeline. 61 repos run it on a cron; if a finding
# reddened the build, the estate would wake up to 61 red repos and the scan
# would be switched off within a day. Each of the three scanners needs a
# DIFFERENT mechanism, which is why this is three assertions and not one:
# semgrep and trivy are steps that exit non-zero on findings
# (`continue-on-error`), while gitleaks exits 1 by design and is swallowed in
# the shell so the Annotations panel stays clean.


@pytest.mark.parametrize("job, step", [
    ("semgrep", "Run Semgrep (SAST)"),
    ("trivy", "Run Trivy (SCA + secrets + IaC/Dockerfile misconfig)"),
])
def test_the_scan_step_is_report_only(job, step):
    block = _job_block(job)
    anchor = block.index(f"- name: {step}")
    nxt = block.find("\n      - name: ", anchor + 1)
    step_text = block[anchor:nxt if nxt != -1 else len(block)]
    assert "continue-on-error: true" in step_text, (
        f"{job}'s scan step must be report-only — a finding must not fail the build"
    )


def test_the_gitleaks_detect_line_swallows_its_own_exit_code():
    """gitleaks exits 1 on a finding by design, so `continue-on-error` is not
    the mechanism here — the `|| true` is. Asserted on the detect line itself
    rather than anywhere in the job, because that is the command whose exit
    code matters."""
    detect = _step_run_command("Run Gitleaks (secrets in git history)")
    assert "gitleaks detect" in detect, f"not the detect command: {detect!r}"
    assert detect.rstrip().endswith("|| true"), (
        f"the gitleaks detect line must end in `|| true`, got {detect!r}"
    )


def test_the_gitleaks_detect_line_writes_the_sarif_it_is_scanned_for():
    """`|| true` only stays safe because the report is already written by the
    time the exit code is discarded. A detect line that lost its
    `--report-path` would swallow the failure AND produce nothing."""
    detect = _step_run_command("Run Gitleaks (secrets in git history)")
    assert "--report-format sarif" in detect
    assert "--report-path gitleaks.sarif" in detect


# --------------------------------------------------------------------------
# full git history is the gitleaks job's entire reason to exist
# --------------------------------------------------------------------------


def test_the_gitleaks_checkout_fetches_full_history():
    """Without `fetch-depth: 0` the checkout is shallow and gitleaks scans
    one commit — silently becoming a worse duplicate of Trivy's own secret
    scanner, which already covers the working tree. Nothing fails; the job
    just stops finding the thing it was added to find."""
    block = _job_block("gitleaks")
    checkout = block.index("actions/checkout@")
    nxt = block.find("- name:", checkout)
    assert "fetch-depth: 0" in block[checkout:nxt], (
        "the gitleaks job's checkout must set fetch-depth: 0"
    )


def test_only_the_gitleaks_job_needs_full_history():
    """Stated so the inverse is deliberate too: semgrep and trivy scan the
    working tree, and a full clone of a large repo on every scan is cost for
    nothing."""
    for job in ("semgrep", "trivy"):
        assert "fetch-depth" not in _job_block(job), (
            f"{job} scans the working tree and needs no history"
        )


# --------------------------------------------------------------------------
# SARIF upload: the categories, the always(), and the loud failure
# --------------------------------------------------------------------------


def test_the_three_sarif_categories_are_exactly_the_three_scanners():
    """A category is the identity of a finding stream in GitHub's code
    scanning UI. Renaming one does not fail anything — it silently starts a
    second history and orphans the first, so a repo looks like it has new
    findings and no old ones."""
    categories = re.findall(r"^\s*category:\s*(\S+)\s*$",
                            WORKFLOW.read_text(), re.MULTILINE)
    assert categories == ["semgrep", "trivy", "gitleaks"], (
        f"expected one category per job, in job order, got {categories}"
    )


def test_each_job_uploads_the_sarif_its_own_scanner_wrote():
    """Guards the copy-paste error this file's shape invites: three nearly
    identical blocks where only the filename and category differ."""
    for job in JOBS:
        block = _job_block(job)
        assert f"sarif_file: {job}.sarif" in block, f"{job} uploads the wrong SARIF"
        assert f"category: {job}" in block, f"{job} uploads under the wrong category"
        assert f"name: {job}-sarif" in block, f"{job}'s artifact name is wrong"


def test_the_sarif_upload_steps_run_even_when_the_scan_failed():
    """`continue-on-error` on the scan keeps the job green, but a step with
    no `if:` is still skipped once an earlier step has failed outright (a
    crash rather than a finding). Without `always()` a crashed scanner
    uploads nothing and reports nothing."""
    text = WORKFLOW.read_text()
    uploads = [m.start() for m in re.finditer(r"- name: Upload SARIF", text)]
    assert len(uploads) == 3, f"expected 3 upload steps, found {len(uploads)}"
    for pos in uploads:
        step = text[pos:text.index("uses:", pos)]
        assert "if: always() && inputs.sarif_upload" in step, (
            "every Upload SARIF step must be `if: always() && inputs.sarif_upload`"
        )


def test_the_sarif_artifact_steps_run_unconditionally():
    """The artifact is the ONLY output a `sarif_upload: false` caller gets,
    so it must not be gated on the upload flag it exists to compensate for,
    nor skipped when a scan step went red."""
    text = WORKFLOW.read_text()
    steps = [m.start() for m in re.finditer(r"- name: Retain SARIF artifact", text)]
    assert len(steps) == 3, f"expected 3 artifact steps, found {len(steps)}"
    for pos in steps:
        step = text[pos:text.index("uses:", pos)]
        assert "if: always()" in step
        assert "inputs.sarif_upload" not in step, (
            "the artifact must NOT be gated on sarif_upload — it is the fallback "
            "for callers that set it false"
        )


def test_the_sarif_upload_steps_are_never_best_effort():
    """When `sarif_upload` is true, a failed upload MUST stay red. A
    `continue-on-error` here produces a green weekly check on a repo whose
    Security tab silently stopped being fed — which is precisely the state
    `aw-app-research-search`'s variant shipped, and the reason the flag is an
    explicit per-caller boolean instead."""
    text = WORKFLOW.read_text()
    for pos in [m.start() for m in re.finditer(r"- name: Upload SARIF", text)]:
        nxt = text.find("\n      - name: ", pos + 1)
        step = text[pos:nxt if nxt != -1 else len(text)]
        assert "continue-on-error" not in step, (
            "an Upload SARIF step must not be continue-on-error: a silent upload "
            "regression is indistinguishable from a healthy scan"
        )


# --------------------------------------------------------------------------
# the inputs, and the self-hosted safety invariant that depends on them
# --------------------------------------------------------------------------


def test_the_reusable_declares_only_workflow_call():
    """THE SECURITY INVARIANT OF THIS FILE. The default `runs_on` is
    `[self-hosted, aw-baremetal]`, which is safe only because this workflow
    is reachable from `schedule`/`workflow_dispatch` on the caller — triggers
    that can run nothing but trusted default-branch code. A `pull_request`
    (or `pull_request_target`) trigger added here would make a fork PR on a
    public caller arbitrary code execution on the bare-metal host that runs
    the whole workspace. The sibling `ai-review-reusable.yml` hardcodes
    `ubuntu-latest` for exactly this reason; this file's answer is to have no
    such trigger at all."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln == "on:")
    triggers = []
    for ln in lines[start + 1:]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if not ln.startswith(" "):
            break
        if re.fullmatch(r"  \w[\w-]*:.*", ln):
            triggers.append(ln.strip().partition(":")[0])
    assert triggers == ["workflow_call"], (
        f"this file must declare ONLY workflow_call under `on:`, found {triggers}. "
        "A schedule here would never fire (triggers live on the caller), and a "
        "pull_request trigger would be RCE on the self-hosted default runner."
    )


def test_the_caller_checklist_forbids_a_pull_request_trigger():
    """The other half of the invariant above: the caller owns its triggers,
    so this file is the only place the prohibition can be written down."""
    text = WORKFLOW.read_text()
    checklist = text[text.index("── CALLER CHECKLIST"):]
    assert "NEVER ADD A `pull_request`" in checklist, (
        "the caller checklist must state the no-PR-trigger rule — it is the only "
        "thing standing between the self-hosted default and a fork-PR RCE"
    )


def test_every_job_derives_its_runner_from_the_input():
    """A job that hardcodes `runs-on` ignores `runs_on`, so a caller that
    cannot reach the self-hosted runners would have that one job queue
    forever while the others ran — the worst version of the bug, because the
    workflow looks half-healthy."""
    runs_on = re.findall(r"^\s+runs-on:\s*(.+?)\s*$", WORKFLOW.read_text(), re.MULTILINE)
    assert len(runs_on) == 3, f"expected one runs-on per job, found {runs_on}"
    assert set(runs_on) == {"${{ fromJSON(inputs.runs_on) }}"}, (
        f"every job must derive runs-on from the input, got {runs_on}"
    )


def test_the_runs_on_default_is_the_self_hosted_pair_as_json():
    """`fromJSON` is the consumption mechanism, so the default has to BE
    valid JSON — a bare `[self-hosted, aw-baremetal]` would throw at
    expression-evaluation time on every caller that omits the input."""
    import json
    text = WORKFLOW.read_text()
    block = text[text.index("      runs_on:"):text.index("      sarif_upload:")]
    m = re.search(r"^\s*default:\s*'(.+)'\s*$", block, re.MULTILINE)
    assert m, "runs_on has no default"
    assert json.loads(m.group(1)) == ["self-hosted", "aw-baremetal"]


def test_sarif_upload_is_a_boolean_defaulting_to_true():
    """Default true: the public-repo fleet behaviour is unchanged from the 61
    copies, and a private caller opts OUT explicitly. Defaulting to false
    would silently stop feeding the Security tab of every repo that works."""
    text = WORKFLOW.read_text()
    block = text[text.index("      sarif_upload:"):text.index("# Declared here AND")]
    assert re.search(r"^\s*type:\s*boolean\s*$", block, re.MULTILINE), (
        "sarif_upload must be typed boolean — a string 'false' is truthy in an `if:`"
    )
    assert re.search(r"^\s*default:\s*true\s*$", block, re.MULTILINE)
    assert re.search(r"^\s*required:\s*false\s*$", block, re.MULTILINE)


def test_there_are_exactly_two_inputs_and_no_secrets():
    """The scan needs only the automatic GITHUB_TOKEN. A `secrets:` block
    would hand a scheduled job on a self-hosted runner a credential it has no
    use for, and every added input is a new axis for 61 callers to drift on."""
    text = WORKFLOW.read_text()
    call = text[text.index("  workflow_call:"):text.index("permissions:\n  contents: read")]
    inputs = re.findall(r"^      (\w+):\s*$", call, re.MULTILINE)
    assert inputs == ["runs_on", "sarif_upload"], f"unexpected input set: {inputs}"
    assert "secrets:" not in call, "this workflow must declare no secrets"


# --------------------------------------------------------------------------
# the two permissions lists must not drift
# --------------------------------------------------------------------------
#
# For a reusable workflow the effective GITHUB_TOKEN is the INTERSECTION of
# the caller's `uses:`-job grant and this file's own top-level block, and once
# a `permissions:` key names any scope, every unnamed scope is forced to
# `none` rather than inherited (GitHub docs, `permissions`). So a scope has to
# appear on BOTH sides and the two lists have to be identical.
# `code-review-reusable.yml`'s v1.1.1 was cut specifically to fix a missing
# scope, added it to every caller and to its checklist, never touched its own
# block, and shipped no fix at all — just a better error message. Nothing
# compared the lists. This does, in both directions.


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
    run straight on into its own trailing prose.
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


def test_the_requested_scopes_are_exactly_what_the_scan_needs():
    """Stated explicitly, because the parity test above is satisfied by any
    two lists that happen to match — including two that are both wrong.

    `contents: read` is the checkout; `security-events: write` is the SARIF
    upload. Nothing else: this workflow opens no PR, writes no status, reads
    no check. In particular it needs none of the `pull-requests`/`issues`/
    `statuses`/`checks`/`actions` scopes the two sibling review workflows do.
    """
    assert _own_permissions() == {
        "contents": "read",
        "security-events": "write",
    }


def test_security_events_write_is_still_required_when_upload_is_off():
    """Deliberate: the checklist lists both scopes unconditionally, including
    for a `sarif_upload: false` caller. The flag is configuration a repo
    flips the day it gets Advanced Security, and a permissions block that has
    to be edited at the same moment is a second step someone will miss."""
    checklist = WORKFLOW.read_text()
    checklist = checklist[checklist.index("── CALLER CHECKLIST"):]
    assert "security-events: write` is still required" in checklist or \
           "`security-events: write` is still required" in checklist, (
        "the checklist must say the scope is still needed with upload off"
    )


# --------------------------------------------------------------------------
# supply-chain pinning
# --------------------------------------------------------------------------


def test_every_action_is_sha_pinned():
    """A tag is mutable. Every third-party action here runs on a SELF-HOSTED
    bare-metal runner by default, so a retagged action is code execution on
    the host that runs the whole workspace — a strictly worse blast radius
    than the same slip on a GitHub-hosted runner."""
    refs = re.findall(r"uses:\s*(\S+)@(\S+)", "\n".join(_uncommented_lines()))
    assert refs, "no action references found at all — extraction is broken"
    for action, ref in refs:
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"{action} must be pinned to a full 40-char commit SHA, got {ref!r}"
        )


def test_the_pinned_action_set_is_the_one_this_scan_needs():
    """Pins the SET, not just the format: a new `uses:` appearing here is a
    new third-party dependency running on bare metal, and should be a
    deliberate review rather than something the format test waves through."""
    actions = {a for a, _ in
               re.findall(r"uses:\s*(\S+)@(\S+)", "\n".join(_uncommented_lines()))}
    assert actions == {
        "actions/checkout",
        "actions/setup-python",
        "aquasecurity/trivy-action",
        "github/codeql-action/upload-sarif",
        "actions/upload-artifact",
    }, f"the action set changed: {sorted(actions)}"


def test_each_action_version_comment_survives_its_pin():
    """A 40-hex SHA is unreadable, so the trailing `# vN` comment is the only
    thing that says what is pinned. Losing it makes every future bump a
    guess."""
    for line in _uncommented_lines():
        if re.search(r"uses:\s*\S+@[0-9a-f]{40}", line):
            assert re.search(r"#\s*v\d", line), (
                f"pinned action line has no version comment: {line.strip()!r}"
            )
