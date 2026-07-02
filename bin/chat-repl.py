#!/usr/bin/env python3
"""chat REPL backend — a multi-turn Anthropic chat, rendered with mdcat.

This is the engine behind the zsh `chat()` function (see zsh/functions.zsh).
`chat()` stays thin: it picks the model id + system prompt, resolves the API
key, and exec's this script. Everything stateful lives here, in memory, for the
life of the process.

Design / isolation notes (mirroring the framing in functions.zsh):
  * Stdlib only. We talk to the Anthropic Messages API directly over HTTPS with
    urllib — no `anthropic` SDK, so there is no pip/venv to manage. The only
    external dependencies are python3 (always present) and `mdcat` (rendering).
  * Nothing persists. The conversation is a plain in-memory list, re-sent in
    full each turn and discarded when the process exits. We import `readline`
    for in-session line editing but NEVER write a history file, so relaunching
    is a clean, cheap reset. `/reset` wipes the context mid-session.
  * Single source of truth for prompts. The system prompt arrives via --system
    (the shared _ASK_SYS_* strings defined in the shell); it is never copied
    here, so there is no drift.
  * Multi-line input via the editor. libedit (the macOS readline) has no
    bracketed-paste support, so a pasted block submits line by line. `/edit`
    opens $EDITOR to compose or paste a whole message instead (see
    compose_in_editor); the buffer comes back as one turn.
  * The API key arrives via the ANTHROPIC_API_KEY env var, never on argv (which
    `ps` can see) and never on disk; we read it once and keep it in memory.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:  # Line editing (arrow keys, in-process history). Importing is enough;
    import readline  # noqa: F401  we deliberately never persist it to disk.
except ImportError:
    pass

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 8192   # a generous cap; you are billed for tokens used, not this.
TIMEOUT = 300       # seconds — non-streaming, so allow slow/long replies.

# Friendly labels for the chips/prompt; falls back to the raw id if unmapped.
MODEL_LABELS = {
    "claude-sonnet-5": "sonnet",
    "claude-fable-5": "fable",
    "claude-sonnet-4-6": "sonnet",
    "claude-opus-4-8": "opus",
}

# Per-turn styling. Each speaker gets a reverse-video "chip" — a padded label on a
# coloured background — so it's unmistakable who is talking; the assistant's reply
# is additionally bracketed by a coloured gutter bar down its left edge, so the
# block visibly starts and ends with the bar (the whole point: no more hunting for
# where a reply begins/ends). Colours are plain SGR codes from the 16-colour
# palette, so they track the terminal's own theme. Styling is emitted ONLY when
# stdout is a TTY (see _chip / render); piped output stays plain text.
YOU_SGR = "1;97;42"     # your turn: bold white on green
GUTTER = "▌"            # the bar glyph; one cell + one space = a 2-col gutter

# model id → (chip SGR, gutter-bar fg). Magenta = the escalated most-capable
# model (fable), blue = sonnet; both are distinct from your green chip so human
# and assistant never blur together. The fallback covers any unlisted model.
ACCENTS = {
    "claude-sonnet-5":   ("1;97;44", "94"),   # white-on-blue    chip, bright-blue    bar
    "claude-fable-5":    ("1;97;45", "95"),   # white-on-magenta chip, bright-magenta bar
    "claude-opus-4-8":   ("1;97;45", "95"),   # white-on-magenta chip, bright-magenta bar
    "claude-sonnet-4-6": ("1;97;44", "94"),   # white-on-blue    chip, bright-blue    bar
}
DEFAULT_ACCENT = ("1;97;45", "95")

# mdcat invocation: render Markdown read from stdin (`-`); `--local` so a reply
# can't make us fetch arbitrary remote image URLs. `--columns` reserves room for
# the 2-col gutter so wrapped lines never overflow. We CAPTURE mdcat's output
# (rather than letting it `--paginate`) so we can draw the gutter bar ourselves,
# then page the barred block through `less -RFX` (see render/_page).
MDCAT = shutil.which("mdcat")

# Heading restyle. A terminal can't resize text, so mdcat signals heading LEVEL with
# a run of ┄ glyphs (U+2504) — visually noisy "dots". We rebuild headings instead:
# `#`×level + UPPERCASE text, in bold Gruvbox bright orange (#fe8019). Orange is the
# one warm Gruvbox accent not already used by the green/blue/magenta speaker chips or
# the yellow links/rules, and it has strong contrast on the #282828 background. ┄ is
# heading-specific (code fences/tables use U+2500), so matching on it is safe.
HEAD_DASH = "┄"                       # U+2504 — mdcat's heading-level marker
HEAD_SGR = "1;38;2;254;128;25"        # bold + Gruvbox bright orange (truecolor)


def _ansi(code, s):
    """Wrap s in an ANSI SGR code, but only when stderr is a real terminal."""
    return f"\033[{code}m{s}\033[0m" if sys.stderr.isatty() else s


def _chip(label, sgr):
    """A padded, coloured badge ` label ` — only on a TTY; plain text otherwise."""
    return f"\033[{sgr}m {label} \033[0m" if sys.stdout.isatty() else label


class _Spinner:
    """An animated braille spinner + elapsed-seconds counter shown on stderr while
    the main thread is blocked waiting for a reply — the network call in `chat`, or
    stdin from `llm` in --render. A daemon thread does the animation, so it keeps
    ticking *through* the blocking call (CPython releases the GIL during socket/stdin
    reads). No-op when stderr isn't a TTY. Used as a context manager around the wait;
    __exit__ stops the thread and erases the line — on success, errors, and Ctrl-C.
    """
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, color=None):
        self._sgr = color or "2"          # model accent colour, or faint as fallback
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        start = time.monotonic()
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            elapsed = int(time.monotonic() - start)
            sys.stderr.write(f"\r\033[{self._sgr}m{frame} {elapsed}s\033[0m\033[K")
            sys.stderr.flush()
            i += 1
            self._stop.wait(0.1)          # ~10 fps; also the max stop latency
        sys.stderr.write("\r\033[K")      # erase the spinner line on the way out
        sys.stderr.flush()

    def __enter__(self):
        if sys.stderr.isatty():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
        return False


def call_api(key, model, system, messages):
    """POST the whole conversation and return the assistant's text.

    Raises RuntimeError with a human-readable message on any API/network error;
    the caller keeps the REPL alive and leaves history untouched.
    """
    payload = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages}
    if system:
        payload["system"] = system   # omit when empty; the API rejects a blank system
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The API returns a JSON error body; surface its message when present.
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        raise RuntimeError(f"API error {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}")

    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )
    if data.get("stop_reason") == "max_tokens":
        text += _ansi("2", f"\n\n[truncated at {MAX_TOKENS} tokens]")
    return text or _ansi("2", "[empty response]")


def _restyle_headings(text):
    """Rebuild mdcat's ┄-run headings as `#`×level + UPPERCASE in our accent colour.

    mdcat encodes heading level as N copies of ┄ (U+2504); we count them, drop
    mdcat's own (too-dark, sonnet-clashing) heading colour, and re-emit the line as
    `#`×level + the heading text uppercased, in bold orange. Only ┄-bearing lines are
    touched, so code fences (U+2500) and tables are untouched. Inline styling inside
    a heading is flattened to plain caps — a fine trade for headings.
    """
    if HEAD_DASH not in text:
        return text
    out = []
    for line in text.split("\n"):
        if HEAD_DASH not in line:
            out.append(line)
            continue
        level = line.count(HEAD_DASH)
        # Strip all ANSI and the ┄ run to recover the bare heading text.
        plain = re.sub(r"\033\[[0-9;]*m", "", line).replace(HEAD_DASH, "").strip()
        out.append(f"\033[{HEAD_SGR}m{'#' * level} {plain.upper()}\033[0m")
    return "\n".join(out)


def render(text, bar):
    """Render Markdown via mdcat, draw a gutter bar down the left, then page it.

    `bar` is the pre-coloured gutter string (e.g. a magenta ▌), or None when stdout
    isn't a TTY — in which case we skip the gutter and the pager and just print. Any
    mdcat/render hiccup falls back to the raw text, so a turn never dies on a
    formatting error.
    """
    out = text
    if MDCAT:
        cols = shutil.get_terminal_size((80, 24)).columns
        width = max(20, cols - 2)        # leave 2 cols for the "▌ " gutter
        try:
            # CLICOLOR_FORCE keeps mdcat's ANSI colour on through the capture pipe
            # (it would otherwise be free to drop colour when stdout isn't a tty).
            env = {**os.environ, "CLICOLOR_FORCE": "1"}
            proc = subprocess.run(
                [MDCAT, "--local", "--columns", str(width), "-"],
                input=text, text=True, capture_output=True, check=False, env=env,
            )
            if proc.returncode == 0:
                out = proc.stdout
        except Exception:
            pass                          # keep the raw text
    out = _restyle_headings(out)          # ┄-run headings → `#`×level UPPERCASE orange
    if bar:
        # Prefix EVERY line (blanks included) so the bar is one continuous gutter.
        out = "\n".join(f"{bar} {line}" for line in out.splitlines())
    _page(out)


def _page(text):
    """Show text through `less -RFX` on a TTY, else just print.

    `-R` preserves the ANSI colour, `-F` quits immediately when the reply fits the
    pane (so the REPL never blocks on short answers), `-X` skips the alternate
    screen so the rendered reply stays in the scrollback. less reads keystrokes from
    its own /dev/tty even though we feed it via a pipe, so paging still works.
    """
    if sys.stdout.isatty():
        try:
            subprocess.run(["less", "-RFX"], input=text, text=True, check=False)
            return
        except Exception:
            pass                          # fall through to a plain dump
    print(text)


def compose_in_editor(seed=""):
    """Open $VISUAL/$EDITOR on a temp file and return its contents, stripped.

    This is the escape hatch for multi-line input: libedit (the macOS readline)
    has no bracketed-paste support, so pasting a block into the prompt submits it
    line by line. Composing in a real editor sidesteps that entirely — paste,
    edit, save, quit, and the whole buffer comes back as one message.

    Returns the edited text (may be ""), or None if the editor couldn't launch
    or exited non-zero (e.g. vim `:cq`), which we treat as "cancel, send nothing"
    — the same convention git uses for its commit editor. The editor inherits the
    terminal, so full-screen editors (nvim, vim) take over and restore normally.
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    fd, path = tempfile.mkstemp(prefix="chat-", suffix=".md")  # .md → syntax hl
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(seed)
        try:
            proc = subprocess.run([*shlex.split(editor), path], check=False)
        except (OSError, ValueError) as e:
            print(_ansi("2", f"chat: could not launch editor '{editor}': {e}"),
                  file=sys.stderr)
            return None
        if proc.returncode != 0:
            print(_ansi("2", f"chat: editor exited {proc.returncode} — cancelled."),
                  file=sys.stderr)
            return None
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--model", required=True)
    p.add_argument("--system")                       # required for the REPL only
    p.add_argument("--render", action="store_true",  # render-from-stdin mode for `ask`
                   help="render Markdown from stdin (with badge/bar) and exit")
    p.add_argument("--oneshot", action="store_true",  # generate-and-print mode for `ask`
                   help="generate from --system + prompt args, print raw text, exit")
    p.add_argument("prompt", nargs="*",              # the prompt, in --oneshot mode
                   help="prompt text (--oneshot only; pass after `--`)")
    args = p.parse_args()

    label = MODEL_LABELS.get(args.model, args.model)
    chip_sgr, bar_fg = ACCENTS.get(args.model, DEFAULT_ACCENT)
    bar = f"\033[{bar_fg}m{GUTTER}\033[0m" if sys.stdout.isatty() else None

    # --render: the one-shot path for `ask`. Read the whole answer from stdin (it's
    # already generated — no API key or system prompt needed) and give it the same
    # model badge + gutter bar + restyled headings a chat reply gets, then exit. The
    # spinner covers the wait while the upstream `llm` is still streaming into us.
    if args.render:
        with _Spinner(bar_fg):
            text = sys.stdin.read()
        text = text.strip("\n")
        if not text:
            return 0
        if sys.stdout.isatty():
            print(_chip(label, chip_sgr))
        render(text, bar)
        return 0

    # --oneshot: the generate-and-print path for `ask`. POST a single Messages
    # request and write the raw answer to stdout; the interactive `ask` then pipes
    # it back through --render. Strip any ANSI the [truncated]/[empty response]
    # markers add so escapes never leak into the Markdown mdcat renders downstream.
    if args.oneshot:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            print("ask: no ANTHROPIC_API_KEY in the environment.", file=sys.stderr)
            return 1
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            return 0
        try:
            text = call_api(key, args.model, args.system or "",
                            [{"role": "user", "content": prompt}])
        except RuntimeError as e:
            print(f"ask: {e}", file=sys.stderr)
            return 1
        text = re.sub(r"\033\[[0-9;]*m", "", text)   # drop marker styling
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0

    if not args.system:
        print("chat: --system is required for the REPL.", file=sys.stderr)
        return 2

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("chat: no ANTHROPIC_API_KEY in the environment.", file=sys.stderr)
        return 1

    messages = []  # the entire conversation, re-sent each turn; in memory only.

    if not MDCAT:
        print("chat: mdcat not found — printing raw Markdown "
              "(brew install mdcat to render).", file=sys.stderr)
    print(_ansi("2", f"chat · {label} · 'exit'/'quit'/Ctrl-D to leave · "
                      "'/reset' clears · '/edit' composes in $EDITOR"),
          file=sys.stderr)

    # label / chip_sgr / bar were computed at the top of main() (shared with --render).
    # Both speakers get a chip on its OWN line, then their content below: ` you `
    # above your input, ` {label} ` above the reply. The you-chip is printed directly
    # (NOT baked into the readline prompt) for a hard reason — macOS libedit mishandles
    # the \001..\002 "non-printing" markers used to colour a prompt: it hoists the
    # bracketed escapes to the front, so a trailing reset lands BEFORE the label and
    # strips its styling (the old colour-in-prompt never actually rendered). Dropping
    # the markers fixes the colour but then libedit counts the escape bytes as width
    # and the cursor drifts on long/wrapping lines. Sidestepping both: the chip is a
    # plain print() (background renders correctly) and the readline prompt is a bare,
    # escape-free "❯ ", so its width is exact and editing stays precise.
    you_chip = _chip("you", YOU_SGR)
    model_chip = _chip(label, chip_sgr)
    prompt = "❯ "

    while True:
        print()                     # blank turn separator
        print(you_chip)             # ` you ` badge on its own line (bg renders here)
        try:
            line = input(prompt)
        except EOFError:            # Ctrl-D
            print()
            break
        except KeyboardInterrupt:   # Ctrl-C cancels the current line, not the REPL
            print()
            continue

        msg = line.strip()
        if not msg:
            continue
        if msg in ("exit", "quit"):
            break
        if msg == "/reset":
            messages.clear()
            print(_ansi("2", "context cleared."), file=sys.stderr)
            continue
        if msg == "/edit" or msg.startswith("/edit "):
            # Anything after "/edit " seeds the buffer (e.g. "/edit fix this:").
            edited = compose_in_editor(msg[6:] if msg[5:6] == " " else "")
            if edited is None:          # launch failed / cancelled (reason shown)
                continue
            msg = edited
            if not msg:
                print(_ansi("2", "empty — nothing sent."), file=sys.stderr)
                continue
            # The editor took over the screen; echo the composed message (under the
            # ` you ` badge already printed above this turn) with the same "❯ " marker
            # the live prompt uses, so the reply still has a visible question.
            sep = "\n" if "\n" in msg else " "
            print(f"❯{sep}{msg}")

        messages.append({"role": "user", "content": msg})
        try:
            # The spinner animates on stderr while call_api blocks; the `with` stops
            # and erases it on success, error, and Ctrl-C alike.
            with _Spinner(bar_fg):
                reply = call_api(key, args.model, args.system, messages)
        except RuntimeError as e:
            messages.pop()          # drop the unanswered turn → history stays valid
            print(_ansi("2", f"chat: {e}"), file=sys.stderr)
            continue
        except KeyboardInterrupt:
            messages.pop()
            print(_ansi("2", "cancelled."), file=sys.stderr)
            continue

        messages.append({"role": "assistant", "content": reply})
        print()                              # space between your line and the reply
        print(model_chip)                    # ` opus ` / ` sonnet ` header chip
        render(reply, bar)

    return 0


if __name__ == "__main__":
    sys.exit(main())
