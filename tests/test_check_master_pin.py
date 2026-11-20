"""Tests for scripts/check_master_pin.py (the @master pin lint)."""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_master_pin", Path(__file__).parent.parent / "scripts" / "check_master_pin.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

OK = "    uses: fredericowu/aw-shared-workflows/.github/workflows/code-review-reusable.yml@master\n"
TAG = "    uses: fredericowu/aw-shared-workflows/.github/workflows/code-review-reusable.yml@v1.5.0\n"
TEK = "  - uses: 'tekflox/aw-shared-workflows/.github/workflows/security-scan-reusable.yml@v1.2.0'\n"


def test_master_passes():
    assert list(mod.scan_text(OK)) == []


def test_tag_fails_with_line_number():
    (hit,) = mod.scan_text("name: x\n" + TAG)
    assert hit == (2, "fredericowu", ".github/workflows/code-review-reusable.yml", "v1.5.0")


def test_tekflox_and_quoted_form_fail():
    (hit,) = mod.scan_text(TEK)
    assert hit[1] == "tekflox" and hit[3] == "v1.2.0"


def test_sha_and_branch_refs_fail():
    sha = OK.replace("@master", "@" + "a" * 40)
    assert len(list(mod.scan_text(sha))) == 1
    assert len(list(mod.scan_text(OK.replace("@master", "@main")))) == 1


def test_comments_ignored():
    assert list(mod.scan_text("# " + TAG)) == []
    assert list(mod.scan_text(OK.rstrip() + "  # was @v1.5.0\n")) == []


def test_other_repos_ignored():
    assert list(mod.scan_text("    uses: actions/checkout@v7\n")) == []


def test_cli_exit_codes(tmp_path, capsys):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "a.yml").write_text("jobs:\n  r:\n" + OK)
    assert mod.main([str(tmp_path)]) == 0
    (wf / "b.yaml").write_text("jobs:\n  r:\n" + TAG)
    assert mod.main([str(tmp_path)]) == 1
    assert "b.yaml:3" in capsys.readouterr().out
    assert mod.main([str(tmp_path / "nope")]) == 2
    assert mod.main([]) == 2
