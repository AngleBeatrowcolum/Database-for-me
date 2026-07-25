"""只向已验证的私有总结仓库提交单个 Markdown 文件。"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class PublicationError(RuntimeError):
    """发布前置条件或 Git 操作失败。"""


@dataclass(frozen=True)
class PublicationResult:
    relative_path: str
    remote_commit_sha: str


class GitPublisher:
    def __init__(self, *, repo_path: Path, repo_slug: str, visibility_checker: Callable[[str], bool] | None = None) -> None:
        self.repo_path = Path(repo_path)
        self.repo_slug = repo_slug
        self._visibility_checker = visibility_checker or self._is_private_with_gh

    def publish(self, source: Path, *, iso_year: int, iso_week: int) -> PublicationResult:
        if not self._visibility_checker(self.repo_slug):
            raise PublicationError("目标 GitHub 仓库未验证为私有仓库，已停止发布。")
        if not self.repo_path.is_dir() or not (self.repo_path / ".git").exists():
            raise PublicationError("总结仓库路径不是有效 Git 工作区。")
        source = Path(source)
        if not source.is_file():
            raise PublicationError("待发布的周总结草稿不存在。")
        self._run("diff", "--cached", "--quiet")
        relative = Path("summaries") / str(iso_year) / f"{iso_year}-W{iso_week:02d}.md"
        destination = self.repo_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        self._run("add", "--", str(relative))
        staged = self._run("diff", "--cached", "--name-only").splitlines()
        if staged != [relative.as_posix()]:
            self._run("reset", "--", str(relative))
            raise PublicationError("Git 暂存区包含非本次周总结文件，已停止提交。")
        self._run("commit", "-m", f"summary: {iso_year}-W{iso_week:02d}")
        branch = self._run("branch", "--show-current")
        if not branch:
            raise PublicationError("当前 Git 工作区未处于分支上。")
        self._run("push", "origin", f"HEAD:refs/heads/{branch}")
        local_sha = self._run("rev-parse", "HEAD")
        remote = self._run("ls-remote", "origin", f"refs/heads/{branch}").split()
        if not remote or remote[0] != local_sha:
            raise PublicationError("远程提交校验失败，未将任务标记为已归档。")
        return PublicationResult(relative.as_posix(), local_sha)

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(["git", "-C", str(self.repo_path), *args], check=True, text=True, capture_output=True)
        except FileNotFoundError as exc:
            raise PublicationError("未找到 Git，请安装 Git 后重试。") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout).strip()
            raise PublicationError(f"Git 操作失败：{detail or '未知错误'}") from exc
        return result.stdout.strip()

    @staticmethod
    def _is_private_with_gh(repo_slug: str) -> bool:
        try:
            result = subprocess.run(["gh", "repo", "view", repo_slug, "--json", "visibility", "--jq", ".visibility"], check=True, text=True, capture_output=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False
        return result.stdout.strip().upper() == "PRIVATE"
