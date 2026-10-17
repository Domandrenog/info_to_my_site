from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import git_publish


class GitPublishTests(unittest.TestCase):
    def make_country(self, root: Path) -> tuple[Path, list[str]]:
        country = root / "info" / "paises" / "asia" / "vietname"
        country.mkdir(parents=True)
        for filename in git_publish.FINAL_COUNTRY_FILES:
            (country / filename).write_text(filename, encoding="utf-8")
        relative = [
            path.relative_to(root).as_posix()
            for path in (country / filename for filename in git_publish.FINAL_COUNTRY_FILES)
        ]
        return country / "app-catalog.json", relative

    def test_country_catalog_must_be_in_the_standard_final_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "catalog.json"
            outside.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(
                git_publish.GitPublishError, "não está dentro de info/paises"
            ):
                git_publish.country_catalog_files(root, outside)

    def test_publish_stages_only_country_outputs_and_verifies_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, relative = self.make_country(root)
            calls: list[list[str]] = []
            staged = False
            committed = False

            def fake_git(_root: Path, command: list[str]) -> str:
                nonlocal staged, committed
                calls.append(command)
                if command == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return "main"
                if command[:4] == ["diff", "--cached", "--name-only", "-z"]:
                    return "\0".join(relative) + "\0" if staged and not committed else ""
                if command[:3] == ["status", "--porcelain", "--untracked-files=all"]:
                    return "?? " + relative[0] if not committed else ""
                if command[:2] == ["add", "--"]:
                    staged = True
                    return ""
                if command[:2] == ["commit", "-m"]:
                    committed = True
                    return "[main abc123] commit"
                if command == ["push", "origin", "main"]:
                    return ""
                if command == ["rev-parse", "HEAD"]:
                    return "a" * 40
                if command == [
                    "ls-remote",
                    "--exit-code",
                    "origin",
                    "refs/heads/main",
                ]:
                    return f"{'a' * 40}\trefs/heads/main"
                raise AssertionError(command)

            with patch.object(git_publish, "run_git", side_effect=fake_git):
                result = git_publish.publish_country_catalog(
                    root, catalog, "Vietnam"
                )

            add = next(command for command in calls if command[:2] == ["add", "--"])
            self.assertEqual(add[2:], relative)
            self.assertIn(
                ["commit", "-m", "feat: add Vietnam coin catalogue"], calls
            )
            self.assertTrue(result.committed)
            self.assertTrue(result.pushed)
            self.assertEqual(result.commit_sha, "a" * 40)

    def test_unrelated_staged_change_blocks_publication_before_add(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, _relative = self.make_country(root)

            def fake_git(_root: Path, command: list[str]) -> str:
                if command == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return "main"
                if command[:4] == ["diff", "--cached", "--name-only", "-z"]:
                    return "main.py\0"
                raise AssertionError(command)

            with (
                patch.object(git_publish, "run_git", side_effect=fake_git),
                self.assertRaisesRegex(
                    git_publish.GitPublishError, "staged fora do país"
                ),
            ):
                git_publish.publish_country_catalog(root, catalog, "Vietnam")

    def test_preflight_rejects_a_branch_other_than_main(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, _relative = self.make_country(root)

            with (
                patch.object(git_publish, "run_git", return_value="feature/test"),
                self.assertRaisesRegex(
                    git_publish.GitPublishError, "só é permitida em main"
                ),
            ):
                git_publish.validate_country_publication(root, catalog)


if __name__ == "__main__":
    unittest.main()
