#!/usr/bin/env python3
"""M-Dash Killer: hunt down the em dash (U+2014) in everything Claude writes.

Two entry points, selected by the first argument:

    mdash_killer.py files   PostToolUse on Write/Edit/MultiEdit/NotebookEdit.
                            Rewrites the file on disk and tells Claude which
                            lines it touched so it can fix the punctuation.

    mdash_killer.py bash    PreToolUse on Bash. Off by default. Set the plugin
                            option TERMINAL_GUARD to "block" or "replace" to
                            catch em dashes in commit messages, PR bodies and
                            anything else typed into a shell command.

Why a hook at all: a written rule ("never use an em dash") depends on the agent
remembering it, and that fails often enough that the character keeps landing in
files. A hook enforces the rule at write time, which is the only cheap moment;
after that the character has to be hunted down in a diff or, worse, in
production.

Why it reports instead of correcting silently: the replacement is mechanical.
A short hyphen is the right punctuation only some of the time; the sentence may
have wanted parentheses, a colon or a semicolon. Silently rewriting someone's
prose and saying nothing would trade one problem for a quieter one, so the hook
always says what it changed and where.

The character itself is never typed literally in this file. It is written as the
escape "\\u2014" so that the source stays readable and so that this script does
not trip over its own rule.

No third party dependencies: standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

# The target. Written as an escape on purpose, see the module docstring.
EM_DASH = "\u2014"
EM_DASH_BYTES = EM_DASH.encode("utf-8")

# A run of em dashes plus any spaces or tabs glued to either side. Newlines are
# excluded from the character classes so a run can never swallow a line break.
EM_DASH_RUN = re.compile(
    r"(?P<indent>[ \t]*)(?:" + EM_DASH + r"[" + EM_DASH + r" \t]*)"
)

# Files larger than this are reported but never rewritten. A hook that stalls
# the session on a huge file is worse than the character it removes.
MAX_FILE_BYTES = 5 * 1024 * 1024

# Upper bound on the per line report, so a pathological file cannot flood the
# context window. Whatever is dropped is stated out loud, never hidden.
MAX_REPORTED_LINES = 25
MAX_SNIPPET_CHARS = 200

VALID_GUARD_MODES = ("off", "block", "replace")

MECHANICAL_WARNING = (
    "That replacement is MECHANICAL, not a style decision: a short hyphen is the"
    " correct punctuation only some of the time."
)

PUNCTUATION_MENU = (
    "parentheses for a parenthetical aside, a colon when what follows explains or"
    " introduces a list, a semicolon when it separates two complete clauses, a"
    " comma for a short aside"
)

FIX_INSTRUCTIONS = (
    MECHANICAL_WARNING
    + " Your task now: go to each of those hyphens, read the whole sentence around"
    " it, and if the hyphen is not the punctuation that sentence needs, change it"
    " to the one that is: " + PUNCTUATION_MENU + "."
    " Leave the short hyphen only where it genuinely works."
)

BASH_FIX_INSTRUCTIONS = (
    MECHANICAL_WARNING
    + " Read the sentence this command carries before you rely on it. If the hyphen"
    " is not the punctuation that sentence needs, do not rerun the command as it"
    " stands: rewrite the wording first, with " + PUNCTUATION_MENU + "."
)

DO_NOT_REPEAT = (
    "And do not type U+2014 again: this hook is a safety net, not a licence."
)


# --------------------------------------------------------------------------
# Text surgery
# --------------------------------------------------------------------------


def _split_line_ending(raw: str) -> tuple[str, str]:
    """Split a line into its body and its line ending, preserving CRLF."""
    for ending in ("\r\n", "\n", "\r"):
        if raw.endswith(ending):
            return raw[: -len(ending)], ending
    return raw, ""


def _replacement(match: re.Match[str]) -> str:
    """Choose the hyphen shape from where the run sits inside its line.

    Middle of a line  ->  " - "  so it reads as punctuation.
    Start of a line   ->  "- "   keeping the original indentation.
    End of a line     ->  " -"   so no trailing space is left behind.

    The spacing matters. An em dash glued between two words is the normal
    English form (word[em dash]word), and collapsing it to "word-word" would
    fabricate a compound word that nobody can spot afterwards. A spaced hyphen
    still reads as punctuation and stays visible.
    """
    line = match.string
    at_line_start = match.start() == 0
    at_line_end = match.end() == len(line)

    if at_line_start and at_line_end:
        return match.group("indent") + "-"
    if at_line_start:
        return match.group("indent") + "- "
    if at_line_end:
        return " -"
    return " - "


def replace_em_dashes(text: str) -> tuple[str, int, list[tuple[int, str]]]:
    """Replace every em dash in ``text``.

    Returns the new text, how many em dash characters were replaced, and the
    (line number, new line body) pairs for every line that changed.
    """
    rebuilt: list[str] = []
    hits: list[tuple[int, str]] = []
    replaced = 0

    for lineno, raw in enumerate(text.splitlines(keepends=True), start=1):
        body, ending = _split_line_ending(raw)
        found = body.count(EM_DASH)
        if found:
            body = EM_DASH_RUN.sub(_replacement, body)
            replaced += found
            hits.append((lineno, body))
        rebuilt.append(body + ending)

    return "".join(rebuilt), replaced, hits


# --------------------------------------------------------------------------
# Hook plumbing
# --------------------------------------------------------------------------


def read_hook_input() -> dict[str, Any]:
    """Read the hook payload from stdin, or die loudly."""
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("no hook payload on stdin")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"hook payload is {type(payload).__name__}, expected object")
    return payload


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def _snippet(line: str) -> str:
    text = line.strip()
    if len(text) > MAX_SNIPPET_CHARS:
        text = text[:MAX_SNIPPET_CHARS] + " [...]"
    return text


def _format_hits(path: str, hits: list[tuple[int, str]]) -> str:
    shown = hits[:MAX_REPORTED_LINES]
    lines = [f"  {path}:{lineno}: {_snippet(body)}" for lineno, body in shown]
    dropped = len(hits) - len(shown)
    if dropped:
        lines.append(
            f"  [{dropped} more changed line(s) not listed here:"
            f" search the file for the remaining hyphens]"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry point: files
# --------------------------------------------------------------------------


def _target_path(payload: dict[str, Any]) -> str | None:
    """Work out which file the tool just wrote."""
    response = payload.get("tool_response")
    if isinstance(response, dict):
        for key in ("filePath", "file_path"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value

    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("file_path", "notebook_path", "filePath"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value

    return None


def cmd_files() -> int:
    payload = read_hook_input()

    target = _target_path(payload)
    if target is None:
        return 0

    if not os.path.isabs(target):
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            target = os.path.join(cwd, target)

    if not os.path.isfile(target):
        return 0

    # Cheap pre-check on raw bytes: no decoding, no rewriting, and it keeps the
    # hook invisible for the overwhelming majority of writes.
    try:
        with open(target, "rb") as handle:
            blob = handle.read()
    except OSError as error:
        emit(
            {
                "systemMessage": f"m-dash-killer: could not read {target}: {error}",
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"M-DASH KILLER: could not read {target} to check it for"
                        f" U+2014 ({error}). The file was left untouched, so it may"
                        " still contain the character. Check it yourself."
                    ),
                },
            }
        )
        return 0

    if EM_DASH_BYTES not in blob:
        return 0

    name = os.path.basename(target)

    if len(blob) > MAX_FILE_BYTES:
        size_mb = len(blob) / (1024 * 1024)
        emit(
            {
                "systemMessage": (
                    f"m-dash-killer: {name} contains U+2014 but is too large to"
                    f" rewrite ({size_mb:.1f} MB). Left untouched."
                ),
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"M-DASH KILLER: {target} contains U+2014 (the em dash) but"
                        f" it is {size_mb:.1f} MB, over the {MAX_FILE_BYTES} byte"
                        " limit this hook will rewrite, so NOTHING was changed."
                        " The character is still in that file. Remove it yourself,"
                        " or confirm the file is meant to keep it. " + DO_NOT_REPEAT
                    ),
                },
            }
        )
        return 0

    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as error:
        emit(
            {
                "systemMessage": (
                    f"m-dash-killer: {name} looks like it contains U+2014 but is not"
                    " valid UTF-8. Left untouched."
                ),
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"M-DASH KILLER: {target} contains the UTF-8 byte sequence for"
                        f" U+2014 but the file is not valid UTF-8 ({error}), so"
                        " NOTHING was changed. Check that file by hand before"
                        " assuming it is clean."
                    ),
                },
            }
        )
        return 0

    new_text, replaced, hits = replace_em_dashes(text)
    if not replaced:
        return 0

    # newline="" on both sides keeps CRLF files CRLF and LF files LF.
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(new_text)

    line_list = ", ".join(str(lineno) for lineno, _ in hits[:MAX_REPORTED_LINES])
    emit(
        {
            "systemMessage": (
                f"m-dash-killer: replaced {replaced} em dash(es) with hyphens in"
                f" {name} (line {line_list}). Claude was asked to check the"
                " punctuation."
            ),
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"M-DASH KILLER: found {replaced} occurrence(s) of U+2014 (the em"
                    f" dash) in {target} and replaced them with SHORT HYPHENS."
                    ' Each one is now a spaced hyphen " - ", or "- " at the start of a'
                    ' line and " -" at the end of one.\n'
                    + FIX_INSTRUCTIONS
                    + "\nThe changed lines, as they read right now:\n"
                    + _format_hits(target, hits)
                    + "\n"
                    + DO_NOT_REPEAT
                ),
            },
        }
    )
    return 0


# --------------------------------------------------------------------------
# Entry point: bash
# --------------------------------------------------------------------------


def guard_mode() -> str:
    """Read the terminal guard mode from the plugin option, or die loudly."""
    raw = os.environ.get("CLAUDE_PLUGIN_OPTION_TERMINAL_GUARD", "off")
    mode = raw.strip().lower()
    if mode not in VALID_GUARD_MODES:
        raise ValueError(
            f"terminal_guard is set to {raw!r}, which is not a valid mode."
            f" Use one of: {', '.join(VALID_GUARD_MODES)}."
        )
    return mode


def cmd_bash() -> int:
    # The mode is read before stdin so that a broken payload can never block a
    # command for someone who has this guard switched off.
    mode = guard_mode()
    if mode == "off":
        return 0

    payload = read_hook_input()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    command = tool_input.get("command")
    if not isinstance(command, str) or EM_DASH not in command:
        return 0

    new_command, replaced, _ = replace_em_dashes(command)

    if mode == "block":
        reason = (
            f"M-DASH KILLER: this command contains {replaced} occurrence(s) of"
            " U+2014 (the em dash), so it was blocked instead of run. Commit"
            " messages, PR bodies and anything else typed into a shell command are"
            " not covered by the file hook, which is why this guard exists.\n"
            "Rewrite the command with the punctuation the sentence actually needs:"
            " a short hyphen, " + PUNCTUATION_MENU + ".\n"
            f"For reference, the mechanical hyphen version would be: {new_command}\n"
            + DO_NOT_REPEAT
        )
        emit(
            {
                "systemMessage": (
                    f"m-dash-killer: blocked a command carrying {replaced} em dash(es)."
                ),
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }
        )
        return 0

    # mode == "replace": hand back the whole tool input with the command fixed.
    # No permission decision, so the command still goes through the normal
    # permission flow, just without the forbidden character.
    updated_input = dict(tool_input)
    updated_input["command"] = new_command
    emit(
        {
            "systemMessage": (
                f"m-dash-killer: rewrote {replaced} em dash(es) as hyphens in a"
                " terminal command."
            ),
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated_input,
                "additionalContext": (
                    f"M-DASH KILLER: the command you were about to run contained"
                    f" {replaced} occurrence(s) of U+2014 (the em dash). The command"
                    " was rewritten with SHORT HYPHENS before running and now reads:\n"
                    f"  {new_command}\n" + BASH_FIX_INSTRUCTIONS + " " + DO_NOT_REPEAT
                ),
            },
        }
    )
    return 0


# --------------------------------------------------------------------------


COMMANDS = {"files": cmd_files, "bash": cmd_bash}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in COMMANDS:
        sys.stderr.write(
            f"usage: {os.path.basename(argv[0])} {{{'|'.join(COMMANDS)}}}\n"
        )
        return 64
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as error:  # noqa: BLE001 - a hook must explain itself
        sys.stderr.write(f"m-dash-killer: {type(error).__name__}: {error}\n")
        # Exit 2 surfaces stderr to Claude. On PostToolUse it is a warning; on
        # PreToolUse it blocks the command, which is the correct outcome for a
        # guard that cannot do its job.
        sys.exit(2)
