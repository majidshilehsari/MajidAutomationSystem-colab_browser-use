"""Contract tests for the fixed-delay typing option in browser_control.sh."""

import os
import subprocess
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "browser_control.sh")


class BrowserControlTypingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="browser-control-test-")
        self.bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin_dir)
        self.log_path = os.path.join(self.tmp.name, "xdotool-args.txt")
        self._write_executable("xdpyinfo", "#!/bin/sh\nexit 0\n")
        self._write_executable(
            "xdotool",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$XDOTOOL_TEST_LOG\"\n",
        )
        self.env = dict(os.environ)
        self.env.update({
            "PATH": self.bin_dir + os.pathsep + self.env.get("PATH", ""),
            "DISPLAY": ":1",
            "XDOTOOL_TEST_LOG": self.log_path,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _write_executable(self, name, text):
        path = os.path.join(self.bin_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o755)

    def _run(self, *args):
        return subprocess.run([SCRIPT, *args], env=self.env, text=True,
                              capture_output=True, check=False)

    def test_requested_fixed_delay_is_passed_to_xdotool_without_shell_interpretation(self):
        result = self._run("type", "--delay-ms", "50", "hello; do not execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().splitlines(), [
                "type", "--clearmodifiers", "--delay", "50", "--", "hello; do not execute",
            ])

    def test_default_delay_is_fixed_at_fifteen_milliseconds(self):
        result = self._run("type", "hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().splitlines(), [
                "type", "--clearmodifiers", "--delay", "15", "--", "hello",
            ])

    def test_delay_outside_safe_range_is_rejected(self):
        result = self._run("type", "--delay-ms", "251", "hello")
        self.assertEqual(result.returncode, 2)
        self.assertIn("between 0 and 250 ms", result.stderr)
        self.assertFalse(os.path.exists(self.log_path), "xdotool must not be called on invalid input")


if __name__ == "__main__":
    unittest.main(verbosity=2)
