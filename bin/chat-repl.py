#!/usr/bin/env python3
"""chat REPL backend — a multi-turn chat with one model, rendered with mdcat.

This is the engine behind the zsh `chat()` function (see zsh/functions.zsh).
`chat()` stays thin: it picks the system prompt, resolves the API key, and
exec's this script. Everything stateful lives here, in memory, for the
life of the process.

Design / isolation notes (mirroring the framing in functions.zsh):
  * Stdlib only. We talk to OpenRouter directly over HTTPS with urllib (OpenAI
    Chat Completions format) — no SDK, so there is no pip/venv to manage. The
    only external dependencies are python3 (always present), `mdcat`
    (rendering), and macOS's own `sips`/`osascript` (images).
  * One model, defined once: MODEL below. Chat, one-shot, and compaction all use
    it; there is no model selection anywhere.
  * Nothing persists. The conversation is a plain in-memory list, re-sent in
    full each turn and discarded when the process exits. We import `readline`
    for in-session line editing but NEVER write a history file, so relaunching
    is a clean, cheap reset. `/reset` wipes the context mid-session.
  * Single source of truth for prompts. The system prompt arrives via --system
    (the shared _ASK_SYS_* strings defined in the shell); it is never copied
    here, so there is no drift. The REPL shows it (a dim `system · …` block) at
    startup and again on `/system`, so the pre-prompt in effect is never hidden.
  * Latency. Every reply is followed by a dim `[latency Ns]` footer on stderr
    (API wall time); `--no-latency` hides it and `--stats` replaces it with the
    fuller [stats] line.
  * Multi-line input via the editor. libedit (the macOS readline) has no
    bracketed-paste support, so a pasted block submits line by line. `/edit`
    opens $EDITOR to compose or paste a whole message instead (see
    compose_in_editor); the buffer comes back as one turn.
  * Images. Drag an image file onto the prompt (Ghostty pastes its path) or queue
    the clipboard image with `/paste`. Each image is validated and downscaled
    locally with macOS `sips` (no Pillow), sent inline as a base64 data URL, and
    stays in the in-memory history so later turns can refer to it. Image
    requests are routed only to the IMAGE_PROVIDERS allowlist (see below).
  * The API key arrives via the OPENROUTER_API_KEY env var, never on argv (which
    `ps` can see) and never on disk; we read it once and keep it in memory.
"""

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.request

try:  # Line editing (arrow keys, in-process history). Importing is enough;
    import readline  # noqa: F401  we deliberately never persist it to disk.
except ImportError:
    pass

MODEL = "deepseek/deepseek-v4.1-flash-1017"   # the one model, via OpenRouter
MAX_TOKENS = 8192   # a generous cap; you are billed for tokens used, not this.
TIMEOUT = 300       # seconds — non-streaming, so allow slow/long replies.

# OpenRouter (OpenAI Chat Completions format). Routing prefs (see
# call_openrouter): lowest-latency provider first; the --stats provider= field
# shows who served each reply.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Images. MODEL accepts them, but OpenRouter exposes no per-provider "supports
# images" flag and some providers serving this model reject or silently ignore
# image parts. So any request carrying an image is pinned to this allowlist
# (provider.only, OpenRouter endpoint tags) of providers verified to handle
# them; text-only requests are unrestricted. Fallbacks stay within the list.
# Empty = image requests are refused locally, before any API call.
# The list was verified by probe on 2026-10-30 against
# deepseek/deepseek-v4.1-flash-1017 (Fireworks tags left out on purpose, as are
# tags that were unroutable). Re-probe if MODEL changes or image errors appear.
IMAGE_PROVIDERS = [
    "morph/fp8", "open-inference/fp4", "wafer/us", "reka", "wafer/mxfp4",
    "relace", "deepinfra/fp8/flex", "inference-net/us", "inference-net/fp8",
    "deepinfra/fp8", "cloudflare", "particle-ai", "streamlake/fp8", "decart/fp4",
    "deepinfra/fp8/priority", "makora/fp8", "ionstream", "alibaba", "modelrun/fp4",
    "digitalocean", "gmicloud/fp8", "cohere", "atlas-cloud/fp8", "novita/fp8",
    "phala", "modal", "baidu/fp8", "crusoe/fp8", "venice/fp8", "mistral/fp8",
    "nebius/fp8", "siliconflow/fp8", "together", "primeintellect", "baseten",
    "parasail/fp8",
]
MAX_IMAGE_SIDE = 1344                 # longer side is downscaled to this (sips -Z)
MAX_IMAGE_B64 = 5 * 1024 * 1024       # cap on one image's base64 payload

# Auto-compaction (REPL only). When the effective prompt size crosses the
# threshold, older turns are summarized by a cheap one-shot into a single
# synthetic exchange, keeping the recent tail intact. On by default; the
# `--no-compact` flag disables only the automatic trigger (manual `/compact`
# still works). The summarizer prompt lives HERE, not in functions.zsh — it's an
# internal implementation detail, not one of the user-facing _ASK_SYS_* prompts.
# The summarizer is the same MODEL as the chat itself.
COMPACT_MAX_TOKENS = 600
# Effective input tokens (see main loop). Triggers late: compacting too eagerly
# thrashes the provider's automatic prefix cache. An explicit --compact-threshold
# overrides it.
COMPACT_THRESHOLD = 35000
SUMMARIZER_SYS = (
    "Summarize this conversation transcript densely for use as context in a "
    "continuing conversation. Preserve: established facts and definitions, "
    "conclusions reached, the user's stated preferences or corrections, and any "
    "open questions. Omit pleasantries and rendering artifacts. Target 300-400 "
    "tokens. Output only the summary, no preamble."
)

