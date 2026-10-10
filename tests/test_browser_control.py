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


class BrowserControlUnicodeTypingTest(unittest.TestCase):
    """`type` must never hand non-ASCII text to xdotool.

    xdotool type replays XTest keysyms, so it can only produce characters the
    active X keyboard layout maps to: Persian text arrives as nothing at all,
    or as the wrong glyph. The script routes such text through the clipboard
    instead. These tests pin both halves of that, including that the ASCII path
    and the clipboard are left exactly as they were.
    """

    PERSIAN = "سلام دنیا"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="browser-control-unicode-")
        self.bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin_dir)
        self.xdotool_log = os.path.join(self.tmp.name, "xdotool.log")
        self.xclip_log = os.path.join(self.tmp.name, "xclip.log")
        self._write_executable("xdpyinfo", "#!/bin/sh\nexit 0\n")
        # Append, not overwrite: the non-ASCII path calls xdotool more than once
        # and the sequence of calls is part of what is being asserted.
        self._write_executable(
            "xdotool",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$XDOTOOL_TEST_LOG\"\n"
            "printf -- '---\\n' >> \"$XDOTOOL_TEST_LOG\"\n",
        )
        self._write_executable(
            "xclip",
            "#!/bin/sh\nprintf 'ARGS:%s\\n' \"$@\" >> \"$XCLIP_TEST_LOG\"\n"
            "cat >> \"$XCLIP_TEST_LOG\"\n",
        )
        self.env = dict(os.environ)
        self.env.update({
            "PATH": self.bin_dir + os.pathsep + self.env.get("PATH", ""),
            "DISPLAY": ":1",
            "XDOTOOL_TEST_LOG": self.xdotool_log,
            "XCLIP_TEST_LOG": self.xclip_log,
            "LANG": "C.UTF-8",
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

    def _read(self, path):
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_persian_text_reaches_the_clipboard_verbatim(self):
        result = self._run("type", self.PERSIAN)
        self.assertEqual(result.returncode, 0, result.stderr)
        clipboard = self._read(self.xclip_log)
        self.assertIn("ARGS:-selection", clipboard)
        self.assertIn("clipboard", clipboard)
        self.assertIn(self.PERSIAN, clipboard)

    def test_persian_text_never_reaches_xdotool_type(self):
        self._run("type", self.PERSIAN)
        calls = self._read(self.xdotool_log)
        self.assertIn("ctrl+v", calls, "the paste keystroke was never sent")
        self.assertNotIn("\ntype\n", calls,
                         "xdotool type cannot produce Persian and must not be used")
        self.assertNotIn(self.PERSIAN, calls)

    def test_ascii_text_keeps_the_original_xdotool_type_path(self):
        self._run("type", "--delay-ms", "40", "hello world")
        first_call = self._read(self.xdotool_log).split("---")[0].split()
        self.assertEqual(
            first_call,
            ["type", "--clearmodifiers", "--delay", "40", "--", "hello", "world"])
        self.assertEqual(self._read(self.xclip_log), "",
                         "ASCII text must not clobber the operator's clipboard")

    def test_text_mixing_scripts_is_pasted(self):
        self._run("type", "order 1234 - شماره")
        self.assertIn("شماره", self._read(self.xclip_log))
        self.assertNotIn("\ntype\n", self._read(self.xdotool_log))

    def test_delay_validation_still_applies_to_non_ascii_text(self):
        result = self._run("type", "--delay-ms", "251", self.PERSIAN)
        self.assertEqual(result.returncode, 2)
        self.assertIn("between 0 and 250 ms", result.stderr)
        self.assertEqual(self._read(self.xclip_log), "",
                         "rejected input must not reach the clipboard")

    def test_shell_metacharacters_in_pasted_text_are_not_interpreted(self):
        marker = os.path.join(self.tmp.name, "should-not-exist")
        result = self._run("type", "سلام ; touch %s" % marker)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(marker),
                         "the pasted text was executed as a shell command")
        self.assertIn("touch", self._read(self.xclip_log),
                      "the text must still be pasted verbatim")


if __name__ == "__main__":
    unittest.main(verbosity=2)
