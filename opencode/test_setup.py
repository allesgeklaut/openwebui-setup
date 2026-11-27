"""Offline setup regression tests: python3 -m unittest discover -s opencode."""

import fnmatch
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config/opencode.json").read_text())


def shell_policy(command):
    """Check configured rule matching, not OpenCode's separate shell scanner."""
    result = "ask"
    for rule in CONFIG["permissions"]:
        pattern = rule["resource"]
        matches = fnmatch.fnmatchcase(command, pattern)
        if pattern.endswith(" *") and command == pattern[:-2]:
            matches = True
        if rule["action"] == "shell" and matches:
            result = rule["effect"]
    return result


class ConfigurationTests(unittest.TestCase):
    def test_normal_safe_diffs(self):
        for base in ["git diff", "git diff --no-ext-diff --no-textconv"]:
            for suffix in ["", " --stat", " --name-only", " --name-status", " --check",
                           " --cached", " --staged", " -- src/file.py",
                           " --cached -- src/file.py", " --staged -- src/file.py",
                           " --text", " --exit-code"]:
                with self.subTest(base=base, suffix=suffix):
                    self.assertEqual(shell_policy(base + suffix), "allow")

    def test_unreviewed_diff_modes_ask(self):
        base = "git diff --no-ext-diff --no-textconv"
        for command in ["git diff --output=/tmp/output",
                        base + " --output=/tmp/output", base + " --out=/tmp/output",
                        base + " --ext-diff", base + " --textconv",
                        base + " --no-index a b", base + " -O /tmp/order",
                        base + " --stat --output=/tmp/output",
                        "git -c diff.external=helper diff"]:
            with self.subTest(command=command):
                self.assertEqual(shell_policy(command), "ask")

    def test_flag_like_pathspec_conservatively_asks(self):
        self.assertEqual(shell_policy(
            "git diff --no-ext-diff --no-textconv -- --output=literal-filename"
        ), "ask")

    def test_no_implicit_cloud_default_or_host_dependencies(self):
        for key in ["model", "mcp"]:
            self.assertNotIn(key, CONFIG)
        self.assertNotIn("{file:", json.dumps(CONFIG))

    def test_only_selected_agents_and_model_plugin_are_shared(self):
        names = {"tier-1", "tier-2", "tier-3", "tier-4"}
        agents = ROOT / "config/agents"
        self.assertEqual({p.stem for p in agents.iterdir()}, names)
        for name in names:
            self.assertEqual(os.readlink(agents / (name + ".md")),
                             "/opt/opencode/agents/" + name + ".md")
        self.assertEqual(CONFIG["plugins"], [{
            "package": "/opt/opencode/session-model-plugin",
            "options": {"agents": ["build", "plan"], "models": ["opencode-go/*"]},
        }])

    def test_only_opencode_go_provider_is_available(self):
        self.assertEqual(CONFIG["policies"], [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": "opencode-go", "effect": "allow"},
        ])

    def test_safe_diff_disables_repository_helpers(self):
        for mode in ["external", "textconv"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                env = {**os.environ, "HOME": directory, "GIT_CONFIG_GLOBAL": "/dev/null",
                       "GIT_CONFIG_NOSYSTEM": "1"}

                def git(*args):
                    return subprocess.run(["git", "-C", directory, *args], env=env,
                                          capture_output=True, check=True, timeout=10)

                git("init", "--quiet")
                (work / "file.txt").write_text("before\n")
                (work / ".gitattributes").write_text("*.txt diff=example\n")
                git("add", "file.txt", ".gitattributes")
                (work / "file.txt").write_text("after\n")
                helper = work / "helper"
                marker = work / "helper-ran"
                helper.write_text("#!/bin/sh\nprintf invoked >> '" + str(marker) + "'\n"
                                  + ('cat "$1"\n' if mode == "textconv" else "exit 0\n"))
                helper.chmod(0o755)
                git("config", "diff.external" if mode == "external" else "diff.example.textconv",
                    str(helper))
                git("diff", "--no-ext-diff", "--no-textconv")
                self.assertFalse(marker.exists())
                # Positive control: prove the helper really would run without the flags.
                git("diff")
                self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
