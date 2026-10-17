#!/usr/bin/env python3
"""Publish one country's final catalogue outputs with a scoped Git commit."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from shutil import which


FINAL_COUNTRY_FILES = (
    "app-catalog.json",
    "availability-statistics.json",
    "coins-availability.xlsx",
)


class GitPublishError(RuntimeError):
    """Raised when a country catalogue cannot be published safely."""


@dataclass(frozen=True)
class GitPublishResult:
    committed: bool
    pushed: bool
    commit_sha: str


def is_wsl() -> bool:
    try:
        return "microsoft" in os.uname().release.lower()
    except AttributeError:
        return False


def wsl_unc_path(path: Path) -> str:
    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu").strip() or "Ubuntu"
    return f"//wsl.localhost/{distro}{path.resolve().as_posix()}"


def git_prefix(project_root: Path) -> list[str]:
    windows_git = which("git.exe") if is_wsl() else None
    if windows_git:
        repository = wsl_unc_path(project_root)
        return [
            windows_git,
            "-c",
            f"safe.directory={repository}",
            "-C",
            repository,
        ]
    return [which("git") or "git", "-C", str(project_root.resolve())]


def run_git(project_root: Path, command: list[str]) -> str:
    executable = [*git_prefix(project_root), *command]
    result = subprocess.run(
        executable,
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "erro desconhecido").strip()
        raise GitPublishError(f"{' '.join(command)} falhou: {details}")
    return result.stdout.strip()


def country_catalog_files(
    project_root: Path, catalog_path: Path
) -> tuple[Path, list[str]]:
    root = project_root.resolve()
    candidate = catalog_path if catalog_path.is_absolute() else root / catalog_path
    resolved = candidate.resolve()
    countries_root = (root / "info" / "paises").resolve()
    try:
        relative = resolved.relative_to(countries_root)
    except ValueError as exc:
        raise GitPublishError(
            "O catálogo importado não está dentro de info/paises/<continente>/<pais>."
        ) from exc
    if len(relative.parts) != 3 or relative.name != "app-catalog.json":
        raise GitPublishError(
            "A publicação automática exige info/paises/<continente>/<pais>/app-catalog.json."
        )

    country_dir = resolved.parent
    files = [country_dir / filename for filename in FINAL_COUNTRY_FILES]
    missing = [path.name for path in files if not path.is_file()]
    if missing:
        raise GitPublishError(
            "Faltam outputs finais do país para publicar: " + ", ".join(missing)
        )
    return country_dir, [path.relative_to(root).as_posix() for path in files]


def nul_paths(output: str) -> list[str]:
    return [value for value in output.split("\0") if value]


def ensure_no_unrelated_staged_changes(
    project_root: Path, allowed_paths: set[str]
) -> None:
    staged = set(
        nul_paths(run_git(project_root, ["diff", "--cached", "--name-only", "-z"]))
    )
    unrelated = sorted(staged - allowed_paths)
    if unrelated:
        raise GitPublishError(
            "Existem alterações staged fora do país; publicação automática cancelada: "
            + ", ".join(unrelated)
        )


def remote_main_sha(project_root: Path) -> str:
    output = run_git(
        project_root,
        ["ls-remote", "--exit-code", "origin", "refs/heads/main"],
    )
    parts = output.split()
    if len(parts) < 2 or parts[1] != "refs/heads/main":
        raise GitPublishError("Não foi possível confirmar o SHA de origin/main.")
    return parts[0]


def validate_country_publication(
    project_root: Path, catalog_path: Path
) -> tuple[Path, list[str]]:
    """Prove that a later scoped country commit cannot capture staged work."""

    root = project_root.resolve()
    country_dir, relative_files = country_catalog_files(root, catalog_path)
    branch = run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch != "main":
        raise GitPublishError(
            f"A publicação automática só é permitida em main; branch atual: {branch!r}."
        )
    ensure_no_unrelated_staged_changes(root, set(relative_files))
    return country_dir, relative_files


def publish_country_catalog(
    project_root: Path,
    catalog_path: Path,
    country: str,
    *,
    commit_message: str = "",
) -> GitPublishResult:
    """Commit only one country's final outputs, push, and verify origin/main."""

    root = project_root.resolve()
    _country_dir, relative_files = validate_country_publication(root, catalog_path)
    allowed_paths = set(relative_files)
    status = run_git(
        root,
        ["status", "--porcelain", "--untracked-files=all", "--", *relative_files],
    )
    committed = False
    if status:
        print("Git: a preparar apenas os outputs finais deste país:")
        for path in relative_files:
            print(f"- {path}")
        run_git(root, ["add", "--", *relative_files])
        ensure_no_unrelated_staged_changes(root, allowed_paths)
        staged_country = set(
            nul_paths(run_git(root, ["diff", "--cached", "--name-only", "-z"]))
        )
        if not staged_country:
            print("Git: os outputs finais já estão guardados num commit.")
        else:
            clean_country = " ".join(country.split()) or "novo país"
            message = commit_message.strip() or f"feat: add {clean_country} coin catalogue"
            print(f"Git: commit -m {message!r}")
            run_git(root, ["commit", "-m", message])
            committed = True
    else:
        print("Git: os outputs finais deste país já estão guardados num commit.")

    print("Git: push origin main")
    run_git(root, ["push", "origin", "main"])
    local_sha = run_git(root, ["rev-parse", "HEAD"])
    remote_sha = remote_main_sha(root)
    if remote_sha != local_sha:
        raise GitPublishError(
            f"origin/main ({remote_sha}) não corresponde ao HEAD local ({local_sha})."
        )
    remaining = run_git(
        root,
        ["status", "--porcelain", "--untracked-files=all", "--", *relative_files],
    )
    if remaining:
        raise GitPublishError(
            "Os outputs do país continuam com alterações depois do commit/push."
        )
    print(f"Git: publicação confirmada em {local_sha[:12]}.")
    return GitPublishResult(committed=committed, pushed=True, commit_sha=local_sha)
