#!/usr/bin/env python3
"""chat REPL backend — a multi-turn Anthropic chat, rendered with mdcat.

This is the engine behind the zsh `chat()` function (see zsh/functions.zsh).
`chat()` stays thin: it picks the model id + system prompt, resolves the API
key, and exec's this script. Everything stateful lives here, in memory, for the
life of the process.

Design / isolation notes (mirroring the framing in functions.zsh):
  * Stdlib only. We talk to the APIs directly over HTTPS with urllib — no
    `anthropic`/`openai` SDK, so there is no pip/venv to manage. The only
    external dependencies are python3 (always present) and `mdcat` (rendering).
  * Two backends, two plain code paths. --backend anthropic (default) speaks
    the Messages API via call_api(); --backend openrouter speaks OpenAI Chat
    Completions via call_openrouter() (gpt-oss-120b, Grok 4.3, DeepSeek V4
    Flash; per-model provider pins live in OPENROUTER_PROVIDER). They are
    deliberately parallel functions,
    not one abstraction — the wire formats differ in enough small ways (system
    placement, response shape, refusal signalling, usage field names) that two
    commented paths stay clearer than a parameterized one. call_backend() is
    just the switch. Keys: ANTHROPIC_API_KEY / OPENROUTER_API_KEY, env-only.
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

# OpenRouter (the --backend openrouter path; OpenAI Chat Completions format).
# OPENROUTER_PROVIDER holds OPTIONAL per-model provider pins: gpt-oss is pinned
# to the fast serving providers with fallbacks off — predictable speed over
# availability (the softer alternative is the ":nitro" model-id suffix with no
# provider block). Models not listed here route freely: Grok is xAI-only anyway,
# and DeepSeek has a dozen-plus providers OpenRouter can arbitrage.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_PROVIDER = {
    "openai/gpt-oss-120b": {"only": ["groq", "cerebras"], "allow_fallbacks": False},
}

# Auto-compaction (REPL only). When the effective prompt size crosses the
# threshold, older turns are summarized by a cheap one-shot into a single
# synthetic exchange, keeping the recent tail intact. On by default; the
# `--no-compact` flag disables only the automatic trigger (manual `/compact`
# still works). The summarizer prompt lives HERE, not in functions.zsh — it's an
# internal implementation detail, not one of the user-facing _ASK_SYS_* prompts.
# The summarizer stays in-backend so an OpenRouter session never needs the
# Anthropic key (and vice versa).
COMPACT_MODEL = "claude-haiku-4-5"     # cheap + fast; summaries don't need Sonnet
COMPACT_MODEL_OPENROUTER = "openai/gpt-oss-120b"   # already cheap; keeps -g one-key
COMPACT_MAX_TOKENS = 600
DEFAULT_COMPACT_THRESHOLD = 8000       # effective input tokens (see main loop)
# The OpenRouter default triggers much later: on Groq/Cerebras the motive is
# interactive latency (decode slowdown at deep context, cold-prefill protection)
# rather than cost, and compacting too eagerly thrashes the provider's automatic
# prefix cache. An explicit --compact-threshold overrides either default.
OPENROUTER_COMPACT_THRESHOLD = 35000
SUMMARIZER_SYS = (
    "Summarize this conversation transcript densely for use as context in a "
    "continuing conversation. Preserve: established facts and definitions, "
    "conclusions reached, the user's stated preferences or corrections, and any "
    "open questions. Omit pleasantries and rendering artifacts. Target 300-400 "
    "tokens. Output only the summary, no preamble."
)

# Friendly labels for the chips/prompt; falls back to the raw id if unmapped.
MODEL_LABELS = {
    "claude-sonnet-5": "sonnet",
    "claude-fable-5": "fable",
    "claude-sonnet-4-6": "sonnet",
    "claude-opus-4-8": "opus",
    "claude-haiku-4-5": "haiku",
    "openai/gpt-oss-120b": "gpt-oss",
    "x-ai/grok-4.3": "grok",
    "deepseek/deepseek-v4-flash": "deepseek",
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
# model (fable), blue = sonnet; the non-Anthropic guests take the remaining
# palette (yellow = gpt-oss, white = grok, red = deepseek); all are distinct
# from your green chip so human and assistant never blur together. The fallback
# covers any unlisted model.
ACCENTS = {
    "claude-sonnet-5":   ("1;97;44", "94"),   # white-on-blue    chip, bright-blue    bar
    "claude-fable-5":    ("1;97;45", "95"),   # white-on-magenta chip, bright-magenta bar
    "claude-opus-4-8":   ("1;97;45", "95"),   # white-on-magenta chip, bright-magenta bar
    "claude-sonnet-4-6": ("1;97;44", "94"),   # white-on-blue    chip, bright-blue    bar
    "claude-haiku-4-5":  ("1;30;46", "96"),   # black-on-cyan    chip, bright-cyan    bar
    "openai/gpt-oss-120b": ("1;30;43", "93"), # black-on-yellow  chip, bright-yellow  bar
    "x-ai/grok-4.3":     ("1;30;47", "97"),   # black-on-white   chip, bright-white   bar
    "deepseek/deepseek-v4-flash": ("1;97;41", "91"),  # white-on-red chip, bright-red bar
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


def _with_cache_breakpoint(messages):
    """Return a copy of `messages` with an ephemeral cache_control breakpoint on
    the last content block of the LAST message.

    History is stored clean (plain string content); the marker is attached only
    here, at send time, so exactly one breakpoint ever exists and it always rides
    the newest turn. Everything up to and including it — system prompt + all prior
    turns — becomes the cacheable prefix, billed at the cache-read rate on repeat
    turns. Only the last element is rebuilt into content-block form; earlier
    string-content messages are left as-is (the API accepts the mix).
    """
    if not messages:
        return messages
    out = list(messages)                 # shallow copy; we replace only the tail
    last = out[-1]
    content = last["content"]
    if isinstance(content, str):         # normal case: history stores strings
        out[-1] = {"role": last["role"], "content": [
            {"type": "text", "text": content,
             "cache_control": {"type": "ephemeral"}},
        ]}
    return out


def call_api(key, model, system, messages, max_tokens=MAX_TOKENS, cache=False,
             effort=None):
    """POST the whole conversation; return (assistant_text, usage_dict).

    `usage_dict` is the API's usage object (input/output/cache token counts); it
    is `{}` if the response omits it. When `cache=True`, an ephemeral cache
    breakpoint is placed on the final message so the re-sent prefix bills at the
    cache-read rate. `effort` (low/medium/high) becomes output_config.effort —
    the thinking-depth / token-spend dial; None omits it entirely, which the API
    treats as "high". Haiku 4.5 rejects the parameter, so callers using a Haiku
    model (e.g. compact()) must leave it None. Raises RuntimeError with a
    human-readable message on any API/network error; the caller keeps the REPL
    alive and leaves history intact.
    """
    if cache:
        messages = _with_cache_breakpoint(messages)
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if effort:
        payload["output_config"] = {"effort": effort}
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
    stop_reason = data.get("stop_reason")
    if stop_reason == "max_tokens":
        text += _ansi("2", f"\n\n[truncated at {max_tokens} tokens]")
    elif stop_reason == "refusal":
        # A safety classifier declined the request: HTTP 200 with stop_reason
        # "refusal" and (usually) an empty content array — Fable trips these readily
        # on biology/cyber topics. Without this branch the empty content falls through
        # to "[empty response]", hiding *why*. stop_details carries the category when
        # present but can be null even on a refusal, so we key off stop_reason and
        # guard the details. (Ordinary model refusals on Sonnet come back as normal
        # text with stop_reason "end_turn", so they never reach here.)
        details = data.get("stop_details") or {}
        category = details.get("category")
        note = "content safety classifier declined this request"
        if category:
            note += f" (category: {category})"
        if model == "claude-fable-5":
            note += "; Fable's classifiers are strict — retry without -f to use Sonnet"
        marker = _ansi("2", f"[refused: {note}]")
        # Pre-output refusal → empty content → just the marker; a partial (mid-turn)
        # refusal keeps what was generated and appends the notice.
        text = f"{text}\n\n{marker}" if text.strip() else marker
    usage = data.get("usage") or {}
    return (text or _ansi("2", "[empty response]")), usage


def call_openrouter(key, model, system, messages, max_tokens=MAX_TOKENS,
                    effort=None):
    """POST the whole conversation to OpenRouter; return (assistant_text, usage).

    The OpenRouter twin of call_api(), speaking OpenAI Chat Completions format —
    kept as a separate, parallel function on purpose (two plain code paths beat
    one parameterized abstraction). The format differences it owns:
      * `system` becomes a leading {"role": "system"} message, not a top-level
        field.
      * The reply lives at choices[0].message.content. A reasoning model's
        thinking may arrive alongside it (message.reasoning) and is deliberately
        never read — only the final answer is shown.
      * `effort` maps to reasoning.effort (every model on this backend supports
        it); None omits the block and the model uses its own default.
      * No cache_control blocks are ever sent (they're Anthropic-only); nothing
        replaces them — the serving providers prefix-cache automatically.
      * usage comes back with OpenAI names (prompt_tokens / completion_tokens /
        prompt_tokens_details.cached_tokens).
    Raises RuntimeError with a human-readable message on any API/network error —
    the same contract as call_api(), so callers treat the two identically.
    """
    msgs = list(messages)
    if system:
        msgs = [{"role": "system", "content": system}] + msgs
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": msgs,
    }
    pin = OPENROUTER_PROVIDER.get(model)   # per-model provider pin, if any
    if pin:
        payload["provider"] = pin
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
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        # Provider-availability errors need context when a pin applied: with
        # fallbacks off, "no providers" means *the pinned ones* are down, not
        # the model — say so instead of leaving a mystery.
        if pin and "provider" in str(detail).lower():
            detail = (f"{detail} (note: this setup pins "
                      f"{'/'.join(pin['only'])} with fallbacks off)")
        raise RuntimeError(f"API error {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}")

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""     # None on some refusals → ""
    finish = choice.get("finish_reason")
    refusal = message.get("refusal")
    if refusal or finish == "content_filter":
        # OpenAI-format refusal signalling: an explicit message.refusal string
        # and/or finish_reason "content_filter". Surfaced the same way the
        # Anthropic path surfaces stop_reason "refusal": marker alone when the
        # content is empty, appended notice when a partial answer exists.
        note = refusal or "provider content filter stopped the response"
        marker = _ansi("2", f"[refused: {note}]")
        text = f"{text}\n\n{marker}" if text.strip() else marker
    elif finish == "length":
        text += _ansi("2", f"\n\n[truncated at {max_tokens} tokens]")
    usage = data.get("usage") or {}
    return (text or _ansi("2", "[empty response]")), usage


def call_backend(backend, key, model, system, messages, max_tokens=MAX_TOKENS,
                 cache=False, effort=None):
    """Route one call to the right API. Not an abstraction over the formats —
    just the switch, so call sites don't repeat it. `cache` is Anthropic-only
    (cache_control blocks must never reach OpenRouter; Groq/Cerebras cache
    prefixes automatically), so it is simply dropped on the OpenRouter path.
    """
    if backend == "openrouter":
        return call_openrouter(key, model, system, messages,
                               max_tokens=max_tokens, effort=effort)
    return call_api(key, model, system, messages, max_tokens=max_tokens,
                    cache=cache, effort=effort)


def _print_stats(backend, elapsed, usage, total=None):
    """One dim [stats] line on stderr — never stdout, so piped/tool output stays
    clean. Requests here are non-streaming, so ttft is the whole request wall
    time and there is no tokens/sec figure. `total` is the full roundtrip —
    message submitted → styled reply ready for the screen — appended when the
    caller can measure it (the chat REPL can; the one-shot process can't see the
    downstream render stage, which prints its own total= line instead). in/out
    come from the usage object (field names differ per backend); cached= appears
    only when the backend reported a cached-token count. Purpose: tune
    --compact-threshold from data.
    """
    if backend == "openrouter":
        tin = usage.get("prompt_tokens", 0)
        tout = usage.get("completion_tokens", 0)
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    else:
        tin = usage.get("input_tokens", 0)
        tout = usage.get("output_tokens", 0)
        cached = usage.get("cache_read_input_tokens")
    line = f"[stats] ttft={elapsed:.2f}s"
    if total is not None:
        line += f" total={total:.2f}s"
    line += f" in={tin} out={tout}"
    if cached is not None:
        line += f" cached={cached}"
    # \r + erase first: when `ask` pipes --oneshot into --render, the render
    # process's spinner may own the current stderr line — clear it so the stats
    # line never lands appended to a half-drawn spinner frame.
    prefix = "\r\033[K" if sys.stderr.isatty() else ""
    print(f"{prefix}{_ansi('2', line)}", file=sys.stderr)


def _serialize_transcript(messages):
    """Flatten stored messages into a plain `You:` / `Assistant:` transcript for
    the summarizer. History is stored as string content; the block branch is
    defensive only (in case a decorated message ever reaches here)."""
    lines = []
    for m in messages:
        who = "You" if m["role"] == "user" else "Assistant"
        content = m["content"]
        if not isinstance(content, str):
            content = "".join(b.get("text", "") for b in content
                              if isinstance(b, dict))
        lines.append(f"{who}: {content}")
    return "\n\n".join(lines)


def compact(key, messages, backend="anthropic", announce_skip=False):
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
    # Summarize in-backend so the session's one key suffices: Haiku on Anthropic
    # (no effort param — it rejects one), gpt-oss itself on OpenRouter at low
    # reasoning effort (a summary needs no deep thinking). gpt-oss's reasoning
    # tokens share the completion budget, so its cap gets headroom on top of the
    # 300-400-token summary target.
    if backend == "openrouter":
        model, effort, cap = COMPACT_MODEL_OPENROUTER, "low", COMPACT_MAX_TOKENS * 2
    else:
        model, effort, cap = COMPACT_MODEL, None, COMPACT_MAX_TOKENS
    try:
        summary, _ = call_backend(backend, key, model, SUMMARIZER_SYS,
                                  [{"role": "user", "content": _serialize_transcript(evict)}],
                                  max_tokens=cap, effort=effort)
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
    """Render Markdown via mdcat and draw a gutter bar down the left; RETURN the
    styled text (callers hand it to _page). Returning instead of printing lets
    callers stop the --stats roundtrip clock at "ready for the screen" — after
    all styling work, before the pager, whose dwell time belongs to the reader.

    `bar` is the pre-coloured gutter string (e.g. a magenta ▌), or None when
    stdout isn't a TTY — then the gutter is skipped (and _page just prints). Any
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


def main():
    # Roundtrip clock for --render --stats: this process spawns when the user
    # hits enter (the ask pipeline starts both stages at once), so main()'s
    # start is the closest measurable point to "enter" — only the interpreter's
    # own startup (tens of ms) precedes it.
    t0 = time.monotonic()
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--model", required=True)
    p.add_argument("--system")                       # required for the REPL only
    p.add_argument("--render", action="store_true",  # render-from-stdin mode for `ask`
                   help="render Markdown from stdin (with badge/bar) and exit")
    p.add_argument("--oneshot", action="store_true",  # generate-and-print mode for `ask`
                   help="generate from --system + prompt args, print raw text, exit")
    p.add_argument("prompt", nargs="*",              # the prompt, in --oneshot mode
                   help="prompt text (--oneshot only; pass after `--`)")
    p.add_argument("--backend", choices=("anthropic", "openrouter"),
                   default="anthropic",
                   help="which API the model id belongs to; the shell layer "
                        "always passes a matching model/backend pair")
    p.add_argument("--effort", choices=("low", "medium", "high"),
                   help="thinking-depth dial: Anthropic output_config.effort / "
                        "OpenRouter reasoning.effort; omit to use the API "
                        "default. The shell layer omits it for Haiku, which "
                        "rejects the parameter.")
    p.add_argument("--stats", action="store_true",
                   help="print one [stats] line (wall time + token counts) per "
                        "API call to stderr")
    p.add_argument("--no-compact", action="store_true",  # REPL only
                   help="disable automatic context compaction (/compact still works)")
    p.add_argument("--compact-threshold", type=int, default=None,
                   help="effective-input token threshold that triggers auto-"
                        "compaction (default 8000, or 35000 on the openrouter "
                        "backend)")
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
        shown = render(text, bar)
        total = time.monotonic() - t0   # enter → answer ready for the screen
        _page(shown)
        if args.stats:
            # The one-shot process already printed ttft/tokens (it made the API
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
        key_var = ("OPENROUTER_API_KEY" if args.backend == "openrouter"
                   else "ANTHROPIC_API_KEY")
        key = os.environ.get(key_var, "").strip()
        if not key:
            print(f"ask: no {key_var} in the environment.", file=sys.stderr)
            return 1
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            return 0
        try:
            t0 = time.monotonic()
            text, usage = call_backend(args.backend, key, args.model,
                                       args.system or "",
                                       [{"role": "user", "content": prompt}],
                                       effort=args.effort)
            elapsed = time.monotonic() - t0
        except RuntimeError as e:
            print(f"ask: {e}", file=sys.stderr)
            return 1
        if args.stats:
            _print_stats(args.backend, elapsed, usage)
        text = re.sub(r"\033\[[0-9;]*m", "", text)   # drop marker styling
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0

    if not args.system:
        print("chat: --system is required for the REPL.", file=sys.stderr)
        return 2

    key_var = ("OPENROUTER_API_KEY" if args.backend == "openrouter"
               else "ANTHROPIC_API_KEY")
    key = os.environ.get(key_var, "").strip()
    if not key:
        print(f"chat: no {key_var} in the environment.", file=sys.stderr)
        return 1

    messages = []       # the entire conversation, re-sent each turn; in memory only.
    last_usage = {}     # usage object from the most recent turn (for /usage + trigger)
    compact_enabled = not args.no_compact
    # Per-backend default (see the threshold constants); an explicit flag wins.
    compact_threshold = args.compact_threshold
    if compact_threshold is None:
        compact_threshold = (OPENROUTER_COMPACT_THRESHOLD
                             if args.backend == "openrouter"
                             else DEFAULT_COMPACT_THRESHOLD)

    if not MDCAT:
        print("chat: mdcat not found — printing raw Markdown "
              "(brew install mdcat to render).", file=sys.stderr)
    # Session banner: always states the model AND the effort in effect. Haiku
    # sessions arrive with no --effort (the parameter isn't supported there), so
    # they honestly say "n/a" rather than implying a level is set.
    effort_note = f" · effort {args.effort}" if args.effort else " · effort n/a"
    compact_note = "" if compact_enabled else " · auto-compact off"
    print(_ansi("2", f"chat · {label}{effort_note}{compact_note} · "
                      "'exit'/'quit'/Ctrl-D to leave · "
                      "commands: /reset /edit /compact /usage"),
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
            last_usage = {}
            print(_ansi("2", "context cleared."), file=sys.stderr)
            continue
        if msg == "/usage":
            if not last_usage:
                print(_ansi("2", "no usage yet — send a message first."),
                      file=sys.stderr)
            elif args.backend == "openrouter":
                # OpenAI usage names; cached comes from prompt_tokens_details
                # when the provider reports it (Groq does).
                u = last_usage
                details = u.get("prompt_tokens_details") or {}
                print(f"input={u.get('prompt_tokens', 0)}  "
                      f"output={u.get('completion_tokens', 0)}  "
                      f"cached={details.get('cached_tokens', 0)}")
            else:
                u = last_usage
                print(f"input={u.get('input_tokens', 0)}  "
                      f"output={u.get('output_tokens', 0)}  "
                      f"cache_creation={u.get('cache_creation_input_tokens', 0)}  "
                      f"cache_read={u.get('cache_read_input_tokens', 0)}")
            continue
        if msg == "/compact":
            # Manual: runs regardless of threshold, ignores --no-compact, but still
            # honors the "too little history → skip" rule (announced out loud).
            new = compact(key, messages, backend=args.backend, announce_skip=True)
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

        # Submit time for --stats total: input()/the editor just returned, so the
        # clock starts the moment the message is actually sent — time spent
        # composing in /edit never counts.
        t_enter = time.monotonic()
        messages.append({"role": "user", "content": msg})
        try:
            # The spinner animates on stderr while the call blocks; the `with` stops
            # and erases it on success, error, and Ctrl-C alike. cache=True marks the
            # re-sent prefix cacheable on the Anthropic path only (see call_backend
            # and _with_cache_breakpoint).
            with _Spinner(bar_fg):
                t_api = time.monotonic()
                reply, last_usage = call_backend(args.backend, key, args.model,
                                                 args.system, messages,
                                                 cache=True, effort=args.effort)
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
        print()                              # space between your line and the reply
        print(model_chip)                    # ` opus ` / ` sonnet ` header chip
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
            _print_stats(args.backend, elapsed, last_usage, total=total)

        # Auto-compaction. Done AFTER rendering so it never delays the current reply.
        # A successful compaction drops the prompt well below threshold, so it can't
        # re-fire until the conversation grows large again.
        if compact_enabled and last_usage:
            if args.backend == "openrouter":
                # OpenAI-format usage: prompt_tokens is the whole prompt, cached
                # or not, so it IS the effective input.
                effective_input = last_usage.get("prompt_tokens", 0)
            else:
                # These three fields partition the true prompt size when caching
                # is on (input_tokens alone counts only the uncached part).
                effective_input = (last_usage.get("input_tokens", 0)
                                   + last_usage.get("cache_read_input_tokens", 0)
                                   + last_usage.get("cache_creation_input_tokens", 0))
            if effective_input >= compact_threshold:
                new = compact(key, messages, backend=args.backend)
                if new is not None:
                    messages = new

    return 0


if __name__ == "__main__":
    sys.exit(main())
