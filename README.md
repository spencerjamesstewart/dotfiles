# dotfiles

My personal macOS / zsh configuration, kept modular and version-controlled.
See `install.sh` and `zsh/`. Secrets live in `~/.zshrc.local` (gitignored),
not in this repo. License: MIT.

Claude Code config (agents, `CLAUDE.md`, global settings) lives separately in
[claude-config](https://github.com/spencerjamesstewart/claude-config) (private).

## ask / chat — terminal LLM helpers

Two zsh functions (`zsh/functions.zsh`; the `chat` REPL is backed by
`bin/chat-repl.py`), both using one model, **deepseek/deepseek-v4.1-flash-1017**
via OpenRouter, for everything including `chat`'s compaction. There is no model
selection; the id is defined once in `bin/chat-repl.py`.

- `ask "..."` — one-shot question, rendered inline as Markdown (model badge +
  gutter bar). Non-interactive/piped/tool use prints raw Markdown instead.
- `chat` — interactive, multi-turn REPL with replies rendered via `mdcat`.

Flags (same for both): `-e`/`--effort low|medium|high` sets the thinking depth
(OpenRouter `reasoning.effort`; default `low`); `-v`/`--verbose` gives
fuller-but-tight answers; `--file f` attaches a text/Markdown/code file (or a PDF
via local `pdftotext`) as context; `--stats` prints dim `[stats]` lines to
stderr: `latency=` (API wall time) and token counts (`cached=` when reported),
plus `total=` — the full roundtrip from submitting the message to the reply
being on screen (pager dwell excluded). In `chat` it is one line under every
reply; in interactive `ask` the `total=` line follows the rendered answer (only
the render stage can time it). Independent of `-v`, and piped stdout stays
clean. `chat` additionally takes `--no-compact` and `--compact-threshold N`
(see below), and `--no-latency`. By default each `chat` reply ends with a dim
`[latency Ns]` footer on stderr (the API wall time, plus the search count when it
searched; `--stats` replaces it with its fuller line), and `--no-latency` hides
it. `--no-search` turns off web search (below), for both. `-h`/`--help` prints
usage for either.

The default system prompt is extra-terse (just the answer, one line when
possible). `-v` swaps it for a fuller-but-tight one. `chat` prints the prompt in
use as a dim `system · …` block at startup, and `/system` shows it again.

Neither uses the `llm` CLI; both POST directly to OpenRouter (OpenAI Chat
Completions format, stdlib-only `urllib`). The key comes from
`OPENROUTER_API_KEY` — export it in `~/.zshrc.local`. Every request (text,
image, search, compaction) tries Together first and falls back only within a
pool of providers verified for image input and tool calling (`PROVIDERS` in
`bin/chat-repl.py`; re-probe if the model changes).

**Web search** is on by default: the model gets OpenRouter's server-side search
tool (Exa) and decides when to use it. A search costs about $0.007 and adds
~3s. Replies that used it end with a
`Sources:` list (on stdout, so a piped `ask` answer keeps it; never stored in
`chat` history), `--stats`/`/usage` add `searches=` and `cost=`, and the
`chat` footer reads `[latency 3.44s, 2 searches]`. `--no-search` (either
command) or `/search off` (in `chat`) disables it; compaction never searches.

### chat REPL commands

Inside the `chat` REPL:

- `/edit` — compose the next message in `$EDITOR` (`$VISUAL` takes precedence;
  neovim here). Save and quit to send; quit with an empty buffer (or a non-zero
  exit like vim `:cq`) to cancel. `/edit some text` pre-fills the buffer. This is
  the way to send a **multi-line paste or code block**: macOS readline (libedit)
  has no bracketed-paste support, so pasting straight into the prompt submits it
  line by line — composing in the editor sidesteps that.
- `/reset` — clear the conversation context without leaving the process.
- `/compact` — summarize older turns now, regardless of the threshold (works even
  under `--no-compact`).
- `/usage` — print the last turn's token counts (input, output, cached), the
  serving provider, and `searches=`/`cost=` if it searched.
- `/search [on|off]` — show or set web search for the rest of the session.
- `/system` — re-print the system prompt in use (it is also shown at startup);
  makes no API request.
- `/paste` — queue the clipboard image (Finder-copied file, screenshot) for the
  next message; type your text after it. `/detach` drops queued images.
- `exit` / `quit` / `Ctrl-D` — leave.

**Images.** Drag an image file (PNG, JPEG, GIF, WebP, HEIC) onto the prompt, or
`/paste` one. Each is checked and downscaled locally (long side ≤ 1344 px, via
macOS `sips`; originals untouched), listed as a dim `attached:` line, and sent
inline. Dragged images ask `send? [Y/n]` first. Images stay in the context for
later turns (compaction keeps only an `[image: name]` note for old ones).

Replies render inline; one that overflows the current pane opens in `less`
(top-anchored, `q` to continue) instead of scrolling you to the bottom. Nothing
is persisted — relaunching `chat` is a clean reset.

### Caching & auto-compaction (chat)

Multi-turn `chat` re-sends the whole history each turn, but keeps it cheap two
ways:

- **Prompt caching** — the serving providers prefix-cache the re-sent history
  automatically; nothing to configure. Check `cached` with `/usage`.
- **Auto-compaction** — once the effective input crosses `--compact-threshold`
  (default **35000** tokens), the older turns are summarized into a single block
  by the same model at low effort, keeping the last two exchanges verbatim. On
  by default; `chat --no-compact` disables the automatic trigger (`/compact`
  still works); the check is skipped after a search turn, whose token count
  includes search results that never enter history. A summary that comes back
  empty, refused or truncated is rejected and history is left unchanged.
  Summaries live only in memory — relaunching is still a clean reset.

`ask` is a stateless one-shot and is unaffected by both.
