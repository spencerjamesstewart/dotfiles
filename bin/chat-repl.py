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
    (API wall time, plus the search count when it searched); `--no-latency`
    hides it and `--stats` replaces it with the fuller [stats] line.
  * Multi-line input via the editor. libedit (the macOS readline) has no
    bracketed-paste support, so a pasted block submits line by line. `/edit`
    opens $EDITOR to compose or paste a whole message instead (see
    compose_in_editor); the buffer comes back as one turn.
  * Images. Drag an image file onto the prompt (Ghostty pastes its path) or queue
    the clipboard image with `/paste`. Each image is validated and downscaled
    locally with macOS `sips` (no Pillow), sent inline as a base64 data URL, and
    stays in the in-memory history so later turns can refer to it.
  * Routing. Every request (chat, one-shot, compaction) tries Together first and
    falls back only within the PROVIDERS pool (see below).
  * Web search. On by default: the model gets OpenRouter's server-side search
    tool and decides when to use it (~$0.007/search, +~3s). Replies that used it
    end with a Sources list (never stored in history); --no-search or the REPL's
    `/search off` turns it off. Compaction never searches.
  * Starter prompts. Each .md/.txt file in chat-prompts/ is a standing-context
    prompt (an optional first line `description: …` labels it). At startup a
    stdlib-only picker (termios + ANSI) offers them, with a word-diff view for
    telling similar ones apart, and `a`/`e`/`d` keys to add, edit (in $EDITOR) and
    delete (to the Trash, never rm) files without leaving the picker; the picker
    opens even with an empty library so `a` can start one. `--starter NAME`
    picks one by name and `--no-starter` skips the picker. The choice is appended to --system and
    re-sent every turn, never stored in the history, so /reset and /compact
    keep it. The global prompt itself is still never copied here.
  * The API key arrives via the OPENROUTER_API_KEY env var, never on argv (which
    `ps` can see) and never on disk; we read it once and keep it in memory.
