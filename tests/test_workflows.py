"""
Structural checks on the .github/workflows/*.yml files themselves
(external review, accepted 2026-08-04: test gate, shared concurrency,
integrity check, backup artifact, retry-on-push).

Plain substring/ordering checks on the raw YAML text, not a full YAML
parse -- PyYAML isn't already a project dependency and these checks don't
need real YAML semantics, just "does step A's text appear before step B's,
and does the commit step's `if:` reference the right step ids" (this
project's "check stdlib/existing deps first" rule, ARCHITECTURE.md/
CLAUDE.md Engineering Rules).
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")

WORKFLOW_FILES = ["weekly_report.yml", "midweek_line_pull.yml", "post_game_audit.yml"]


def _read(name):
    with open(os.path.join(WORKFLOWS_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_all_three_workflows_share_one_concurrency_group_no_cancel():
    """Two overlapping runs pushing at once caused real push races -- all
    three scheduled workflows must serialize against EACH OTHER, not just
    against themselves, so the group name must match exactly across all
    three files."""
    groups = set()
    for name in WORKFLOW_FILES:
        text = _read(name)
        assert "concurrency:" in text
        assert "cancel-in-progress: false" in text
        for line in text.splitlines():
            if line.strip().startswith("group:"):
                groups.add(line.strip())
                break
    assert len(groups) == 1, f"concurrency group name differs across workflows: {groups}"


def test_test_suite_runs_before_any_mutation_step():
    for name in WORKFLOW_FILES:
        text = _read(name)
        assert "python -m pytest -q" in text
        assert "id: tests" in text

        tests_pos = text.index("id: tests")
        # Every fetch/generate/commit-adjacent step must appear AFTER the
        # test step in the file (steps run top-to-bottom).
        for marker in ("python data/fetch_stats.py", "python data/fetch_odds.py",
                       "python models/card_generator.py", "python models/post_game_audit.py",
                       "python models/pool_view.py", "python models/gambling_view.py",
                       "git commit", "git push"):
            if marker in text:
                assert text.index(marker) > tests_pos, f"{marker} appears before the test gate in {name}"


def test_mutation_steps_gated_on_test_outcome():
    for name in WORKFLOW_FILES:
        text = _read(name)
        assert "steps.tests.outcome == 'success'" in text
        # The commit step specifically must require both the test gate and
        # the integrity check to have passed.
        commit_idx = text.index("git commit")
        preceding = text[:commit_idx]
        # Nearest preceding `if:` line before the commit block's `run: |`.
        if_lines = [l for l in preceding.splitlines() if l.strip().startswith("if:")]
        assert if_lines, f"no if: condition found before the commit step in {name}"
        last_if = if_lines[-1]
        assert "steps.tests.outcome == 'success'" in last_if
        assert "steps.integrity.outcome == 'success'" in last_if


def test_integrity_check_runs_before_commit():
    for name in WORKFLOW_FILES:
        text = _read(name)
        assert "id: integrity" in text
        assert "db.integrity_check()" in text
        assert text.index("id: integrity") < text.index("git commit")


def test_backup_artifact_uploaded_before_any_mutation():
    for name in WORKFLOW_FILES:
        text = _read(name)
        assert "actions/upload-artifact@" in text  # version-agnostic on purpose -- don't pin a major here
        assert "retention-days: 14" in text
        artifact_pos = text.index("actions/upload-artifact@")
        for marker in ("python data/fetch_stats.py", "python data/fetch_odds.py",
                       "python models/post_game_audit.py", "python models/pool_view.py"):
            if marker in text:
                assert artifact_pos < text.index(marker), \
                    f"backup artifact step comes after a mutation step ({marker}) in {name}"


def test_commit_step_has_bounded_pull_rebase_retry():
    for name in WORKFLOW_FILES:
        text = _read(name)
        assert "git pull --rebase origin main" in text
        assert "max_attempts=5" in text
        assert "git push" in text


def test_install_dependencies_uses_requirements_file():
    """pytest has to actually be installed in CI for the test gate to mean
    anything -- `pip install requests` alone (the pre-review state) would
    leave it missing."""
    for name in WORKFLOW_FILES:
        text = _read(name)
        assert "pip install -r requirements.txt" in text
