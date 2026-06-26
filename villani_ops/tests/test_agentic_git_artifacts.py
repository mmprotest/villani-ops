import subprocess
from pathlib import Path

from villani_ops.agentic.git_artifacts import (
    capture_git_patch,
    ensure_git_baseline,
    is_git_compatible_patch,
    patch_contains_internal_artifacts,
)
from villani_ops.core.acceptance import is_attempt_acceptance_eligible


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'; repo.mkdir()
    subprocess.run(['git','init'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git','config','user.email','t@example.invalid'], cwd=repo, check=True)
    subprocess.run(['git','config','user.name','T'], cwd=repo, check=True)
    (repo/'tracked.txt').write_text('one\n')
    (repo/'delete.txt').write_text('bye\n')
    subprocess.run(['git','add','-A'], cwd=repo, check=True)
    subprocess.run(['git','commit','-m','base'], cwd=repo, check=True, capture_output=True)
    return repo


def test_capture_git_patch_includes_untracked_modified_deleted_and_excludes_internal(tmp_path):
    repo = _repo(tmp_path)
    (repo/'tracked.txt').write_text('two\n')
    (repo/'delete.txt').unlink()
    (repo/'new.txt').write_text('new\n')
    (repo/'.villani').mkdir(); (repo/'.villani'/'context_state.json').write_text('{}')
    (repo/'.villani_code').mkdir(); (repo/'.villani_code'/'transcript.log').write_text('secret')
    (repo/'debug.log').write_text('log')

    res = capture_git_patch(repo, tmp_path/'patch.diff')
    text = Path(res.patch_path).read_text()

    assert res.has_changes
    assert set(res.changed_files) == {'tracked.txt', 'delete.txt', 'new.txt'}
    assert res.added_files == ['new.txt']
    assert res.deleted_files == ['delete.txt']
    assert res.modified_files == ['tracked.txt']
    assert 'diff --git' in text
    assert 'Added file:' not in text
    assert '.villani' not in text
    assert '.villani_code' not in text
    assert 'debug.log' not in text
    assert is_git_compatible_patch(res.patch_path)
    assert not patch_contains_internal_artifacts(res.patch_path)


def test_generated_patch_passes_git_apply_check_against_clean_base(tmp_path):
    repo = _repo(tmp_path)
    (repo/'new.txt').write_text('new\n')
    res = capture_git_patch(repo, tmp_path/'patch.diff')
    clean = tmp_path/'clean'
    subprocess.run(['git','clone',str(repo),str(clean)], check=True, capture_output=True)
    subprocess.run(['git','reset','--hard','HEAD'], cwd=clean, check=True, capture_output=True)
    chk = subprocess.run(['git','apply','--check',res.patch_path], cwd=clean, text=True, capture_output=True)
    assert chk.returncode == 0, chk.stderr


def test_invalid_custom_and_internal_patches_are_not_acceptance_eligible(tmp_path):
    bad = tmp_path/'bad.patch'
    bad.write_text('Added file: .villani/context_state.json\n--- /dev/null\n+++ b/.villani/context_state.json\n@@ -0,0 +1 @@\n+{}\n')
    attempt = {'status':'completed','scope':'candidate','exit_code':0,'patch_path':str(bad),'changed_files':['.villani/context_state.json'],'review':{'decision':'pass','recommended_action':'accept','passed':True}}
    eligible, blockers = is_attempt_acceptance_eligible(attempt)
    assert not eligible
    assert 'internal_artifacts_only' in blockers
    assert 'patch_contains_internal_artifacts' in blockers
    assert 'invalid_patch_format' in blockers


def test_ensure_git_baseline_allows_capture_in_copied_worktree(tmp_path):
    wt = tmp_path/'wt'; wt.mkdir()
    (wt/'a.txt').write_text('a\n')
    ensure_git_baseline(wt)
    (wt/'a.txt').write_text('b\n')
    (wt/'b.txt').write_text('new\n')
    res = capture_git_patch(wt, tmp_path/'p.diff')
    assert set(res.changed_files) == {'a.txt','b.txt'}
    assert Path(res.patch_path).read_text().startswith('diff --git')