"""

import argparse
import base64
import contextlib
import difflib
import functools
import http.client
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
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

# OpenRouter (OpenAI Chat Completions format). Routing (see call_openrouter):
# Together first, fallbacks within PROVIDERS; the --stats provider= field shows
# who served each reply.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Starter prompts (chat only): one .md/.txt file each, in <repo>/chat-prompts/.
# Read at call time (see load_starters), so a test can point it elsewhere.
STARTERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "chat-prompts")

# Provider pool (provider.only, OpenRouter endpoint tags) for every request.
# Tags verified for BOTH image input and tool calling on 2026-10-30 against
# deepseek/deepseek-v4.1-flash-1017 (Fireworks tags left out on purpose, as are
# tags that were unroutable). Together is tried first; fallbacks stay inside the
# list. relace and modal returned upstream 429 during the probe, so they are
# untested rather than known-incapable, and kept. Re-probe if MODEL changes.
PROVIDERS = [
    "morph/fp8", "open-inference/fp4", "wafer/us", "reka", "wafer/mxfp4",
    "relace", "deepinfra/fp8/flex", "inference-net/us", "inference-net/fp8",
    "deepinfra/fp8", "cloudflare", "particle-ai", "streamlake/fp8", "decart/fp4",
    "deepinfra/fp8/priority", "makora/fp8", "ionstream", "alibaba", "modelrun/fp4",
    "digitalocean", "gmicloud/fp8", "cohere", "atlas-cloud/fp8", "novita/fp8",
    "phala", "modal", "baidu/fp8", "crusoe/fp8", "venice/fp8", "mistral/fp8",
    "nebius/fp8", "siliconflow/fp8", "together", "primeintellect", "baseten",
    "parasail/fp8",
]

# Web search: OpenRouter's server-side tool. OpenRouter runs the search loop and
# returns one ordinary assistant message, so this client never handles tool
# calls. Exa because MODEL has no native search (auto would pick it anyway), it
# honors max_characters, and it costs ~$0.007/search. Caps keep prompts and cost
# small: 3 results per search, 6 per turn, 1000 chars each, 2 searches per turn
# (max_uses; call_openrouter also sends it top-level as max_tool_calls). Exa's
# "mode": "fast" is accepted but was slower, not faster, when tried, so it's out.
SEARCH_TOOL = {"type": "openrouter:web_search", "parameters": {
    "engine": "exa", "max_results": 3, "max_total_results": 6,
    "max_uses": 2, "max_characters": 1000}}

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


def _wire_message(m):
    """Copy of a stored message with private (`_`-prefixed) part keys such as
    `_label` dropped, so OpenRouter never sees them. The stored message keeps
    them (compaction uses the label)."""
    if not isinstance(m["content"], list):
        return m
    parts = [{k: v for k, v in p.items() if not k.startswith("_")}
             for p in m["content"]]
    return {**m, "content": parts}


def _search_info(message, usage):
    """(search count, [(title, url), ...]) from a response; sources are deduped by
    URL, in order. Either is 0 / empty when absent or malformed. Excerpts
    (`content`) are never read."""
    count = 0
    for name in ("server_tool_use_details", "server_tool_use"):  # docs vs probes
        d = usage.get(name)
        n = d.get("web_search_requests") if isinstance(d, dict) else None
        if isinstance(n, int) and not isinstance(n, bool):
            count = n
            break
    sources, seen = [], set()
    anns = message.get("annotations")
    for a in anns if isinstance(anns, list) else []:
        c = a.get("url_citation") if isinstance(a, dict) else None
        url = c.get("url") if isinstance(c, dict) else None
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            title = c.get("title")
            sources.append((title if isinstance(title, str) else "", url))
    return count, sources


def _sources_block(sources):
    """The "Sources:" Markdown list for a reply, or "" when there are none. Titles
    and URLs come from the web, so control characters (ESC included) are
    flattened to spaces before anything reaches the terminal."""
    if not sources:
        return ""
    lines = []
    for i, (title, url) in enumerate(sources, 1):
        title = re.sub(r"[\s\x00-\x1f\x7f-\x9f]+", " ", title).strip()
        title = title.lstrip("-*+#>• ")     # a leading marker would open a nested Markdown block
        if len(title) > 80:
            title = title[:79] + "…"
        url = re.sub(r"[\s\x00-\x1f\x7f-\x9f]+", " ", url).strip()
        lines.append(f"{i}. {title} — {url}" if title else f"{i}. {url}")
    return "\n\nSources:\n" + "\n".join(lines)


def call_openrouter(key, model, system, messages, max_tokens=MAX_TOKENS,
                    effort=None, strict=False, search=False):
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
      * `search=True` offers SEARCH_TOOL; OpenRouter runs the searches itself.
        The search count and the cited pages ride back in usage as `searches`
        and `sources` ([(title, url), ...]), present only when non-empty.
      * No cache markers are sent — the serving providers prefix-cache
        automatically.
      * Routing: always Together first, with fallbacks restricted to PROVIDERS.
        The file-parser plugin is always disabled so nothing is processed in
        OpenRouter's cloud.
      * usage comes back with OpenAI names (prompt_tokens / completion_tokens /
        prompt_tokens_details.cached_tokens).
    Raises RuntimeError with a human-readable message on any API/network error
    or malformed response; the caller keeps the REPL alive and leaves history
    intact.
    """
    msgs = [_wire_message(m) for m in messages]
    if system:
        msgs = [{"role": "system", "content": system}] + msgs
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": msgs,
        "provider": {"order": ["together"], "only": PROVIDERS, "allow_fallbacks": True},
        "plugins": [{"id": "file-parser", "enabled": False}],
    }
    if effort:
        payload["reasoning"] = {"effort": effort}
    hint = ""
    if search:
        payload["tools"] = [SEARCH_TOOL]
        payload["max_tool_calls"] = SEARCH_TOOL["parameters"]["max_uses"]
        hint = " (web search was on: /search off in chat, --no-search in ask)"
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
            try:                                # the body read can fail too
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            try:
                detail = json.loads(detail)["error"]["message"]
            except Exception:
                pass
            # 400/403/404 on a request carrying the search tool: search (no tool
            # support, engine or region rejection) is the likely cause.
            raise RuntimeError(f"API error {e.code}: {detail or e.reason}"
                               f"{hint if e.code in (400, 403, 404) else ''}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"network error: {e.reason}")
        except (OSError, http.client.HTTPException) as e:   # timeout, reset, cut-off body
            raise RuntimeError(f"network error: {str(e) or type(e).__name__}")
        except ValueError as e:                 # invalid JSON / non-UTF-8 body
            raise RuntimeError(f"unreadable API response: {e}")

    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        # A 200 can still carry an {"error": ...} object instead of choices.
        err = data.get("error") if isinstance(data, dict) else None
        err = err.get("message") if isinstance(err, dict) else err
        raise RuntimeError(f"API error: {err}" if err else "API response had no choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("API response had no message")
    if message.get("tool_calls"):
        # The search tool runs server-side, so a client-side tool call is a
        # provider quirk; this loop can't run it, and storing it would break history.
        raise RuntimeError(f"model requested a tool call, which isn't supported{hint}")
    text = message.get("content")           # None on some refusals → ""
    if text is None:
        text = ""
    elif not isinstance(text, str):
        raise RuntimeError("API response had non-text content")
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
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    # OpenRouter names the serving provider top-level in the response; tuck it
    # into usage so it rides along to --stats / /usage without a wider return.
    # Search results ride along the same way.
    if isinstance(data.get("provider"), str) and data["provider"]:
        usage["provider"] = data["provider"]
    searches, sources = _search_info(message, usage)
    if searches:
        usage["searches"] = searches
    if sources:
        usage["sources"] = sources
    return (text or _ansi("2", "[empty response]")), usage


def _search_fields(usage):
    """["searches=N", "cost=$X"] for a turn that searched, else []. cost is the
    API's usage.cost (tokens + search fees), shown only alongside a search."""
    n = usage.get("searches", 0)
    if n <= 0:
        return []
    cost = usage.get("cost")
    return [f"searches={n}"] + (
        [f"cost=${cost:.4f}"] if isinstance(cost, (int, float)) else [])


def _print_stats(elapsed, usage, total=None):
    """One dim [stats] line on stderr — never stdout, so piped/tool output stays
    clean. Requests here are non-streaming, so latency is the whole request wall
    time and there is no tokens/sec figure. `total` is the full roundtrip —
    message submitted → styled reply ready for the screen — appended when the
    caller can measure it (the chat REPL can; the one-shot process can't see the
    downstream render stage, which prints its own total= line instead). cached=
    appears only when the provider reported a cached-token count; searches= and
    cost= only when the turn searched. Purpose: tune --compact-threshold from data.
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
    line += "".join(f" {f}" for f in _search_fields(usage))
    # \r + erase first: when `ask` pipes --oneshot into --render, the render
    # process's spinner may own the current stderr line — clear it so the stats
    # line never lands appended to a half-drawn spinner frame.
    prefix = "\r\033[K" if sys.stderr.isatty() else ""
    print(f"{prefix}{_ansi('2', line)}", file=sys.stderr)


# The REPL command list shown by /help. zsh/functions.zsh `_chat_help` carries a
# copy of this text under "In the REPL:" (zsh can't import Python) — keep the two
# in sync by hand when a command is added or reworded.
REPL_HELP = """\
In the REPL:
  /edit [text]      compose the next message in $EDITOR (multi-line input)
  /reset            clear the conversation context
  /compact          summarize older turns now
  /usage            show the last turn's token counts
  /system           show the system prompt in use (also printed at startup)
  /paste            queue the clipboard image for the next message
  /detach           drop queued images
  /search [on|off]  show or set web search (on by default)
  /help             show this list
  exit, quit        leave (Ctrl-D also works)"""


_SLASH_WORD = re.compile(r"/[A-Za-z0-9_-]*")


def _is_slash_command_word(msg):
    """True when the first whitespace-delimited word of `msg` is `/` plus only
    letters, digits, `_` or `-` (so `/foo`, `/Help`, a bare `/`). A word with
    further slashes or dots (`/etc/hosts`, `/tmp/a.png`) is a path, not a command
    — it still goes to the model, and dragged-in image paths still reach
    extract_dropped_images."""
    words = msg.split(None, 1)
    return bool(words) and _SLASH_WORD.fullmatch(words[0]) is not None


def _print_system(system, starter=None):
    """Print the system prompt to stderr as a dim, labelled, word-wrapped block
    (REPL startup and `/system`). Each line is styled on its own, so a wrapped
    block never leaves an SGR span open across a line break. Lines never break
    at a hyphen, so hyphenated words in the prompt stay whole. A starter
    (name, body) follows under a `starter: NAME` divider, in normal intensity and
    wrapped per source line, so its own line breaks survive."""
    label = "system · "
    lines = textwrap.wrap(system, width=min(shutil.get_terminal_size().columns, 100),
                          initial_indent=label, subsequent_indent=" " * len(label),
                          break_on_hyphens=False)
    for line in lines:
        print(_ansi("2", line), file=sys.stderr)
    if starter:
        name, body = starter
        width = min(shutil.get_terminal_size().columns, 100)
        print(_ansi("2", f"── starter: {name} ──"), file=sys.stderr)
        for src in _clean(body).split("\n"):
            for line in textwrap.wrap(src, width=width, break_on_hyphens=False) or [""]:
                print(line, file=sys.stderr)


def _serialize_transcript(messages):
    """Flatten stored messages into a plain `You:` / `Assistant:` transcript for
    the summarizer. Content is a string, or a list of parts for a turn with
    images; each image becomes `[image: <label>]`, so the summarizer request is
    text-only."""
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
    except KeyboardInterrupt:
        print(_ansi("2", "chat: compaction cancelled; history unchanged."),
              file=sys.stderr)
        return None
    except Exception as e:      # RuntimeError from the API, or anything unexpected
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


# --- starter prompts (chat only) ------------------------------------------------
# A library of standing-context prompts, one text file each. At REPL startup a
# full-screen picker (stdlib only: termios + ANSI) chooses one, or none; its text
# is appended to the system prompt for the whole session and is never sent as a
# message. Esc / Ctrl-C / EOF in the picker mean "none".

def load_starters():
    """[(name, description, body), ...] from STARTERS_DIR: regular .md/.txt files,
    not hidden, sorted by filename (case-insensitive); of duplicate names the
    first wins. A first line `description: …` is split off as the description.
    Unreadable or empty files are skipped, and a missing directory is just an
    empty library."""
    try:
        files = sorted(os.listdir(STARTERS_DIR), key=lambda f: (f.lower(), f))
    except OSError:
        return []
    starters, seen = [], set()
    for fname in files:
        name, ext = os.path.splitext(fname)
        path = os.path.join(STARTERS_DIR, fname)
        if (fname.startswith(".") or ext.lower() not in (".md", ".txt")
                or name.lower() in seen or not os.path.isfile(path)):
            continue
        try:
            # utf-8-sig, so a BOM can't hide the `description:` line
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip()
        first, _, rest = text.partition("\n")
        m = re.match(r"description:\s*(.*)$", first, re.I)
        desc = ""
        if m:
            desc, text = m.group(1).strip(), re.sub(r"\A(?:[ \t]*\n)+", "", rest)
        if text:
            seen.add(name.lower())
            starters.append((name, desc, text))
    return starters


def find_starter(starters, name):
    """The entry NAME refers to: a case-insensitive exact match, else a unique
    case-insensitive prefix. Raises RuntimeError with a human-readable message
    (no `chat:` prefix, like read_file_context) when there is none or several."""
    if not starters:
        raise RuntimeError(f"no starter prompts found in {STARTERS_DIR}")
    low = name.lower()
    for s in starters:
        if s[0].lower() == low:
            return s
    hits = [s for s in starters if s[0].lower().startswith(low)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise RuntimeError(f"'{name}' matches several starter prompts: "
                           f"{', '.join(s[0] for s in hits)} (be more specific)")
    raise RuntimeError(f"no starter prompt matches '{name}' "
                       f"(available: {', '.join(s[0] for s in starters)})")


# --- starter management (picker keys a / e / d) ---------------------------------
# File operations behind the picker's add / edit / delete, kept free of any
# terminal handling so they can be called (and tested) without a TTY. Each
# returns a short message for the picker's footer ("" when there is nothing to say).

_STARTER_EXTS = (".md", ".txt")
_KEBAB = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*$")


def _starter_file(name):
    """Path of the starter file NAME (case-insensitive; same files and order as
    load_starters), or None."""
    try:
        files = sorted(os.listdir(STARTERS_DIR), key=lambda f: (f.lower(), f))
    except OSError:
        return None
    for fname in files:
        stem, ext = os.path.splitext(fname)
        path = os.path.join(STARTERS_DIR, fname)
        if (not fname.startswith(".") and ext.lower() in _STARTER_EXTS
                and stem.lower() == name.lower() and os.path.isfile(path)):
            return path
    return None


def _name_problem(name):
    """Why NAME can't be a new starter's name (a short message), or "" if it can."""
    if not name:
        return "empty name"
    if not _KEBAB.match(name):
        return f"'{name}': use lowercase letters, digits and single hyphens"
    if _starter_file(name):
        return f"'{name}' already exists"
    return ""


def _run_editor(path):
    """Open PATH in $VISUAL / $EDITOR (else nvim, else vi) and wait. Returns a
    message if it couldn't launch or exited non-zero, else ""."""
    editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR")
              or ("nvim" if shutil.which("nvim") else "vi"))
    try:
        rc = subprocess.run([*shlex.split(editor), path], check=False).returncode
    except (OSError, ValueError) as e:
        return f"could not launch editor '{editor}': {e}"
    return f"editor exited {rc}" if rc else ""


def _add_starter(name):
    """Create STARTERS_DIR/NAME.md (and the directory), edit it, and keep it only
    if it ends up non-empty. NAME must already have passed _name_problem."""
    path = os.path.join(STARTERS_DIR, name + ".md")
    try:
        os.makedirs(STARTERS_DIR, exist_ok=True)
        open(path, "x").close()             # "x": never overwrite a file made meanwhile
    except OSError as e:
        return f"could not create {name}.md: {e}"
    note = _run_editor(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            empty = not f.read().strip()
    except OSError:
        empty = True                        # the editor removed or renamed it
    if empty:
        try:
            os.unlink(path)                 # our own empty file: nothing to trash
        except OSError:
            pass
        return note or f"{name}: empty, discarded"
    return note or f"added {name}"


def _edit_starter(name):
    path = _starter_file(name)
    return _run_editor(path) if path else f"{name}: file not found"


def _trash_starter(name):
    """Move the starter's file to the Trash with `trash`; refuses (rather than
    deleting for good) when `trash` isn't on PATH."""
    path = _starter_file(name)
    if not path:
        return f"{name}: file not found"
    trash = shutil.which("trash")
    if not trash:
        return "`trash` not found on PATH: not deleted"
    if subprocess.run([trash, path], check=False, capture_output=True).returncode:
        return f"trash failed: {name} not deleted"
    return f"moved {name} to Trash"


def _ask_new_name():
    """Ask for a new starter's name (cooked mode). Returns (name, "") or ("", why)."""
    try:
        name = input("New starter name (lowercase-kebab-case, empty to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return "", "cancelled"
    problem = _name_problem(name)
    return ("", problem) if problem else (name, "")


# Picker drawing. Everything goes out as plain ANSI: alt screen, cursor
# addressing, ESC[K, and a few SGR codes. Text is sanitized first, and no row is
# ever drawn into the last column, so the terminal never autowraps or scrolls.
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")    # C0/C1 controls but \t, \n
_DEL_SGR, _INS_SGR = "31;9", "32;4"     # diff view: only in A (red, struck) / only in B (green, underlined)


def _clean(s):
    """Text safe to draw or print: tabs become 4 spaces and every other control
    character except newline is dropped, so a prompt file can't move the cursor
    or restyle the screen."""
    return _CTRL.sub("", s.replace("\t", "    "))


def _one_line(s):
    return " ".join(_clean(s).split())      # all whitespace → single spaces


def _clip(s, w):
    """s cut to at most w display columns, ending in … when it had to be cut."""
    if w < 1:
        return ""
    out, n = [], 0
    for ch in s:
        cw = _display_width(ch)
        if n + cw > w:
            while out and n + 1 > w:        # make room for the …
                n -= _display_width(out.pop())
            return "".join(out) + "…"
        out.append(ch)
        n += cw
    return s


def _fit(s, w):
    """s clipped to w columns, then space-padded to exactly w."""
    s = _clip(s, w)
    return s + " " * (w - _display_width(s))


def _width(pieces):
    return sum(_display_width(t) for _, t in pieces)


def _styled(pieces):
    """One wrapped line, [(sgr, text), ...], as a string with its SGR spans."""
    return "".join(f"\033[{sgr}m{text}\033[0m" if sgr else text for sgr, text in pieces)


def _hard_split(pieces, width):
    """An over-long word [(sgr, text), ...] cut into lines of at most `width`
    columns (a wide character is never split)."""
    lines, cur, curw = [], [], 0
    for sgr, text in pieces:
        for ch in text:
            cw = _display_width(ch)
            if cur and curw + cw > width:
                lines.append(cur)
                cur, curw = [], 0
            cur.append((sgr, ch))
            curw += cw
    return lines + [cur]


def _wrap(chunks, width):
    """Greedy word-wrap of [(sgr, text), ...] chunks into lines (lists of
    (sgr, text) pieces) of at most `width` columns. A newline always breaks, so
    blank lines survive; indentation is kept; spaces at a wrap point are dropped;
    a word wider than a line is hard-split. A word may span chunks ("hello" + ",")."""
    toks = []                       # (kind, pieces): "w"ord, "s"pace or "n"ewline
    for sgr, text in chunks:
        for m in re.finditer(r"\n|[^\S\n]+|\S+", text):
            s = m.group()
            kind = "n" if s == "\n" else "s" if s.isspace() else "w"
            if kind == "w" and toks and toks[-1][0] == "w":
                toks[-1][1].append((sgr, s))
            else:
                toks.append((kind, [(sgr, s)]))
    lines, cur, curw, gap, gapw = [], [], 0, [], 0    # gap: spaces before the next word
    for kind, pieces in toks:
        if kind == "n":
            lines.append(cur)
            cur, curw, gap, gapw = [], 0, [], 0
            continue
        w = _width(pieces)
        if kind == "s":
            gap, gapw = gap + pieces, gapw + w
        elif curw + gapw + w <= width:
            cur, curw = cur + gap + pieces, curw + gapw + w
            gap, gapw = [], 0
        else:                       # no room: wrap, dropping the gap
            if cur:
                lines.append(cur)
            parts = _hard_split(pieces, width) if w > width else [pieces]
            lines += parts[:-1]
            cur = parts[-1]
            curw = _width(cur)
            gap, gapw = [], 0
    if cur:
        lines.append(cur)
    return lines


@functools.lru_cache(maxsize=8)     # recomputed only when the focus or mark moves
def _diff_chunks(a, b):
    """Word-level diff of prompt a (the marked one) against b (the focused one):
    ([(sgr, text), ...] for _wrap, number of changes). Only word and punctuation
    tokens are compared, so whitespace-only differences never show. b's own text
    stands for matches and additions (additions green + underlined); a's words
    that b lacks come first, red + struck through. Only the tokens are styled,
    never the whitespace between them."""
    ta, tb = (re.findall(r"(\w+|[^\w\s])(\s*)", _clean(s).strip()) for s in (a, b))
    ops = difflib.SequenceMatcher(None, [t for t, _ in ta], [t for t, _ in tb],
                                  autojunk=False).get_opcodes()
    chunks, changes = [], 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            chunks.append(("", "".join(t + w for t, w in tb[j1:j2])))
            continue
        changes += 1
        if tag in ("delete", "replace"):
            if chunks and not chunks[-1][1][-1:].isspace():
                chunks.append(("", " "))        # a deletion must not glue onto the word before
            for t, _ in ta[i1:i2]:
                chunks += [(_DEL_SGR, t), ("", " ")]
        if tag in ("insert", "replace"):
            for t, w in tb[j1:j2]:
                chunks += [(_INS_SGR, t)] + ([("", w)] if w else [])
    return chunks, changes


def _list_rows(names, previews, focus, mark, top, count, cols):
    """The picker's list rows top..top+count: cursor and mark markers, one space, a
    shared name column, then the preview; the focused row in reverse video."""
    nw = min(max(_display_width(s) for s in names), 24, max(8, (cols - 6) // 3))
    out = []
    for i in range(top, top + count):
        text = (("▸" if i == focus else " ") + ("●" if i == mark else " ") + " "
                + _fit(names[i], nw) + "  " + _clip(previews[i], cols - 6 - nw))
        if i == focus:
            text = f"\033[7m{text}{' ' * (cols - 1 - _display_width(text))}\033[0m"
        out.append(text)
    return out


def _footer(cols, diff):
    """The footer row: the dim key legend, cut at the last item that fits. In diff
    mode it starts with the colour legend, `only in A` / `only in B` drawn in the
    two diff styles, which fits at every usable width."""
    pieces = [("2", " ")]
    if diff:
        pieces += [(_DEL_SGR, "only in A"), ("2", " · "), (_INS_SGR, "only in B")]
    for key in ("↑↓ move", "Enter select", "a add · e edit · d del", "m mark/compare",
                "^D/^U scroll", "Esc skip"):
        item = ("2", (" · " if len(pieces) > 1 else "") + key)
        if _width(pieces + [item]) > cols - 1:
            break
        pieces.append(item)
    return _styled(pieces)


def _read_key(fd, size):
    """The next key press: 'up' 'down' 'pgup' 'pgdn' 'esc' 'eof', 'ignored' for
    an unbound escape sequence, else the character itself. None once the window
    size no longer matches `size`. Reads ONE byte at a time (more only inside an
    escape sequence), so type-ahead after Enter stays in the tty queue for the
    REPL's input()."""
    while not select.select([fd], [], [], 0.25)[0]:
        if shutil.get_terminal_size() != size:
            return None
    b = os.read(fd, 1)
    if not b:
        return "eof"
    if b != b"\x1b":
        return b.decode("latin-1")

    def more():                     # next byte of the sequence; b"" if none follows
        return os.read(fd, 1) if select.select([fd], [], [], 0.05)[0] else b""

    c = more()
    if not c or c == b"\x1b":
        return "esc"                # a bare Esc (or two in a row), not a sequence
    if c == b"O":                   # SS3: one more byte
        return {b"A": "up", b"B": "down"}.get(more(), "ignored")
    if c != b"[":
        return "ignored"            # Alt-<key> or some other escape
    params = b""
    while True:                     # CSI: parameter bytes, then a final byte @..~
        c = more()
        if not c or 0x40 <= c[0] <= 0x7E:
            break
        params += c
    return {(b"", b"A"): "up", (b"", b"B"): "down",
            (b"5", b"~"): "pgup", (b"6", b"~"): "pgdn"}.get((params, c), "ignored")


def _picker_rows(starters):
    """(names, previews) for the list rows; row 0 is `(none)`."""
    return (["(none)"] + [_one_line(name) for name, _, _ in starters],
            ["no starter — just the global pre-prompt"] + [
                _one_line(desc or body) for _, desc, body in starters])


def _pick_loop(fd, starters, suspend):
    """The picker's event loop, with the terminal already raw. Returns the chosen
    (name, body), or None for no starter. Row 0 is `(none)`; starter i is row i.
    A full frame is drawn after every key and every size change. `a`/`e`/`d` add,
    edit and delete starters; `suspend()` is a context manager that hands the
    terminal back (cooked, normal screen) for the prompt and the editor. Any
    change reloads the library and clears the mark."""
    n = len(starters)
    names, previews = _picker_rows(starters)
    focus, mark, top, scroll = 0, None, 0, 0    # mark: a row >= 1 to compare against
    prev_size = prev_view = None
    note = ""                                   # one-frame message in the footer
    while True:
        size = shutil.get_terminal_size()
        cols, rows = size.columns, size.lines
        small = cols < 30 or rows < 8
        if small:
            screen = [_clip("terminal too small — Enter or Esc: no starter", cols - 1)]
        else:
            list_h = min(n + 1, max(3, rows // 3))
            pane_h = rows - list_h - 2              # the rest: header, pane, footer
            width = min(cols - 2, 100)
            diff = mark is not None and focus not in (0, mark)
            if focus == 0:
                lines = _wrap([("2", "No starter prompt: chat starts with just the "
                                     "global pre-prompt."
                                     + ("" if n else " Press a to add one."))], width)
            elif diff:
                chunks, changes = _diff_chunks(starters[mark - 1][2], starters[focus - 1][2])
                lines = _wrap(chunks, width)
            else:
                lines = _wrap([("", _clean(starters[focus - 1][2]))], width)
            view = (focus, mark if diff else None)
            if view != prev_view:                   # new content: back to the top
                prev_view = view
                scroll = 0
                if diff:                            # ...a diff: just above its first change
                    scroll = max(0, next((i for i, l in enumerate(lines)
                                          if any(s for s, _ in l)), 0) - 1)
            max_scroll = max(0, len(lines) - pane_h)
            scroll = min(scroll, max_scroll)
            top = max(0, min(max(top, focus - list_h + 1), focus, n + 1 - list_h))
            if diff:
                head = f"diff  A: {names[mark]} → B: {names[focus]} · {changes} change(s)"
            elif focus == 0:
                head = f"{names[0]} (0/{n})"
            elif focus == mark:
                head = f"{names[focus]} ({focus}/{n}) · marked — move to another prompt to compare"
            else:
                pct = 100 * scroll // max_scroll if max_scroll else 100
                head = f"{names[focus]} ({focus}/{n}) · {len(starters[focus - 1][2])} chars · {pct}%"
            pane = [" " + _styled(l) for l in lines[scroll:scroll + pane_h]]
            screen = (_list_rows(names, previews, focus, mark, top, list_h, cols)
                      + [f"\033[1m {_clip(head, cols - 2)}\033[0m"]
                      + pane + [""] * (pane_h - len(pane))
                      + [_styled([("33", " " + _clip(note, cols - 3))]) if note
                         else _footer(cols, diff)])
        screen += [""] * (rows - len(screen))
        sys.stdout.write(("\033[2J" if size != prev_size else "") + "".join(
            f"\033[{r};1H{text}\033[K" for r, text in enumerate(screen, 1)))
        sys.stdout.flush()
        prev_size = size

        key = _read_key(fd, size)
        if key is None:                             # the window was resized
            continue
        note = ""
        if key in ("\r", "\n"):
            return None if small or focus == 0 else (
                starters[focus - 1][0], starters[focus - 1][2])
        if key in ("esc", "eof", "\x03"):
            return None
        if small:
            continue
        if key in ("up", "k", "\x10"):
            focus = max(0, focus - 1)
        elif key in ("down", "j", "\x0e"):
            focus = min(n, focus + 1)
        elif key == "m" and focus:                  # not on (none)
            mark = None if mark == focus else focus
        elif key in ("\x04", "\x15", "pgdn", "pgup"):
            step = {"\x04": pane_h // 2, "\x15": -(pane_h // 2),
                    "pgdn": pane_h - 1, "pgup": 1 - pane_h}[key]
            scroll = max(0, min(max_scroll, scroll + step))
        elif key == "a" or (key in ("e", "d") and focus):
            target = starters[focus - 1][0] if focus else ""
            if key == "a":
                with suspend():
                    target, note = _ask_new_name()
                    if target:
                        note = _add_starter(target)
            elif key == "e":
                with suspend():
                    note = _edit_starter(target)
            else:                                   # confirm in the footer row
                sys.stdout.write(f"\033[{rows};1H\033[1m "
                                 + _clip(f"Move '{names[focus]}' to the Trash? [y/N]", cols - 3)
                                 + "\033[0m\033[K")
                sys.stdout.flush()
                note = (_trash_starter(target) if _read_key(fd, size) in ("y", "Y")
                        else "cancelled")
            starters = load_starters()
            n = len(starters)
            names, previews = _picker_rows(starters)
            mark = prev_view = prev_size = None     # indices moved; redraw from scratch
            focus = next((i for i, s in enumerate(starters, 1)
                          if s[0].lower() == target.lower()), min(focus, n))


def _pick_starter(starters):
    """Let the user pick one of `starters` full-screen (and add, edit or delete
    them); return (name, body), or None for no starter. Also None, silently, when
    there is no usable terminal (not a TTY, TERM unset/dumb, termios refuses); an
    empty library still opens the picker, so `a` can add the first one. The
    terminal is always put back: alt screen, cursor, attributes, SIGTERM/SIGHUP
    handlers."""
    if (not (sys.stdin.isatty() and sys.stdout.isatty())
            or os.environ.get("TERM", "") in ("", "dumb")):
        return None
    fd = sys.stdin.fileno()
    sigs = (signal.SIGTERM, signal.SIGHUP)
    prev = [signal.getsignal(s) for s in sigs]
    old = None                      # the original attributes, once read
    entered = False                 # the alt screen may be on

    def die(signum, frame):         # unwind through the finally below, then exit
        raise SystemExit(128 + signum)

    @contextlib.contextmanager
    def suspend():                  # cooked mode + normal screen; picker state back after
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        try:
            yield
        finally:                    # also on an exception: the outer finally restores for good
            termios.tcsetattr(fd, termios.TCSADRAIN, raw)
            sys.stdout.write("\033[?1049h\033[?25l")
            sys.stdout.flush()

    try:
        for s in sigs:
            signal.signal(s, die)
        try:
            old, raw = termios.tcgetattr(fd), termios.tcgetattr(fd)
            raw[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR)
            raw[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            raw[6][termios.VMIN], raw[6][termios.VTIME] = 1, 0
            termios.tcsetattr(fd, termios.TCSADRAIN, raw)     # DRAIN: never drop type-ahead
        except (termios.error, OSError):
            return None             # no usable terminal: skip silently
        entered = True
        sys.stdout.write("\033[?1049h\033[?25l")              # alt screen, hide cursor
        return _pick_loop(fd, starters, suspend)
    except KeyboardInterrupt:       # an external SIGINT; Ctrl-C itself arrives as a key
        return None
    finally:
        # Nested, so a signal landing in the write can't skip the steps after it.
        # Nothing polls or reads the tty afterwards: bytes typed ahead must stay
        # queued, untouched, for the REPL's input().
        try:
            if entered:
                sys.stdout.write("\033[0m\033[?25h\033[?1049l")
                sys.stdout.flush()
        except Exception:
            pass
        finally:
            try:
                if old is not None:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass
            finally:
                for s, h in zip(sigs, prev):
                    signal.signal(s, h or signal.SIG_DFL)


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
    p.add_argument("--no-search", action="store_true",
                   help="don't offer the model web search (on by default); "
                        "the REPL's /search toggles it mid-session")
    g = p.add_mutually_exclusive_group()             # REPL only
    g.add_argument("--starter", metavar="NAME", default=None,
                   help="start with this prompt from chat-prompts/ (name or unique "
                        "prefix, case-insensitive) appended to the system prompt; "
                        "skips the picker")
    g.add_argument("--no-starter", action="store_true",
                   help="use no starter prompt and skip the picker")
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
                                          effort=args.effort,
                                          search=not args.no_search)
            elapsed = time.monotonic() - t0
        except RuntimeError as e:
            print(f"ask: {e}", file=sys.stderr)
            return 1
        except Exception as e:      # never a traceback
            print(f"ask: unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        if args.stats:
            _print_stats(elapsed, usage)
        text = re.sub(r"\033\[[0-9;]*m", "", text)   # drop marker styling
        # Sources are part of the answer (a piped answer keeps them), so they
        # go to stdout right after it.
        text += _sources_block(usage.get("sources"))
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0

    if not args.system:
        print("chat: --system is required for the REPL.", file=sys.stderr)
        return 2

    # The starter prompt, (name, body) or None. A named one is resolved here, before
    # the key check or any terminal work, so a typo fails fast and locally.
    starter = None
    if args.starter is not None:
        try:
            name, _, body = find_starter(load_starters(), args.starter)
        except RuntimeError as e:
            print(f"chat: {e}", file=sys.stderr)
            return 2
        starter = (name, body)

    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("chat: no OPENROUTER_API_KEY in the environment.", file=sys.stderr)
        return 1

    if args.starter is None and not args.no_starter:
        starter = _pick_starter(load_starters())    # None when nothing to pick or no TTY
    # The starter rides along with the global prompt on every request — never in
    # `messages` — so /reset and /compact (which only touch history) keep it.
    system = args.system + "\n\n" + starter[1] if starter else args.system

    messages = []       # the entire conversation, re-sent each turn; in memory only.
    queued = []         # /paste'd (or image-only) attachments waiting for the next
                        # message; load_image() dicts, cleared once a turn succeeds.
    last_usage = {}     # usage object from the most recent turn (for /usage + trigger)
    pending_file = file_block   # attached to the first message the user actually
                                 # sends; None once consumed. Set only from --file.
    compact_enabled = not args.no_compact
    search_enabled = not args.no_search     # /search flips it
    compact_threshold = (args.compact_threshold if args.compact_threshold is not None
                         else COMPACT_THRESHOLD)   # an explicit flag wins

    if not MDCAT:
        print("chat: mdcat not found — printing raw Markdown "
              "(brew install mdcat to render).", file=sys.stderr)
    # Session banner: states the effort in effect (the shell always passes one).
    effort_note = f" · effort {args.effort}" if args.effort else ""
    compact_note = "" if compact_enabled else " · auto-compact off"
    latency_note = " · latency off" if args.no_latency else ""
    search_note = " · search off" if args.no_search else ""
    file_note = f" · file: {os.path.basename(args.file)}" if pending_file else ""
    starter_note = f" · starter: {starter[0]}" if starter else ""
    print(_ansi("2", f"chat · {LABEL}{effort_note}{compact_note}{latency_note}{search_note}{file_note}{starter_note} · "
                      "'exit'/'quit'/Ctrl-D to leave · "
                      "/help for commands"),
          file=sys.stderr)
    _print_system(args.system, starter)

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
        if msg == "/help":
            for line in REPL_HELP.split("\n"):     # per line, like _print_system
                print(_ansi("2", line), file=sys.stderr)
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
                      f"provider={u.get('provider', '?').lower()}"
                      + "".join(f"  {f}" for f in _search_fields(u)))
            continue
        if msg == "/system":
            _print_system(args.system, starter)     # no API call, history untouched
            continue
        if msg == "/search" or msg.startswith("/search "):
            arg = msg[8:].strip().lower()
            if arg in ("on", "off"):
                search_enabled = arg == "on"
            elif arg:
                print(_ansi("2", "usage: /search [on|off]"), file=sys.stderr)
                continue
            print(_ansi("2", f"web search {'on' if search_enabled else 'off'}."),
                  file=sys.stderr)
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
        # Every known command above is matched exactly (case-sensitive); anything
        # else shaped like a command is rejected here rather than sent to the model.
        # This must stay BEFORE /edit: its result falls through to the send path
        # and an edited message that starts with "/" is deliberate text.
        if msg != "/edit" and not msg.startswith("/edit ") and _is_slash_command_word(msg):
            print(_ansi("2", f"unknown command: {msg.split(None, 1)[0]}"), file=sys.stderr)
            for hline in REPL_HELP.split("\n"):
                print(_ansi("2", hline), file=sys.stderr)
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
                reply, last_usage = call_openrouter(key, MODEL, system,
                                                    messages, effort=args.effort,
                                                    search=search_enabled)
                elapsed = time.monotonic() - t_api
        except RuntimeError as e:
            messages.pop()          # drop the unanswered turn → history stays valid
            print(_ansi("2", f"chat: {e}"), file=sys.stderr)
            continue
        except KeyboardInterrupt:
            messages.pop()
            print(_ansi("2", "cancelled."), file=sys.stderr)
            continue
        except Exception as e:      # a bug or odd response must not end the session
            messages.pop()
            print(_ansi("2", f"chat: unexpected error: {type(e).__name__}: {e}"),
                  file=sys.stderr)
            continue

        messages.append({"role": "assistant", "content": reply})
        # Consume the pending file (and queued images) only now — after a confirmed
        # successful turn. The error/KeyboardInterrupt branches above `continue`
        # before reaching here, so a failed or cancelled send leaves them set and
        # the retry still carries them.
        pending_file = None
        queued.clear()
        print()                              # space between your line and the reply
        print(model_chip)                    # ` deepseek ` header chip
        # Sources are shown with the reply but never stored: history keeps the bare text.
        shown = render(reply + _sources_block(last_usage.get("sources")), bar)
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
            # Default footer (API wall time, + search count); --stats' line already has it.
            n = last_usage.get("searches", 0)
            note = f", {n} search{'' if n == 1 else 'es'}" if n > 0 else ""
            print(_ansi("2", f"[latency {elapsed:.2f}s{note}]"), file=sys.stderr)

        # Auto-compaction. Done AFTER rendering so it never delays the current reply.
        # A successful compaction drops the prompt well below threshold, so it can't
        # re-fire until the conversation grows large again.
        # Skipped after a search turn: its prompt_tokens includes search results that
        # never enter history, so it overstates the real context.
        if compact_enabled and last_usage and not last_usage.get("searches"):
            # prompt_tokens is the whole prompt, cached or not, so it IS the
            # effective input.
            effective_input = last_usage.get("prompt_tokens", 0)
            if isinstance(effective_input, int) and effective_input >= compact_threshold:
                new = compact(key, messages)
                if new is not None:
                    messages = new

    return 0


if __name__ == "__main__":
    sys.exit(main())