# Per-turn styling. Each speaker gets a reverse-video "chip" — a padded label on a
# coloured background — so it's unmistakable who is talking; the assistant's reply
# is additionally bracketed by a coloured gutter bar down its left edge, so the
# block visibly starts and ends with the bar (the whole point: no more hunting for
# where a reply begins/ends). Your chip is a plain 16-colour SGR code, so it tracks
# the terminal's own theme; the model's chip and bar are fixed truecolor DeepSeek
# blue (like the truecolor HEAD_SGR below). Styling is emitted ONLY when
# stdout is a TTY (see _chip / render); piped output stays plain text.
YOU_SGR = "1;97;42"     # your turn: bold white on green
GUTTER = "▌"            # the bar glyph; one cell + one space = a 2-col gutter

# The model's chip label and colours — DeepSeek logo blue (#4D6BFE), distinct from
# your green chip so human and assistant never blur together.
LABEL = "deepseek"
CHIP_SGR = "1;97;48;2;77;107;254"   # bold white on DeepSeek blue chip
BAR_FG = "38;2;77;107;254"          # DeepSeek blue gutter bar + wait spinner

# mdcat invocation: render Markdown read from stdin (`-`); `--local` so a reply
# can't make us fetch arbitrary remote image URLs. `--columns` reserves room for
# the 2-col gutter so wrapped lines never overflow. We CAPTURE mdcat's output
# (rather than letting it `--paginate`) so we can draw the gutter bar ourselves,
# then page the barred block through `less -RFX` (see render/_page).
MDCAT = shutil.which("mdcat")

# Width measurement for the adaptive re-render in render(): mdcat undercounts the
# display width of some glyphs (arrows, superscripts), so we measure its output
# ourselves and re-render narrower until it fits the gutter budget. Strip SGR AND
# OSC 8 hyperlinks (mdcat emits them) so the URL payload isn't counted as width.
_MEASURE_STRIP = re.compile(
    r'\033\]8;;.*?(?:\033\\|\007)'   # OSC 8 hyperlink open/close (keep visible label)
    r'|\033\[[0-9;?]*[ -/]*[@-~]'    # any CSI (covers SGR ...m)
)


def _display_width(s):
    """True terminal display width of one line: ANSI/OSC stripped, combining
    marks 0, East-Asian wide/full 2, everything else 1 (ambiguous=1, matching
    how Ghostty renders arrows/superscripts)."""
    s = _MEASURE_STRIP.sub("", s)
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _max_width(out):
    return max((_display_width(l) for l in out.splitlines()), default=0)


