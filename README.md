# M-Dash Killer

A Claude Code plugin that removes the em dash (U+2014) from everything Claude
writes, and then tells Claude what it changed so the punctuation gets fixed
properly.

Telling an agent "never use an em dash" in a project instruction file does not
work reliably. The rule depends on the model remembering it, and it forgets
often enough that the character keeps landing in files. This plugin enforces the
rule at write time, which is the only cheap moment to catch it: after that the
character has to be hunted down in a diff or, worse, in production.

## What it does

Every time Claude writes or edits a file, the plugin scans it. If the file
contains U+2014, the plugin rewrites it in place and hands Claude a message
naming the file, the count, and every line it touched.

The message matters as much as the replacement. Swapping an em dash for a hyphen
is a mechanical move, and a hyphen is the right punctuation only some of the
time. The sentence may have wanted parentheses, a colon or a semicolon. So the
plugin never corrects silently: it reports what it did and asks Claude to read
each sentence and pick the punctuation that actually belongs there.

### Exactly what changes

An em dash becomes a **spaced** hyphen, in one of three shapes depending on
where it sits:

| Where the em dash is | Becomes | Example |
|---|---|---|
| Middle of a line | `" - "` | `a plan, the real one, works` |
| Start of a line | `"- "`, indentation preserved | `    - an item` |
| End of a line | `" -"` | `a trailing thought -` |

The spacing is deliberate. In English an em dash is normally glued between two
words, and collapsing `word` + U+2014 + `word` into `word-word` would invent a
compound word that nobody can spot later. A spaced hyphen still reads as
punctuation and stays visible.

Runs of spaces and tabs around the dash are collapsed, so no double spaces are
left behind. Line endings are preserved exactly: a CRLF file stays CRLF.

### The one case it gets wrong on purpose

Occasionally a glued em dash was standing in for a compound hyphen, and
`state-of-the-art` comes back as `state - of - the - art`, which reads as
broken. That is a deliberate trade. Guessing the other way would turn ordinary
punctuation into `plan-the real one-works`, which hides in plain sight, whereas
a wrongly spaced compound is impossible to miss and gets fixed on the spot. The
message the plugin hands Claude names this case explicitly, so it is caught in
the same pass.

## Install

```bash
/plugin marketplace add compota334/m-dash-killer
```

```bash
/plugin install m-dash-killer@compota334-plugins
```

To install from a local clone instead, point the marketplace at the directory
that contains `.claude-plugin/`:

```bash
/plugin marketplace add ./m-dash-killer
```

If the install summary says `Run /reload-plugins to activate.`, run that.

## The terminal guard (off by default)

The file hook only sees files. A commit message written with
`git commit -m "..."` or a PR body passed to `gh pr create` never touches disk as
a file, so it slips through.

The terminal guard closes that hole. It ships **switched off**, because blocking
or rewriting shell commands is intrusive and should be a deliberate choice. Turn
it on through the plugin's configuration, setting `terminal_guard` to one of:

| Mode | Behaviour |
|---|---|
| `off` | Default. The guard does nothing. |
| `block` | The command is refused before it runs. Claude is told which character it carried, is shown the mechanical hyphen version for reference, and has to rewrite the command itself with the right punctuation. |
| `replace` | The command is rewritten automatically, every em dash swapped for a hyphen, and then runs. Claude is told what changed and warned not to rely on the result without reading the sentence. |

`block` is the safer of the two: it never puts words in your mouth. `replace` is
the convenient one, at the cost of a mechanical punctuation choice landing in a
commit message that is awkward to amend later.

## What this plugin does not cover

Worth stating plainly, because a safety net that looks bigger than it is will
get trusted where it should not be:

- **Claude's chat replies are not touched.** Nothing here inspects the text
  Claude writes to you in conversation. That rule is still yours to enforce.
- **A legitimate em dash gets replaced too.** If a file must reproduce an em dash
  verbatim, because it is external data or a quotation, this plugin will change
  it anyway. The change is reported, never silent, so it is easy to put back.
- **Files above 5 MB are not rewritten.** If one of them contains the character,
  the plugin says so loudly and leaves the file alone rather than stalling the
  session.
- **Files that are not valid UTF-8 are not rewritten.** Same treatment: reported,
  never touched.

## Requirements

- Claude Code with plugin support.
- `python3` on PATH. Standard library only, nothing to install.

Tested on Linux with Claude Code 2.1.233 and Python 3.12.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers the replacement rules, the two hook entry points end to end
(real JSON in, real JSON out), the failure paths, and the manifests. It also
asserts that no file in this repository contains a literal em dash, so the
plugin cannot ship carrying the character it exists to remove.
