#!/usr/bin/env python3
"""Tests for the M-Dash Killer hook.

Run from the repository root:

    python3 -m unittest discover -s tests -v

The forbidden character is never typed literally in this file either. It is
built from its code point so the test suite cannot be corrupted by the very
hook it is testing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

EM = chr(0x2014)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(
    REPO_ROOT, "plugins", "m-dash-killer", "scripts", "mdash_killer.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("mdash_killer", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mdash = _load_module()


def run_hook(mode: str, payload: dict, env_extra: dict | None = None):
    """Run the hook exactly the way Claude Code runs it: JSON in, JSON out."""
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_OPTION_TERMINAL_GUARD", None)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, SCRIPT, mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return result


class TestSourceIsClean(unittest.TestCase):
    """The plugin must not carry the character it exists to remove."""

    def test_no_literal_em_dash_in_shipped_files(self):
        offenders = []
        for base, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
            for name in files:
                path = os.path.join(base, name)
                with open(path, "rb") as handle:
                    blob = handle.read()
                if EM.encode("utf-8") in blob:
                    offenders.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(offenders, [], f"literal em dash found in: {offenders}")


class TestReplaceEmDashes(unittest.TestCase):
    def assert_replaced(self, source: str, expected: str, count: int):
        new_text, replaced, _ = mdash.replace_em_dashes(source)
        self.assertEqual(new_text, expected)
        self.assertEqual(replaced, count)

    def test_untouched_when_absent(self):
        text = "nothing to see here\nsecond line\n"
        new_text, replaced, hits = mdash.replace_em_dashes(text)
        self.assertEqual(new_text, text)
        self.assertEqual(replaced, 0)
        self.assertEqual(hits, [])

    def test_spaced_in_the_middle(self):
        self.assert_replaced(f"the plan {EM} the real one {EM} works",
                             "the plan - the real one - works", 2)

    def test_glued_between_words_becomes_spaced(self):
        # The important one: "word-word" would read as a compound word and
        # become invisible. It has to stay spaced.
        self.assert_replaced(f"the plan{EM}the real one{EM}works",
                             "the plan - the real one - works", 2)

    def test_asymmetric_spacing(self):
        self.assert_replaced(f"left {EM}right", "left - right", 1)
        self.assert_replaced(f"left{EM} right", "left - right", 1)

    def test_extra_whitespace_collapses(self):
        self.assert_replaced(f"left   {EM}   right", "left - right", 1)
        self.assert_replaced(f"left\t{EM}\tright", "left - right", 1)

    def test_line_start_keeps_indentation(self):
        self.assert_replaced(f"    {EM} an item", "    - an item", 1)
        self.assert_replaced(f"{EM} an item", "- an item", 1)

    def test_line_end_leaves_no_trailing_space(self):
        self.assert_replaced(f"trailing {EM}", "trailing -", 1)
        self.assert_replaced(f"trailing{EM}", "trailing -", 1)

    def test_line_that_is_only_a_dash(self):
        self.assert_replaced(EM, "-", 1)
        self.assert_replaced(f"  {EM}", "  -", 1)

    def test_consecutive_dashes_collapse_but_are_all_counted(self):
        self.assert_replaced(f"a {EM}{EM} b", "a - b", 2)
        self.assert_replaced(f"a {EM} {EM} b", "a - b", 2)

    def test_crlf_line_endings_survive(self):
        source = f"first {EM} line\r\nsecond line\r\n"
        self.assert_replaced(source, "first - line\r\nsecond line\r\n", 1)

    def test_missing_final_newline_survives(self):
        self.assert_replaced(f"no newline at eof {EM} here",
                             "no newline at eof - here", 1)

    def test_final_newline_survives(self):
        self.assert_replaced(f"line {EM} one\n", "line - one\n", 1)

    def test_hits_report_correct_line_numbers(self):
        source = "\n".join([
            "clean line",
            f"dirty {EM} line",
            "clean again",
            f"another{EM}dirty one",
        ]) + "\n"
        _, replaced, hits = mdash.replace_em_dashes(source)
        self.assertEqual(replaced, 2)
        self.assertEqual([lineno for lineno, _ in hits], [2, 4])
        self.assertEqual(hits[0][1], "dirty - line")
        self.assertEqual(hits[1][1], "another - dirty one")

    def test_non_ascii_neighbours_do_not_shift_lines(self):
        source = f"emoji line\nrocket and {EM} dash\n"
        _, replaced, hits = mdash.replace_em_dashes(source)
        self.assertEqual(replaced, 1)
        self.assertEqual(hits[0][0], 2)

    def test_dash_inside_a_word_is_still_spaced(self):
        self.assert_replaced(f"re{EM}entry", "re - entry", 1)


class TestFilesHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, content: str, encoding: str = "utf-8") -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
        return path

    def test_rewrites_the_file_and_reports_lines(self):
        path = self.write("note.md", f"# Title\n\nA plan {EM} a good one.\n")
        result = run_hook("files", {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": path},
            "tool_response": {"filePath": path},
        })
        self.assertEqual(result.returncode, 0, result.stderr)

        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# Title\n\nA plan - a good one.\n")

        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("U+2014", context)
        self.assertIn("SHORT HYPHENS", context)
        self.assertIn("parentheses", context)
        self.assertIn(f"{path}:3", context)
        self.assertIn("A plan - a good one.", context)
        self.assertNotIn(EM, result.stdout)
        self.assertIn("note.md", payload["systemMessage"])

    def test_silent_when_the_file_is_clean(self):
        path = self.write("clean.txt", "nothing here\n")
        result = run_hook("files", {
            "tool_input": {"file_path": path},
            "tool_response": {"filePath": path},
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "nothing here\n")

    def test_falls_back_to_tool_input_path(self):
        path = self.write("only-input.txt", f"a {EM} b\n")
        result = run_hook("files", {"tool_input": {"file_path": path}})
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "a - b\n")

    def test_notebook_path_is_understood(self):
        path = self.write("nb.ipynb", f'{{"cell": "a {EM} b"}}\n')
        result = run_hook("files", {"tool_input": {"notebook_path": path}})
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"cell": "a - b"}\n')

    def test_relative_path_is_resolved_against_cwd(self):
        self.write("rel.txt", f"a {EM} b\n")
        result = run_hook("files", {
            "cwd": self.tmp.name,
            "tool_input": {"file_path": "rel.txt"},
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.tmp.name, "rel.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "a - b\n")

    def test_missing_file_is_not_an_error(self):
        result = run_hook("files", {
            "tool_input": {"file_path": os.path.join(self.tmp.name, "ghost.txt")},
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_no_path_at_all_is_not_an_error(self):
        result = run_hook("files", {"tool_name": "Write", "tool_input": {}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_binary_file_is_reported_not_mangled(self):
        path = os.path.join(self.tmp.name, "blob.bin")
        original = b"\x00\xff" + EM.encode("utf-8") + b"\x80\x81"
        with open(path, "wb") as handle:
            handle.write(original)

        result = run_hook("files", {"tool_input": {"file_path": path}})
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), original, "binary file must be untouched")
        context = result.stdout
        self.assertIn("not valid UTF-8", context)
        self.assertIn("NOTHING was changed", context)

    def test_oversized_file_is_reported_not_rewritten(self):
        path = os.path.join(self.tmp.name, "huge.txt")
        filler = "x" * 1024
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"top {EM} line\n")
            for _ in range(mdash.MAX_FILE_BYTES // 1024 + 2):
                handle.write(filler + "\n")

        result = run_hook("files", {"tool_input": {"file_path": path}})
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(path, encoding="utf-8") as handle:
            self.assertIn(EM, handle.read(), "oversized file must be untouched")
        self.assertIn("too large", result.stdout)
        self.assertIn("NOTHING was changed", result.stdout)

    def test_long_report_is_capped_and_says_so(self):
        lines = [f"line {n} {EM} tail" for n in range(mdash.MAX_REPORTED_LINES + 5)]
        path = self.write("many.txt", "\n".join(lines) + "\n")
        result = run_hook("files", {"tool_input": {"file_path": path}})
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("5 more changed line(s) not listed here", context)

    def test_broken_payload_fails_loudly(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "files"],
            input="this is not json",
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("m-dash-killer", result.stderr)

    def test_unknown_mode_is_rejected(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "nonsense"],
            input="{}",
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("usage:", result.stderr)


class TestBashHook(unittest.TestCase):
    COMMAND = f'git commit -m "Arreglar el parser {EM} otra vez"'

    def payload(self):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": self.COMMAND, "description": "commit"},
        }

    def test_off_by_default(self):
        result = run_hook("bash", self.payload())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_off_explicitly(self):
        result = run_hook("bash", self.payload(),
                          {"CLAUDE_PLUGIN_OPTION_TERMINAL_GUARD": "off"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_clean_command_passes_through_in_block_mode(self):
        result = run_hook("bash", {"tool_input": {"command": "git status"}},
                          {"CLAUDE_PLUGIN_OPTION_TERMINAL_GUARD": "block"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_block_mode_denies(self):
        result = run_hook("bash", self.payload(),
                          {"CLAUDE_PLUGIN_OPTION_TERMINAL_GUARD": "block"})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        specific = payload["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertIn("U+2014", specific["permissionDecisionReason"])
        self.assertIn("Arreglar el parser - otra vez",
                      specific["permissionDecisionReason"])
        self.assertNotIn(EM, result.stdout)

    def test_replace_mode_rewrites_the_command(self):
        result = run_hook("bash", self.payload(),
                          {"CLAUDE_PLUGIN_OPTION_TERMINAL_GUARD": "replace"})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        specific = payload["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", specific)
        self.assertEqual(
            specific["updatedInput"],
            {
                "command": 'git commit -m "Arreglar el parser - otra vez"',
                "description": "commit",
            },
            "the whole tool input must come back, not just the command",
        )
        context = specific["additionalContext"]
        self.assertIn("MECHANICAL", context)
        self.assertIn("do not rerun the command as it stands", context)
        self.assertIn('git commit -m "Arreglar el parser - otra vez"', context)
        self.assertNotIn(EM, result.stdout)

    def test_invalid_mode_blocks_loudly(self):
        result = run_hook("bash", self.payload(),
                          {"CLAUDE_PLUGIN_OPTION_TERMINAL_GUARD": "maybe"})
        self.assertEqual(result.returncode, 2, "a broken guard must not stay silent")
        self.assertIn("not a valid mode", result.stderr)

    def test_mode_is_case_and_space_insensitive(self):
        result = run_hook("bash", self.payload(),
                          {"CLAUDE_PLUGIN_OPTION_TERMINAL_GUARD": "  BLOCK "})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )


class TestPackaging(unittest.TestCase):
    """The manifests have to be valid JSON and point at files that exist."""

    def test_marketplace_points_at_the_plugin(self):
        path = os.path.join(REPO_ROOT, ".claude-plugin", "marketplace.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("name", data)
        self.assertIn("owner", data)
        entry = data["plugins"][0]
        self.assertEqual(entry["name"], "m-dash-killer")
        target = os.path.join(REPO_ROOT, entry["source"])
        self.assertTrue(os.path.isdir(target), f"missing plugin dir: {target}")

    def test_plugin_manifest_declares_its_hooks(self):
        path = os.path.join(
            REPO_ROOT, "plugins", "m-dash-killer", ".claude-plugin", "plugin.json"
        )
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["name"], "m-dash-killer")
        hooks_path = os.path.join(REPO_ROOT, "plugins", "m-dash-killer", data["hooks"])
        self.assertTrue(os.path.isfile(hooks_path), f"missing hooks: {hooks_path}")
        self.assertEqual(
            data["userConfig"]["terminal_guard"]["default"], "off",
            "the terminal guard ships switched off",
        )

    def test_hooks_reference_the_real_script(self):
        path = os.path.join(REPO_ROOT, "plugins", "m-dash-killer", "hooks", "hooks.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        events = data["hooks"]
        self.assertIn("PostToolUse", events)
        self.assertIn("PreToolUse", events)
        commands = [
            hook["command"]
            for event in events.values()
            for group in event
            for hook in group["hooks"]
        ]
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", command)
            self.assertIn("scripts/mdash_killer.py", command)
            self.assertIn('"', command, "the path must be quoted, it can contain spaces")


if __name__ == "__main__":
    unittest.main()
