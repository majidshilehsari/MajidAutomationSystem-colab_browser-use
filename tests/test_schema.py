"""Tests for automation/schema.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import schema  # noqa: E402


def flow(**overrides):
    base = {
        "schema": 1,
        "name": "demo",
        "viewport": {"width": 1366, "height": 768},
        "steps": [
            {"type": "goto_url", "url": "https://example.com"},
            {"type": "click", "x": 100, "y": 200},
            {"type": "paste", "text": "cute panda"},
            {"type": "key", "keys": ["Return"]},
        ],
    }
    base.update(overrides)
    return base


class ValidateFlowTest(unittest.TestCase):
    def test_accepts_a_well_formed_flow(self):
        normalized, errors = schema.validate_flow(flow())
        self.assertEqual(errors, [])
        self.assertEqual(len(normalized["steps"]), 4)
        self.assertEqual(normalized["settings"]["defaultDelayAfterMs"], 350)

    def test_fills_in_ids_labels_and_defaults(self):
        normalized, errors = schema.validate_flow(flow())
        self.assertEqual(errors, [])
        first = normalized["steps"][1]
        self.assertTrue(first["id"])
        self.assertEqual(first["label"], "click 100,200")
        self.assertTrue(first["enabled"])
        self.assertFalse(first["requiresConfirmation"])

    def test_rejects_unknown_step_type(self):
        normalized, errors = schema.validate_flow(flow(steps=[{"type": "teleport", "x": 1}]))
        self.assertTrue(any("unknown type" in e for e in errors))
        self.assertEqual(normalized["steps"], [])

    def test_rejects_missing_required_field(self):
        _, errors = schema.validate_flow(flow(steps=[{"type": "click", "x": 10}]))
        self.assertTrue(any("missing required field 'y'" in e for e in errors))

    def test_rejects_coordinate_outside_the_desktop(self):
        _, errors = schema.validate_flow(flow(steps=[{"type": "click", "x": 2000, "y": 10}]))
        self.assertTrue(any("outside the 1366x768 desktop" in e for e in errors))

    def test_rejects_unexpected_field(self):
        _, errors = schema.validate_flow(flow(steps=[{"type": "wait", "ms": 10, "bogus": 1}]))
        self.assertTrue(any("unexpected field 'bogus'" in e for e in errors))

    def test_rejects_unknown_settings_field(self):
        _, errors = schema.validate_flow(flow(settings={"nope": True}))
        self.assertTrue(any("settings: unexpected field" in e for e in errors))

    def test_merges_settings_over_defaults(self):
        normalized, errors = schema.validate_flow(flow(settings={"repeat": 3, "stopOnError": False}))
        self.assertEqual(errors, [])
        self.assertEqual(normalized["settings"]["repeat"], 3)
        self.assertFalse(normalized["settings"]["stopOnError"])
        self.assertTrue(normalized["settings"]["screenshotAfterEachStep"] is False)

    def test_shell_steps_are_blocked_unless_enabled(self):
        _, errors = schema.validate_flow(flow(steps=[{"type": "shell", "command": "ls"}]))
        self.assertTrue(any("shell steps are disabled" in e for e in errors))
        normalized, errors = schema.validate_flow(
            flow(steps=[{"type": "shell", "command": "ls"}],
                 settings={"allowShellSteps": True}))
        self.assertEqual(errors, [])
        self.assertEqual(normalized["steps"][0]["command"], "ls")

    def test_rejects_url_without_scheme(self):
        _, errors = schema.validate_flow(flow(steps=[{"type": "goto_url", "url": "example.com"}]))
        self.assertTrue(any("must start with a scheme" in e for e in errors))

    def test_rejects_bad_button_and_bad_keys(self):
        _, errors = schema.validate_flow(flow(steps=[
            {"type": "click", "x": 1, "y": 1, "button": "sideways"},
            {"type": "key", "keys": []},
        ]))
        self.assertTrue(any("button must be one of" in e for e in errors))
        self.assertTrue(any("'keys' must be a non-empty list" in e for e in errors))

    def test_string_keys_are_promoted_to_a_list(self):
        normalized, errors = schema.validate_flow(flow(steps=[{"type": "key", "keys": "Return"}]))
        self.assertEqual(errors, [])
        self.assertEqual(normalized["steps"][0]["keys"], ["Return"])

    def test_rejects_unsupported_schema_version(self):
        _, errors = schema.validate_flow(flow(schema=99))
        self.assertTrue(any("unsupported version" in e for e in errors))

    def test_rejects_non_object_flow(self):
        _, errors = schema.validate_flow([1, 2, 3])
        self.assertEqual(errors, ["flow: must be a JSON object"])

    def test_duplicate_ids_are_made_unique(self):
        normalized, errors = schema.validate_flow(flow(steps=[
            {"id": "same", "type": "wait", "ms": 1},
            {"id": "same", "type": "wait", "ms": 2},
        ]))
        self.assertEqual(errors, [])
        ids = [s["id"] for s in normalized["steps"]]
        self.assertEqual(len(set(ids)), 2)

    def test_rejects_non_boolean_flags(self):
        _, errors = schema.validate_flow(flow(steps=[{"type": "wait", "ms": 1, "enabled": "yes"}]))
        self.assertTrue(any("'enabled' must be true or false" in e for e in errors))

    def test_labels_accept_double_quotes_and_persian(self):
        # Regression: labelFor() renders paste steps as: paste "سلام"
        normalized, errors = schema.validate_flow(flow(steps=[
            {"type": "paste", "text": "سلام", "label": 'paste "سلام"'},
            {"type": "type", "text": "hi", "label": 'type "hi"'},
        ]))
        self.assertEqual(errors, [])
        self.assertEqual(len(normalized["steps"]), 2)

    def test_flow_names_accept_persian_and_spaces(self):
        _, errors = schema.validate_flow(flow(name="جریان من"))
        self.assertEqual(errors, [])
        _, errors = schema.validate_flow(flow(name="bad/name"))
        self.assertTrue(any("name" in e for e in errors))

    def test_labels_reject_control_characters(self):
        _, errors = schema.validate_flow(flow(steps=[{"type": "wait", "ms": 1, "label": "<script>x"}]))
        self.assertTrue(any("must be plain text" in e for e in errors))

    def test_prompt_steps_drop_ui_noise(self):
        normalized, _ = schema.validate_flow(flow())
        compact = schema.flow_to_prompt_steps(normalized)
        self.assertNotIn("_index", compact[0])
        self.assertNotIn("enabled", compact[0])
        self.assertEqual(compact[0]["type"], "goto_url")


class DefaultLabelTest(unittest.TestCase):
    def test_labels_are_readable(self):
        cases = {
            "click 10,20": {"type": "click", "x": 10, "y": 20},
            "drag 1,2 -> 3,4": {"type": "drag", "x1": 1, "y1": 2, "x2": 3, "y2": 4},
            "key ctrl+l": {"type": "key", "keys": ["ctrl+l"]},
            "wait 500ms": {"type": "wait", "ms": 500},
            "scroll -3": {"type": "scroll", "amount": -3},
        }
        for expected, step in cases.items():
            self.assertEqual(schema.default_label(step), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
