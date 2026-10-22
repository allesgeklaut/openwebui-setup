import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from render_config import render


ROOT = Path(__file__).resolve().parent


class PackagePinsTest(unittest.TestCase):
    def test_config_render_preserves_json_special_characters(self):
        secret = 'quotes" backslash\\ newline\n${NOT_ANOTHER_SUBSTITUTION}'
        value = {"env": {"TOKEN": "${TOKEN}"}, "args": ["${URL}", 123, True]}
        result = render(value, {"TOKEN": secret, "URL": "http://example.invalid"})
        self.assertEqual(json.loads(json.dumps(result))["env"]["TOKEN"], secret)
        self.assertEqual(result["args"], ["http://example.invalid", 123, True])

    def test_config_render_rejects_missing_variable(self):
        with self.assertRaises(KeyError):
            render({"token": "${MISSING}"}, {})

    def test_npx_rejects_non_exact_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "npm").mkdir()
            (root / "run-npx.sh").write_text((ROOT / "run-npx.sh").read_text())
            for version in ["latest", "^1.0.0", "1.0.0;false", None]:
                with self.subTest(version=version):
                    (root / "npm/package.json").write_text(
                        json.dumps({"dependencies": {"example": version}})
                    )
                    result = subprocess.run(
                        ["sh", str(root / "run-npx.sh"), "example"],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 64)

    def test_npx_uses_manifest_versions_and_preserves_arguments(self):
        packages = json.loads((ROOT / "npm/package.json").read_text())["dependencies"]
        with tempfile.TemporaryDirectory() as directory:
            npx = Path(directory) / "npx"
            npx.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            npx.chmod(0o755)
            env = dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}")
            for package, version in packages.items():
                with self.subTest(package=package):
                    self.assertRegex(version, r"^\d+\.\d+\.\d+$")
                    result = subprocess.run(
                        ["sh", str(ROOT / "run-npx.sh"), package, "/path with spaces"],
                        env=env, capture_output=True, text=True, check=True,
                    )
                    self.assertEqual(
                        result.stdout.splitlines(),
                        ["--yes", f"{package}@{version}", "/path with spaces"],
                    )

    def test_npx_rejects_missing_or_unlisted_package(self):
        for args in [[], ["unlisted-package"], ["__proto__"]]:
            with self.subTest(args=args):
                result = subprocess.run(
                    ["sh", str(ROOT / "run-npx.sh"), *args],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 64)

    def test_template_uses_tracked_manifests(self):
        servers = json.loads((ROOT / "template_config.json").read_text())["mcpServers"]
        for name in ["filesystem", "trilium", "searxng"]:
            self.assertEqual(servers[name]["command"], "/bin/sh")
            self.assertEqual(servers[name]["args"][0], "/stacks/webui/mcpo/run-npx.sh")
        for name in ["trainlocks", "deep-research"]:
            self.assertEqual(servers[name]["command"], "uvx")
            self.assertEqual(
                servers[name]["args"][:2],
                ["--with-requirements", "/stacks/webui/mcpo/requirements.txt"],
            )
        for requirement in (ROOT / "requirements.txt").read_text().splitlines():
            self.assertIsNotNone(re.fullmatch(r"[\w-]+==\d+\.\d+\.\d+", requirement))


if __name__ == "__main__":
    unittest.main()
