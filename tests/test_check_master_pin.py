"""Tests for scripts/check_master_pin.py (the @master pin lint)."""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_master_pin", Path(__file__).parent.parent / "scripts" / "check_master_pin.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

def _wf(uses_line):
    return "jobs:\n  r:\n" + uses_line


R = "fredericowu/aw-shared-workflows/.github/workflows/code-review-reusable.yml"
OK = _wf(f"    uses: {R}@master\n")
TAG = _wf(f"    uses: {R}@v1.5.0\n")
TEK = ("jobs:\n  r:\n    steps:\n      - uses: "
       "'tekflox/aw-shared-workflows/.github/workflows/security-scan-reusable.yml@v1.2.0'\n")


def test_master_passes():
    assert list(mod.scan_text(OK)) == []


def test_tag_fails_with_line_number():
    (hit,) = mod.scan_text(TAG)
    assert hit == ("fredericowu", ".github/workflows/code-review-reusable.yml", "v1.5.0")


def test_tekflox_and_quoted_form_fail():
    (hit,) = mod.scan_text(TEK)
    assert hit[0] == "tekflox" and hit[2] == "v1.2.0"


def test_sha_and_branch_refs_fail():
    sha = OK.replace("@master", "@" + "a" * 40)
    assert len(list(mod.scan_text(sha))) == 1
    assert len(list(mod.scan_text(OK.replace("@master", "@main")))) == 1


def test_comments_ignored():
    assert list(mod.scan_text("# x\n" + OK.rstrip() + "  # was @v1.5.0\n")) == []
    assert list(mod.scan_text(_wf(f"    #uses: {R}@v1.5.0\n    uses: {R}@master\n"))) == []


def test_other_repos_ignored():
    assert list(mod.scan_text(_wf("    uses: actions/checkout@v7\n"))) == []


def test_quoted_key_and_folded_scalar_fail():
    quoted = _wf(f'    "uses": {R}@v1.5.0\n')
    folded = _wf(f"    uses: >-\n      {R}@v1.5.0\n")
    assert len(list(mod.scan_text(quoted))) == 1
    assert len(list(mod.scan_text(folded))) == 1


def test_uses_text_inside_run_script_is_not_a_reference():
    doc = ("jobs:\n  r:\n    steps:\n      - run: |\n"
           f"          cat <<EOF\n          uses: {R}@v1.5.0\n          EOF\n")
    assert list(mod.scan_text(doc)) == []


def test_cli_exit_codes(tmp_path, capsys):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "a.yml").write_text("jobs:\n  r:\n" + OK)
    assert mod.main([str(tmp_path)]) == 0
    (wf / "b.yaml").write_text("jobs:\n  r:\n" + TAG)
    assert mod.main([str(tmp_path)]) == 1
    assert "b.yaml" in capsys.readouterr().out
    (wf / "dir.yml").mkdir()  # a directory with a workflow suffix is skipped, not a crash
    assert mod.main([str(tmp_path)]) == 1
    (wf / "bad.yml").write_text("jobs: [unclosed\n")
    assert mod.main([str(tmp_path)]) == 2
    assert mod.main([str(tmp_path / "nope")]) == 2
    assert mod.main([]) == 2
