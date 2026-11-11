"""Offline wrapper regression checks: python3 -m unittest discover -s searxng/tests."""

from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


class EntrypointWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.secrets = root / "secrets"
        self.secrets.mkdir()
        self.names = ("exa.key", "research-search.token", "searxng.secret")
        self.value = "test&pipe|back\\slash"
        for name in self.names:
            (self.secrets / name).write_text(self.value)
        template = root / "settings.yml"
        template.write_text("key: __EXA_API_KEY__\ntoken: __SCOUT_TOKEN__\n")
        self.output = root / "generated.yml"
        source = (Path(__file__).parents[1] / "config/entrypoint-wrapper.sh").read_text()
        self.script = (
            source.replace("/run/secrets/", str(self.secrets) + "/")
            .replace("/etc/searxng/settings.yml", str(template))
            .replace("/tmp/searxng-settings.yml", str(self.output))
            .replace("exec /usr/local/searxng/entrypoint.sh", 'test -n "$SEARXNG_SECRET"')
        )

    def run_wrapper(self):
        return subprocess.run(["sh", "-c", self.script], capture_output=True)

    def test_escapes_sed_replacement_characters(self):
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(
            self.output.read_text(), f"key: {self.value}\ntoken: {self.value}\n"
        )

    def test_private_permissions_on_new_and_existing_output(self):
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.output.chmod(0o644)
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_missing_or_empty_secrets_stop_startup(self):
        for name in self.names:
            with self.subTest(name=name):
                secret = self.secrets / name
                secret.unlink()
                self.assertNotEqual(self.run_wrapper().returncode, 0)
                secret.write_text("")
                self.assertNotEqual(self.run_wrapper().returncode, 0)
                secret.write_text(self.value)
