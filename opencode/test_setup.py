"""Offline setup regression tests: python3 -m unittest discover -s opencode."""

import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
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
        names = {"alloy", "tier-1", "tier-2", "tier-3", "tier-4"}
        agents = ROOT / "config/agents"
        self.assertEqual({p.stem for p in agents.iterdir()}, names)
        for name in names:
            self.assertEqual(os.readlink(agents / (name + ".md")),
                             "/opt/opencode/agents/" + name + ".md")
        self.assertEqual(CONFIG["plugins"], [{
            "package": "/opt/opencode/session-model-plugin",
            "options": {"agents": ["alloy"], "models": ["mistral/*"]},
        }])

    def test_only_mistral_provider_is_available(self):
        self.assertEqual(CONFIG["policies"], [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": "mistral", "effect": "allow"},
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


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.link = self.home / ".local/bin/opencode"
        self.link.parent.mkdir(parents=True)
        self.old = self.home / ".local/share/opencode-cli/old/bin/opencode"
        self.mockbin = self.home / "mockbin"
        self.mockbin.mkdir()
        npm = self.mockbin / "npm"
        npm.write_text(f"#!{sys.executable}\n" + '''
import os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
with (home / "npm-called").open("a") as log:
    log.write("called\\n")
if os.environ.get("MOCK_NPM_FAIL"):
    sys.exit(1)
prefix = pathlib.Path(sys.argv[sys.argv.index("--prefix") + 1])
binary = prefix / "bin/opencode"
binary.parent.mkdir(parents=True, exist_ok=True)
version = os.environ.get("MOCK_VERSION", "2.2.6")
binary.write_text("#!/bin/sh\\nprintf '%s\\\\n' 'opencode v" + version + "'\\n")
binary.chmod(0o755)
''')
        npm.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home),
                    "PATH": str(self.mockbin) + os.pathsep + os.environ["PATH"]}

    def install(self, **environment):
        return subprocess.run(["sh", str(ROOT / "install.sh")],
                              env={**self.env, **environment},
                              capture_output=True, text=True, timeout=10)

    def test_existing_regular_file_stops_before_npm(self):
        self.link.write_text("user executable")
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse((self.home / "npm-called").exists())
        self.assertEqual(self.link.read_text(), "user executable")

    def test_unmanaged_symlink_stops_before_npm(self):
        self.link.symlink_to("/unmanaged/opencode")
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse((self.home / "npm-called").exists())
        self.assertEqual(os.readlink(self.link), "/unmanaged/opencode")

    def test_wrong_version_preserves_existing_launcher(self):
        self.link.symlink_to(self.old)
        self.assertNotEqual(self.install(MOCK_VERSION="0.0.0").returncode, 0)
        self.assertEqual(os.readlink(self.link), str(self.old))

    def test_install_failure_preserves_existing_launcher(self):
        self.link.symlink_to(self.old)
        self.assertNotEqual(self.install(MOCK_NPM_FAIL="1").returncode, 0)
        self.assertEqual(os.readlink(self.link), str(self.old))

    def test_validated_install_and_idempotent_rerun(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "opencode v2.2.6")
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual((self.home / "npm-called").read_text(), "called\n")
        self.assertFalse(list(self.link.parent.glob(".opencode-install.*")))


if __name__ == "__main__":
    unittest.main()