def _mdcat(text, width):
    """Render markdown through mdcat at the given column width; return raw
    mdcat stdout, or the untouched text on any non-zero exit / exception."""
    try:
        env = {**os.environ, "CLICOLOR_FORCE": "1"}
        proc = subprocess.run(
            [MDCAT, "--local", "--columns", str(width), "-"],
            input=text, text=True, capture_output=True, check=False, env=env,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass
    return text


# Heading restyle. A terminal can't resize text, so mdcat signals heading LEVEL with
# a run of ┄ glyphs (U+2504) — visually noisy "dots". We rebuild headings instead:
# `#`×level + UPPERCASE text, in bold Gruvbox bright orange (#fe8019). Orange is the
# one warm Gruvbox accent not already used by the green (you) / DeepSeek-blue (model)
# speaker chips or the yellow links/rules, and it has strong contrast on the #282828
# background. ┄ is heading-specific (code fences/tables use U+2500), so matching on
# it is safe.
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


def _has_image(messages):
    """True if any message carries an image_url part (list content only)."""
    return any(isinstance(m["content"], list)
               and any(p.get("type") == "image_url" for p in m["content"])
               for m in messages)


def _wire_message(m):
    """Copy of a stored message with private (`_`-prefixed) part keys such as
    `_label` dropped, so OpenRouter never sees them. The stored message keeps
    them (compaction uses the label)."""
    if not isinstance(m["content"], list):
        return m
    parts = [{k: v for k, v in p.items() if not k.startswith("_")}
             for p in m["content"]]
    return {**m, "content": parts}


def call_openrouter(key, model, system, messages, max_tokens=MAX_TOKENS,
                    effort=None, strict=False):
    """POST the whole conversation to OpenRouter; return (assistant_text, usage).

    Speaks OpenAI Chat Completions format:
      * `system` becomes a leading {"role": "system"} message.
      * The reply lives at choices[0].message.content. A reasoning model's
        thinking may arrive alongside it (message.reasoning) and is deliberately
        never read — only the final answer is shown.
      * `effort` maps to reasoning.effort; None omits the block and the model
        uses its own default.
      * `strict=True` raises RuntimeError instead of returning marker text for a
        refused, truncated or empty reply — for callers (compaction) that can't
        use such a reply as real content.
      * No cache markers are sent — the serving providers prefix-cache
        automatically.
      * Routing: always `provider.sort = latency`. If ANY message in the list has
        an image part (older turns count, e.g. after compaction keeps one), the
        request is also limited to IMAGE_PROVIDERS. The file-parser plugin is
        always disabled so nothing is processed in OpenRouter's cloud.
      * usage comes back with OpenAI names (prompt_tokens / completion_tokens /
        prompt_tokens_details.cached_tokens).
    Raises RuntimeError with a human-readable message on any API/network error;
    the caller keeps the REPL alive and leaves history intact.
    """
    has_image = _has_image(messages)
    provider = {"sort": "latency"}
    if has_image:
        if not IMAGE_PROVIDERS:
            raise RuntimeError("images aren't enabled: IMAGE_PROVIDERS is empty "
                               "in chat-repl.py, so no image request can be routed.")
        provider["only"] = IMAGE_PROVIDERS
    msgs = [_wire_message(m) for m in messages]
    if system:
        msgs = [{"role": "system", "content": system}] + msgs
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": msgs,
        "provider": provider,
        "plugins": [{"id": "file-parser", "enabled": False}],
    }
    if effort:
        payload["reasoning"] = {"effort": effort}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
        },
        method="POST",
    )
    # Even with fallbacks on, a 429/5xx can slip through when the whole pool is
    # briefly saturated — and capacity usually frees up within seconds. Retry
    # those transient statuses a few times before giving up; the spinner is
    # already running, so the pause reads as a slow reply, not a stall.
    for attempt in range(4):                    # 3 retries: ~1s + 2s + 4s
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except Exception:
                pass
            if has_image and e.code in (404, 503):   # 503 only after retries ran out
                raise RuntimeError(
                    "no image-capable provider available; allowlisted: "
                    f"{', '.join(IMAGE_PROVIDERS)} (API error {e.code}: {detail})")
            raise RuntimeError(f"API error {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"network error: {e.reason}")

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""     # None on some refusals → ""
    finish = choice.get("finish_reason")
    refusal = message.get("refusal")
    note = refusal or "provider content filter stopped the response"
    if strict:
        # Raise instead of returning marker text, so a caller that needs real
        # content (compaction) can't mistake a marker for it. Same precedence
        # as the marker branches below.
        if refusal or finish == "content_filter":
            raise RuntimeError(f"reply refused: {note}")
        if finish == "length":
            raise RuntimeError(f"reply truncated at {max_tokens} tokens")
        if not text.strip():
            raise RuntimeError("empty reply")
    if refusal or finish == "content_filter":
        # OpenAI-format refusal signalling: an explicit message.refusal string
        # and/or finish_reason "content_filter". Marker alone when the content
        # is empty, appended notice when a partial answer exists.
        marker = _ansi("2", f"[refused: {note}]")
        text = f"{text}\n\n{marker}" if text.strip() else marker
    elif finish == "length":
        text += _ansi("2", f"\n\n[truncated at {max_tokens} tokens]")
    usage = data.get("usage") or {}
    # OpenRouter names the serving provider top-level in the response; tuck it
    # into usage so it rides along to --stats / /usage without a wider return.
    if data.get("provider"):
        usage["provider"] = data["provider"]
    return (text or _ansi("2", "[empty response]")), usage


def _print_stats(elapsed, usage, total=None):
    """One dim [stats] line on stderr — never stdout, so piped/tool output stays
    clean. Requests here are non-streaming, so latency is the whole request wall
    time and there is no tokens/sec figure. `total` is the full roundtrip —
    message submitted → styled reply ready for the screen — appended when the
    caller can measure it (the chat REPL can; the one-shot process can't see the
    downstream render stage, which prints its own total= line instead). cached=
    appears only when the provider reported a cached-token count. Purpose: tune
    --compact-threshold from data.
    """
    tin = usage.get("prompt_tokens", 0)
    tout = usage.get("completion_tokens", 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    line = f"[stats] latency={elapsed:.2f}s"
    if total is not None:
        line += f" total={total:.2f}s"
    line += f" in={tin} out={tout}"
    if cached is not None:
        line += f" cached={cached}"
    if usage.get("provider"):            # who served this reply
        line += f" provider={usage['provider'].lower()}"
    # \r + erase first: when `ask` pipes --oneshot into --render, the render
    # process's spinner may own the current stderr line — clear it so the stats
    # line never lands appended to a half-drawn spinner frame.
    prefix = "\r\033[K" if sys.stderr.isatty() else ""
    print(f"{prefix}{_ansi('2', line)}", file=sys.stderr)


def _print_system(system):
    """Print the system prompt to stderr as a dim, labelled, word-wrapped block
    (REPL startup and `/system`). Each line is styled on its own, so a wrapped
    block never leaves an SGR span open across a line break. Lines never break
    at a hyphen, so hyphenated words in the prompt stay whole."""
    label = "system · "
    lines = textwrap.wrap(system, width=min(shutil.get_terminal_size().columns, 100),
                          initial_indent=label, subsequent_indent=" " * len(label),
                          break_on_hyphens=False)
    for line in lines:
        print(_ansi("2", line), file=sys.stderr)


def _serialize_transcript(messages):
    """Flatten stored messages into a plain `You:` / `Assistant:` transcript for
    the summarizer. Content is a string, or a list of parts for a turn with
    images; each image becomes `[image: <label>]`, so the summarizer request is
    text-only (and so carries no image routing)."""
    lines = []
    for m in messages:
        who = "You" if m["role"] == "user" else "Assistant"
        content = m["content"]
        if not isinstance(content, str):
            content = " ".join(
                p.get("text", "") if p.get("type") == "text"
                else f"[image: {p.get('_label', 'image')}]"
                for p in content)
        lines.append(f"{who}: {content}")
    return "\n\n".join(lines)


def compact(key, messages, announce_skip=False):
    """Summarize older turns into one synthetic exchange, keeping the recent tail.

    Returns a NEW messages list on success, or None when it was skipped (too
    little history) or the summary request failed — in which case history must be
    left unchanged. Prints its own one-line notice/warning. `announce_skip` makes
    the too-short skip say so out loud (manual `/compact`); the automatic caller
    passes False so it stays quiet when it can't yet act.
    """
    # History strictly alternates user/assistant starting with user, so pairs is
    # just half the length. Keep the last 2 exchanges (4 messages); evict the rest.
    pairs = len(messages) // 2
    if pairs <= 3:
        if announce_skip:
            print(_ansi("2", "chat: not enough history to compact."), file=sys.stderr)
        return None
    keep, evict = messages[-4:], messages[:-4]
    # Same model at low reasoning effort (a summary needs no deep thinking).
    # Reasoning tokens share the completion budget, so the cap gets headroom on
    # top of the 300-400-token summary target.
    try:
        summary, _ = call_openrouter(key, MODEL, SUMMARIZER_SYS,
                                     [{"role": "user", "content": _serialize_transcript(evict)}],
                                     max_tokens=COMPACT_MAX_TOKENS * 2, effort="low",
                                     strict=True)   # marker text must never become the summary
    except RuntimeError as e:
        print(_ansi("2", f"chat: compaction failed ({e}); history unchanged."),
              file=sys.stderr)
        return None
    summary = re.sub(r"\033\[[0-9;]*m", "", summary).strip()   # drop any marker SGR
    compacted = [
        {"role": "user", "content":
            "[Summary of earlier conversation, compacted to save context]\n" + summary},
        {"role": "assistant", "content": "Understood — continuing from that summary."},
    ] + keep
    print(_ansi("2", f"[compacted: {len(evict)} turns → summary; kept last 2 exchanges]"),
          file=sys.stderr)
    return compacted


def _restyle_headings(text):
    """Rebuild mdcat's ┄-run headings as `#`×level + UPPERCASE in our accent colour.

    mdcat encodes heading level as N copies of ┄ (U+2504); we count them, drop
    mdcat's own (too-dark, clashing) heading colour, and re-emit the line as
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
    """Render Markdown via mdcat and draw a gutter bar down the left; RETURN the
    styled text (callers hand it to _page). Returning instead of printing lets
    callers stop the --stats roundtrip clock at "ready for the screen" — after
    all styling work, before the pager, whose dwell time belongs to the reader.

    `bar` is the pre-coloured gutter string (e.g. a blue ▌), or None when
    stdout isn't a TTY — then the gutter is skipped (and _page just prints). Any
    mdcat/render hiccup falls back to the raw text, so a turn never dies on a
    formatting error.
    """
    out = text
    if MDCAT:
        cols = shutil.get_terminal_size((80, 24)).columns
        budget = max(20, cols - 2)        # leave 2 cols for the "▌ " gutter
        out = _mdcat(text, budget)
        if _max_width(out) > budget:
            # mdcat overshot the width (it undercounts some glyphs). Re-render
            # narrower, shrinking by the measured overshoot, until it fits.
            budget_render, width, cand = out, budget, out
            for _ in range(3):
                width = max(20, width - (_max_width(cand) - budget))
                cand = _mdcat(text, width)
                if _max_width(cand) <= budget:
                    out = cand
                    break
                if width == 20:
                    break
            else:
                out = budget_render       # never converged (e.g. a wide table)
            if _max_width(out) > budget:  # covers the break-on-floor path too
                out = budget_render       # unwrappable → today's behavior, no worse
    out = _restyle_headings(out)          # ┄-run headings → `#`×level UPPERCASE orange
    if bar:
        # Prefix EVERY line (blanks included) so the bar is one continuous gutter.
        out = "\n".join(f"{bar} {line}" for line in out.splitlines())
    return out


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


# --file support (ask/chat). Extensions we explicitly refuse rather than try to
# decode as text — the model behind these extensions is media, not prose, so a
# UTF-8 read would just fail with a confusing "invalid byte" error; naming the
# media type up front is clearer. --file never ingests media; in the chat REPL,
# images go in by drag-and-drop or /paste instead (see load_image).
BINARY_MEDIA_EXTS = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".heic": "image", ".bmp": "image", ".tiff": "image",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".aac": "audio",
    ".flac": "audio", ".ogg": "audio",
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".avi": "video",
    ".webm": "video",
}


def read_file_context(path):
    """Read `path` and wrap it as a <file> context block for --file.

    Text/Markdown/code extensions (and anything unrecognized) are read as
    strict UTF-8 — binary files reliably fail that decode, which is the signal
    we want. PDFs go through the LOCAL `pdftotext` (poppler) — no cloud
    parsing, no OCR, per the file-flag spec; a scanned/image-heavy PDF still
    "succeeds" (near-empty text) but earns a stderr warning so the caller isn't
    silently sending nothing. Known image/audio/video extensions are rejected
    up front with a clear message instead of an opaque decode error.

    Raises RuntimeError with a human-readable message on any failure; callers
    (in main()) already have the RuntimeError → "ask:"/"chat:" prefix
    convention, so nothing here talks to stderr except the near-empty-PDF
    warning, which is a non-fatal heads-up, not an error.
    """
    if not os.path.isfile(path):
        # Covers both "doesn't exist" and "it's a directory" — one message,
        # since the fix is the same either way: point at a real file.
        raise RuntimeError(f"not a readable regular file: {path}")

    ext = os.path.splitext(path)[1].lower()
    media = BINARY_MEDIA_EXTS.get(ext)
    if media:
        raise RuntimeError(f"{media} files aren't supported yet: {path}")

    if ext == ".pdf":
        if not shutil.which("pdftotext"):
            raise RuntimeError(
                "pdftotext not found — install poppler: brew install poppler")
        proc = subprocess.run(["pdftotext", path, "-"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"pdftotext failed: {proc.stderr.strip()}")
        text = proc.stdout
        if len(re.sub(r"\s", "", text)) < 50:
            # Not fatal — a thin/scanned PDF still yields whatever text layer
            # exists (maybe none); let the caller decide whether that's useful.
            # "warning:" rather than an ask:/chat: prefix — this helper serves
            # both modes and can't know which one is running.
            print(_ansi("2", f"warning: {path} extracted almost no text — it "
                             "may be scanned/image-heavy; OCR isn't supported."),
                  file=sys.stderr)
    else:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            raise RuntimeError(
                f"{path} isn't valid UTF-8 text — binary files aren't supported")
        except OSError as e:
            raise RuntimeError(f"can't read {path}: {e}")

    name = os.path.basename(path).replace('"', '\\"')
    return f'<file name="{name}">\n{text}\n</file>'


# --- images (chat only) ---------------------------------------------------------
# Pipeline: detect (drag-and-drop path / clipboard) → load_image (validate, convert,
# downscale, all via macOS `sips` in a throwaway temp dir) → a dict that rides in
# the message as a base64 data URL. Originals are never modified. Failures raise
# RuntimeError, like read_file_context, so the REPL reports them and sends nothing.

# Leading bytes → kind. Used both to recognise a dropped file as an image (so a
# path to anything else stays plain text) and to identify sips' output.
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm",
                b"hevs", b"mif1", b"msf1"}
FILE_KINDS = {"png", "jpeg", "gif", "webp", "heic"}   # accepted as files; TIFF only
                                                       # arrives via the clipboard
_MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def _sniff_image(path):
    """Image kind from magic bytes ('png', 'jpeg', 'gif', 'webp', 'heic', 'tiff'),
    or None if it isn't one (or can't be read)."""
    try:
        with open(path, "rb") as f:
            h = f.read(16)
    except OSError:
        return None
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "webp"
    if h[4:8] == b"ftyp" and h[8:12] in _HEIF_BRANDS:
        return "heic"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return None


def _image_size(path, label):
    """(width, height) via sips. sips exits 0 even on a corrupt file (the fields
    just come back `<nil>`), so validate by parsing, not by return code."""
    proc = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                          capture_output=True, text=True)
    dims = dict(re.findall(r"pixel(Width|Height): (\d+)", proc.stdout))
    if len(dims) != 2:
        raise RuntimeError(f"can't read image (corrupt or unsupported): {label}")
    return int(dims["Width"]), int(dims["Height"])


def load_image(path, label):
    """Validate and prepare one image; return {"label", "url", "info"} where url
    is the data: URL and info is the one-line `attached:` description.

    PNG/JPEG/WebP within MAX_IMAGE_SIDE pass through byte-for-byte. Anything else
    goes through sips: oversize → downscaled (-Z); HEIC → JPEG; GIF (first frame)
    and TIFF → PNG; oversize WebP → PNG (sips can't write WebP). If the base64
    payload would still exceed MAX_IMAGE_B64, it is re-encoded as JPEG.
    """
    kind = _sniff_image(path)
    if kind is None:
        raise RuntimeError(f"not a supported image: {label}")
    w, h = _image_size(path, label)
    resize = max(w, h) > MAX_IMAGE_SIDE
    # sips keeps the source format unless told otherwise, so always name one when
    # converting. None = (maybe resize, but) keep the format.
    fmt = {"heic": "jpeg", "gif": "png", "tiff": "png"}.get(kind)
    if kind == "webp" and resize:
        fmt = "png"

    def run_sips(fmt, *extra):
        """Run sips into a fresh temp dir; return (bytes, kind, (w, h))."""
        with tempfile.TemporaryDirectory(prefix="chat-img-") as tmp:
            out = os.path.join(tmp, "out")
            cmd = ["sips", *extra]
            if resize:
                cmd += ["-Z", str(MAX_IMAGE_SIDE)]
            if fmt:
                cmd += ["-s", "format", fmt]
            proc = subprocess.run(cmd + [path, "--out", out],
                                  capture_output=True, text=True)
            out_kind = _sniff_image(out)   # sniff: a failed sips can leave no file
            if proc.returncode != 0 or out_kind not in ("png", "jpeg"):
                raise RuntimeError(f"couldn't process image: {label}")
            size = _image_size(out, label)
            with open(out, "rb") as f:
                return f.read(), out_kind, size

    if kind in ("png", "jpeg", "webp") and not resize:
        with open(path, "rb") as f:
            data = f.read()
        out_kind, size = kind, (w, h)
    else:
        data, out_kind, size = run_sips(fmt)
    if len(data) * 4 // 3 > MAX_IMAGE_B64:
        data, out_kind, size = run_sips("jpeg", "-s", "formatOptions", "80")
        if len(data) * 4 // 3 > MAX_IMAGE_B64:
            raise RuntimeError(f"image too large to send even as JPEG: {label}")

    dims = f"{w}x{h}" if size == (w, h) else f"{w}x{h} → {size[0]}x{size[1]}"
    kb = len(data) / 1024
    weight = f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
    b64 = base64.b64encode(data).decode("ascii")
    return {"label": label,
            "url": f"data:{_MIME[out_kind]};base64,{b64}",
            "info": f"{label}  {dims}  {weight}"}


# One whitespace-delimited word as a shell would see it: escaped chars, '…' and
# "…" runs, bare chars — and a stray quote or backslash is taken literally, so an
# apostrophe in prose ("what's this") doesn't wreck the whole line. Whitespace is
# ASCII only: macOS screenshot names contain U+202F, which must stay in the token.
_WORD = re.compile(r"""(?:\\.|'[^']*'|"(?:\\.|[^"\\])*"|[^ \t\r\n'"\\]|['"\\])+""",
                   re.S)


def extract_dropped_images(line):
    """Find dragged-in image paths in `line`. Returns (text, paths): the line with
    the attachment words cut out, and the absolute image paths.

    Ghostty pastes a dropped file as a shell-escaped (or quoted) path. A word
    counts only if, once unquoted and ~-expanded, it is an ABSOLUTE path to an
    existing regular file whose magic bytes say image; everything else (relative
    paths, URLs, other files, prose) is left in place untouched. Only the
    whitespace around a removed word is normalised.
    """
    paths, kept, pos = [], [], 0
    for m in _WORD.finditer(line):
        try:
            words = shlex.split(m.group())
        except ValueError:
            continue
        path = os.path.expanduser(words[0]) if len(words) == 1 else ""
        if os.path.isabs(path) and os.path.isfile(path) \
                and _sniff_image(path) in FILE_KINDS:
            paths.append(path)
            kept.append(line[pos:m.start()])
            pos = m.end()
    if not paths:
        return line, []
    kept.append(line[pos:])
    # Rejoin the pieces with one space, except where a newline already separates
    # them (then keep a single newline).
    text = kept[0]
    for piece in kept[1:]:
        text, piece = text.rstrip(" \t"), piece.lstrip(" \t")
        if text.endswith("\n") and piece.startswith("\n"):
            piece = piece[1:]
        gap = "" if text.endswith("\n") or piece.startswith("\n") else " "
        text += gap + piece
    return text.strip(), paths


def _osascript(script):
    """Stdout of an AppleScript snippet, or None if it failed (e.g. the
    clipboard has no data of the requested type)."""
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def clipboard_images():
    """Images on the macOS clipboard, as a list of load_image() dicts.

    Tried in order: (a) Finder-copied files; (b) PNG data (screenshots); (c) TIFF
    data (what some apps put there). Raises RuntimeError if there is none.
    """
    # (a) File(s). `as list` + `as alias` rather than «class furl»: furl fails for
    # several files, and coerces plain text like "hello" into the path "/hello".
    files = _osascript('''set out to {}
repeat with i in (the clipboard as list)
    try
        set end of out to POSIX path of (i as alias)
    end try
end repeat
set AppleScript's text item delimiters to linefeed
out as text''')
    paths = [p for p in (files or "").splitlines() if os.path.isfile(p)]
    if paths:   # real files are the answer — never fall through to their icon bitmap
        bad = [os.path.basename(p) for p in paths if _sniff_image(p) not in FILE_KINDS]
        if bad:
            raise RuntimeError(f"clipboard file isn't a supported image: {bad[0]}")
        return [load_image(p, os.path.basename(p)) for p in paths]
    # (b)/(c) Raw image data. Have AppleScript write the bytes straight to a temp
    # file (its stdout form is hex text, twice the size).
    for code, ext in (("PNGf", "png"), ("TIFF", "tiff")):
        with tempfile.TemporaryDirectory(prefix="chat-clip-") as tmp:
            path = os.path.join(tmp, f"clipboard.{ext}")
            ok = _osascript(f'''set d to the clipboard as «class {code}»
set f to open for access POSIX file "{path}" with write permission
try
    set eof f to 0
    write d to f
end try
close access f
"ok"''')
            if ok and _sniff_image(path):
                return [load_image(path, f"clipboard.{ext}")]
    raise RuntimeError("no image on the clipboard")


def main():
    # Roundtrip clock for --render --stats: this process spawns when the user
    # hits enter (the ask pipeline starts both stages at once), so main()'s
    # start is the closest measurable point to "enter" — only the interpreter's
    # own startup (tens of ms) precedes it.
    t0 = time.monotonic()
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--system")                       # required for the REPL only
    p.add_argument("--render", action="store_true",  # render-from-stdin mode for `ask`
                   help="render Markdown from stdin (with badge/bar) and exit")
    p.add_argument("--oneshot", action="store_true",  # generate-and-print mode for `ask`
                   help="generate from --system + prompt args, print raw text, exit")
    p.add_argument("prompt", nargs="*",              # the prompt, in --oneshot mode
                   help="prompt text (--oneshot only; pass after `--`)")
    p.add_argument("--effort", choices=("low", "medium", "high"),
                   help="thinking-depth dial (OpenRouter reasoning.effort); "
                        "omit to use the model's default")
    p.add_argument("--file",
                   help="path to a text/Markdown/code file (or PDF, via local "
                        "pdftotext) whose contents are injected as context "
                        "ahead of the message; --oneshot prepends it to the "
                        "prompt, the REPL attaches it to the first message typed")
    p.add_argument("--stats", action="store_true",
                   help="print one [stats] line (wall time + token counts) per "
                        "API call to stderr")
    p.add_argument("--no-compact", action="store_true",  # REPL only
                   help="disable automatic context compaction (/compact still works)")
    p.add_argument("--compact-threshold", type=int, default=None,
                   help="effective-input token threshold that triggers auto-"
                        "compaction (default 35000)")
    p.add_argument("--no-latency", action="store_true",  # REPL only
                   help="hide the per-reply [latency Ns] footer (on by default)")
    args = p.parse_args()

    # Resolve --file BEFORE any key check or API call: a bad path should fail
    # fast and locally, never burn a network round-trip. The oneshot/REPL modes
    # share one error-message prefix convention (RuntimeError → "ask:"/"chat:"),
    # picked here by --oneshot since that's the only signal available this early.
    file_block = None
    if args.file:
        prefix = "ask" if args.oneshot else "chat"
        try:
            file_block = read_file_context(args.file)
        except RuntimeError as e:
            print(f"{prefix}: {e}", file=sys.stderr)
            return 1

    bar = f"\033[{BAR_FG}m{GUTTER}\033[0m" if sys.stdout.isatty() else None

    # --render: the one-shot path for `ask`. Read the whole answer from stdin (it's
    # already generated — no API key or system prompt needed) and give it the same
    # model badge + gutter bar + restyled headings a chat reply gets, then exit. The
    # spinner covers the wait while the upstream `llm` is still streaming into us.
    if args.render:
        with _Spinner(BAR_FG):
            text = sys.stdin.read()
        text = text.strip("\n")
        if not text:
            return 0
        if sys.stdout.isatty():
            print(_chip(LABEL, CHIP_SGR))
        shown = render(text, bar)
        total = time.monotonic() - t0   # enter → answer ready for the screen
        _page(shown)
        if args.stats:
            # The one-shot process already printed latency/tokens (it made the API
            # call); this side owns the rest of the roundtrip — the stdin wait
            # (= the API call), mdcat, and styling. Printed after the pager so
            # it lands under the answer; pager dwell is excluded from total.
            print(_ansi("2", f"[stats] total={total:.2f}s"), file=sys.stderr)
        return 0

    # --oneshot: the generate-and-print path for `ask`. POST a single Messages
    # request and write the raw answer to stdout; the interactive `ask` then pipes
    # it back through --render. Strip any ANSI the [truncated]/[empty response]
    # markers add so escapes never leak into the Markdown mdcat renders downstream.
    if args.oneshot:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            print("ask: no OPENROUTER_API_KEY in the environment.", file=sys.stderr)
            return 1
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            if file_block:
                # A bare --file with no question is almost certainly a mistake
                # (nothing to answer), and silently no-op'ing would hide it.
                print("ask: --file needs a question to go with it.", file=sys.stderr)
                return 1
            return 0
        if file_block:
            # File first, question after — a cache-friendly prefix, and reads
            # naturally as "here's the context, now the question".
            prompt = file_block + "\n\n" + prompt
        try:
            t0 = time.monotonic()
            text, usage = call_openrouter(key, MODEL, args.system or "",
                                          [{"role": "user", "content": prompt}],
                                          effort=args.effort)
            elapsed = time.monotonic() - t0
        except RuntimeError as e:
            print(f"ask: {e}", file=sys.stderr)
            return 1
        if args.stats:
            _print_stats(elapsed, usage)
        text = re.sub(r"\033\[[0-9;]*m", "", text)   # drop marker styling
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0

    if not args.system:
        print("chat: --system is required for the REPL.", file=sys.stderr)
        return 2

    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("chat: no OPENROUTER_API_KEY in the environment.", file=sys.stderr)
        return 1

    messages = []       # the entire conversation, re-sent each turn; in memory only.
    queued = []         # /paste'd (or image-only) attachments waiting for the next
                        # message; load_image() dicts, cleared once a turn succeeds.
    last_usage = {}     # usage object from the most recent turn (for /usage + trigger)
    pending_file = file_block   # attached to the first message the user actually
                                 # sends; None once consumed. Set only from --file.
    compact_enabled = not args.no_compact
    compact_threshold = (args.compact_threshold if args.compact_threshold is not None
                         else COMPACT_THRESHOLD)   # an explicit flag wins

    if not MDCAT:
        print("chat: mdcat not found — printing raw Markdown "
              "(brew install mdcat to render).", file=sys.stderr)
    # Session banner: states the effort in effect (the shell always passes one).
    effort_note = f" · effort {args.effort}" if args.effort else ""
    compact_note = "" if compact_enabled else " · auto-compact off"
    latency_note = " · latency off" if args.no_latency else ""
    file_note = f" · file: {os.path.basename(args.file)}" if pending_file else ""
    print(_ansi("2", f"chat · {LABEL}{effort_note}{compact_note}{latency_note}{file_note} · "
                      "'exit'/'quit'/Ctrl-D to leave · "
                      "commands: /reset /edit /compact /usage /system /paste /detach"),
          file=sys.stderr)
    _print_system(args.system)

    # LABEL / CHIP_SGR / bar are shared with --render (see top of main()).
    # Both speakers get a chip on its OWN line, then their content below: ` you `
    # above your input, ` deepseek ` above the reply. The you-chip is printed directly
    # (NOT baked into the readline prompt) for a hard reason — macOS libedit mishandles
    # the \001..\002 "non-printing" markers used to colour a prompt: it hoists the
    # bracketed escapes to the front, so a trailing reset lands BEFORE the label and
    # strips its styling (the old colour-in-prompt never actually rendered). Dropping
    # the markers fixes the colour but then libedit counts the escape bytes as width
    # and the cursor drifts on long/wrapping lines. Sidestepping both: the chip is a
    # plain print() (background renders correctly) and the readline prompt is a bare,
    # escape-free "❯ ", so its width is exact and editing stays precise.
    you_chip = _chip("you", YOU_SGR)
    model_chip = _chip(LABEL, CHIP_SGR)
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
            queued.clear()
            last_usage = {}
            print(_ansi("2", "context cleared."), file=sys.stderr)
            continue
        if msg == "/usage":
            if not last_usage:
                print(_ansi("2", "no usage yet — send a message first."),
                      file=sys.stderr)
            else:
                # OpenAI usage names; cached comes from prompt_tokens_details
                # when the provider reports it.
                u = last_usage
                details = u.get("prompt_tokens_details") or {}
                print(f"input={u.get('prompt_tokens', 0)}  "
                      f"output={u.get('completion_tokens', 0)}  "
                      f"cached={details.get('cached_tokens', 0)}  "
                      f"provider={u.get('provider', '?').lower()}")
            continue
        if msg == "/system":
            _print_system(args.system)      # no API call, history untouched
            continue
        if msg == "/paste":
            # A slash command rather than a key binding: libedit can't run Python
            # from a keystroke, so there's no way to hook Cmd-V / a hotkey here.
            # The image is queued for the NEXT message, so type that text after.
            try:
                new = clipboard_images()
            except RuntimeError as e:
                print(_ansi("2", f"chat: {e}"), file=sys.stderr)
                continue
            queued.extend(new)
            for img in new:
                print(_ansi("2", f"attached: {img['info']}  (queued — type your message)"),
                      file=sys.stderr)
            continue
        if msg == "/detach":
            print(_ansi("2", f"dropped {len(queued)} queued image(s)." if queued
                        else "no queued images."), file=sys.stderr)
            queued.clear()
            continue
        if msg == "/compact":
            # Manual: runs regardless of threshold, ignores --no-compact, but still
            # honors the "too little history → skip" rule (announced out loud).
            new = compact(key, messages, announce_skip=True)
            if new is not None:
                messages = new
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

        # Drag-and-drop: Ghostty pastes a dropped file's path as typed text. Pull
        # out any image paths and prepare them now, so a bad image fails here,
        # before anything is sent.
        text, dropped = extract_dropped_images(msg)
        dropped_imgs = []
        if dropped:
            try:
                dropped_imgs = [load_image(path, os.path.basename(path)) for path in dropped]
            except RuntimeError as e:
                print(_ansi("2", f"chat: {e}"), file=sys.stderr)
                continue
            if not text:            # image-only line: queue it, like /paste
                queued.extend(dropped_imgs)
                for img in dropped_imgs:
                    print(_ansi("2", f"attached: {img['info']}  (queued — type your message)"),
                          file=sys.stderr)
                continue
        images = queued + dropped_imgs
        for img in images:
            print(_ansi("2", f"attached: {img['info']}"), file=sys.stderr)
        if dropped_imgs:            # only dragged images need confirming
            try:
                answer = input("send? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = "n"
            if answer not in ("", "y", "yes"):
                print(_ansi("2", "discarded."), file=sys.stderr)
                continue

        # Submit time for --stats total: input()/the editor just returned, so the
        # clock starts the moment the message is actually sent — time spent
        # composing in /edit never counts.
        t_enter = time.monotonic()
        # Attach the pending --file block to the OUTGOING content, not to `msg`
        # itself — /edit's echo above already printed the bare `msg`, and later
        # turns must never re-inject the file (it lives in history from here).
        outgoing = text if pending_file is None else pending_file + "\n\n" + text
        if images:
            # Text first, then the images (OpenRouter's recommended order). The
            # `_label` key is ours: stripped before sending, used by compaction.
            outgoing = [{"type": "text", "text": outgoing}] + [
                {"type": "image_url", "image_url": {"url": img["url"]},
                 "_label": img["label"]} for img in images]
        messages.append({"role": "user", "content": outgoing})
        try:
            # The spinner animates on stderr while the call blocks; the `with` stops
            # and erases it on success, error, and Ctrl-C alike.
            with _Spinner(BAR_FG):
                t_api = time.monotonic()
                reply, last_usage = call_openrouter(key, MODEL, args.system,
                                                    messages, effort=args.effort)
                elapsed = time.monotonic() - t_api
        except RuntimeError as e:
            messages.pop()          # drop the unanswered turn → history stays valid
            print(_ansi("2", f"chat: {e}"), file=sys.stderr)
            continue
        except KeyboardInterrupt:
            messages.pop()
            print(_ansi("2", "cancelled."), file=sys.stderr)
            continue

        messages.append({"role": "assistant", "content": reply})
        # Consume the pending file (and queued images) only now — after a confirmed
        # successful turn. The RuntimeError/KeyboardInterrupt branches above
        # `continue` before reaching here, so a failed or cancelled send leaves
        # them set and the retry still carries them.
        pending_file = None
        queued.clear()
        print()                              # space between your line and the reply
        print(model_chip)                    # ` deepseek ` header chip
        shown = render(reply, bar)
        # total = submit → styled reply ready for the screen, so it covers the
        # API call AND the mdcat/styling work. Measured before the pager: less
        # paints instantly, but a long reply can sit in it while you read, and
        # that dwell time is the reader's, not the pipeline's.
        total = time.monotonic() - t_enter
        _page(shown)
        if args.stats:
            # After the reply on purpose: the line sits under each message, and
            # never gets buried above a paged answer.
            _print_stats(elapsed, last_usage, total=total)
        elif not args.no_latency:
            # Default footer (API wall time only); --stats' line already has it.
            print(_ansi("2", f"[latency {elapsed:.2f}s]"), file=sys.stderr)

        # Auto-compaction. Done AFTER rendering so it never delays the current reply.
        # A successful compaction drops the prompt well below threshold, so it can't
        # re-fire until the conversation grows large again.
        if compact_enabled and last_usage:
            # prompt_tokens is the whole prompt, cached or not, so it IS the
            # effective input.
            effective_input = last_usage.get("prompt_tokens", 0)
            if effective_input >= compact_threshold:
                new = compact(key, messages)
                if new is not None:
                    messages = new

    return 0


if __name__ == "__main__":
    sys.exit(main())
