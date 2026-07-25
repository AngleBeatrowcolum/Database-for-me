from __future__ import annotations

import subprocess
from pathlib import Path

from app.integrations.git_publisher import GitPublisher


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, text=True, capture_output=True).stdout.strip()


def test_publisher_stages_only_summary_file(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    worktree = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    _git(worktree, "config", "user.email", "test@example.invalid")
    _git(worktree, "config", "user.name", "Test")
    (worktree / "README.md").write_text("private fixture\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "initial")
    _git(worktree, "branch", "-M", "main")
    _git(worktree, "remote", "add", "origin", str(remote))
    _git(worktree, "push", "-u", "origin", "main")
    summary = tmp_path / "draft.md"
    summary.write_text("# 虚构总结\n", encoding="utf-8")

    result = GitPublisher(
        repo_path=worktree,
        repo_slug="AngleBeatrowcolum/personal-weekly-summaries",
        visibility_checker=lambda slug: slug == "AngleBeatrowcolum/personal-weekly-summaries",
    ).publish(summary, iso_year=2026, iso_week=30)

    assert _git(worktree, "show", "--name-only", "--format=") == "summaries/2026/2026-W30.md"
    assert result.remote_commit_sha == _git(worktree, "rev-parse", "HEAD")
