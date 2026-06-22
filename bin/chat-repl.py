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
import shlex
import shutil
import subprocess
import sys
import tempfile
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

# Friendly labels for the prompt line; falls back to the raw id if unmapped.
MODEL_LABELS = {
    "claude-sonnet-4-6": "sonnet",
    "claude-opus-4-8": "opus",
}

# mdcat invocation: render Markdown read from stdin (`-`); `--local` so a reply
# can't make us fetch arbitrary remote image URLs. `--paginate` routes the
# rendered output through MDCAT_PAGER (below). mdcat auto-detects terminal width
# and colour.
MDCAT = shutil.which("mdcat")
MDCAT_ARGS = ["--local", "--paginate", "-"]

# Pager for mdcat. `less -F` quits immediately when a reply fits the current pane,
# so the REPL never blocks on normal-length answers; only a reply taller than the
# pane opens in less (top-anchored, `q` to continue) instead of being dumped and
# leaving you scrolled to the bottom. Sizing is per-pane (less reads its own tty),
# so splits of any size just work. `-R` preserves mdcat's ANSI colour; `-X` skips
# the alternate screen so rendered replies stay in the scrollback.
MDCAT_PAGER = "less -RFX"


def _ansi(code, s):
    """Wrap s in an ANSI SGR code, but only when stderr is a real terminal."""
    return f"\033[{code}m{s}\033[0m" if sys.stderr.isatty() else s


def _hint_on():
    """Show a faint 'working' marker (nothing prints until a reply lands)."""
    if sys.stderr.isatty():
        sys.stderr.write(_ansi("2", "…"))
        sys.stderr.flush()


def _hint_off():
    """Erase the 'working' marker line."""
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


def call_api(key, model, system, messages):
    """POST the whole conversation and return the assistant's text.

    Raises RuntimeError with a human-readable message on any API/network error;
    the caller keeps the REPL alive and leaves history untouched.
    """
    body = json.dumps({
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
    }).encode("utf-8")
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


def render(text):
    """Pipe text through mdcat; print raw if mdcat is missing or errors."""
    if not MDCAT:
        print(text)
        return
    try:
        env = {**os.environ, "MDCAT_PAGER": MDCAT_PAGER}
        subprocess.run([MDCAT, *MDCAT_ARGS], input=text, text=True,
                       check=False, env=env)
    except Exception:
        print(text)  # never let a render hiccup kill the turn


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
    p.add_argument("--system", required=True)
    args = p.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("chat: no ANTHROPIC_API_KEY in the environment.", file=sys.stderr)
        return 1

    label = MODEL_LABELS.get(args.model, args.model)
    messages = []  # the entire conversation, re-sent each turn; in memory only.

    if not MDCAT:
        print("chat: mdcat not found — printing raw Markdown "
              "(brew install mdcat to render).", file=sys.stderr)
    print(_ansi("2", f"chat · {label} · 'exit'/'quit'/Ctrl-D to leave · "
                      "'/reset' clears · '/edit' composes in $EDITOR"),
          file=sys.stderr)

    # Coloured prompt; the \001..\002 markers tell readline the escape bytes are
    # non-printing, so it computes line width correctly when editing long input.
    # Keep the prompt a SINGLE line: a leading "\n" inside the prompt corrupts
    # libedit's (macOS) row/column model and garbles editing at the right margin
    # (cursor drift, backspace desync). The blank line between turns is printed
    # separately, at the top of the loop, so it never reaches readline.
    if sys.stdout.isatty():
        prompt = f"\001\033[1;36m\002{label} ❯ \001\033[0m\002"
    else:
        prompt = f"{label} ❯ "

    while True:
        print()                     # turn separator — NOT part of the prompt
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
            # The editor took over the screen, so echo the composed prompt to the
            # transcript; otherwise the reply would appear with no visible question.
            sep = "\n" if "\n" in msg else " "
            print(f"{label} ❯{sep}{msg}")

        messages.append({"role": "user", "content": msg})
        _hint_on()
        try:
            reply = call_api(key, args.model, args.system, messages)
        except RuntimeError as e:
            _hint_off()
            messages.pop()          # drop the unanswered turn → history stays valid
            print(_ansi("2", f"chat: {e}"), file=sys.stderr)
            continue
        except KeyboardInterrupt:
            _hint_off()
            messages.pop()
            print(_ansi("2", "cancelled."), file=sys.stderr)
            continue
        _hint_off()

        messages.append({"role": "assistant", "content": reply})
        render(reply)

    return 0


if __name__ == "__main__":
    sys.exit(main())
