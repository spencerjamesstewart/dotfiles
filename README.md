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
(see below), `--no-latency`, and `-s`/`--starter NAME` or `--no-starter` (see
Starter prompts). By default each `chat` reply ends with a dim
`[latency Ns]` footer on stderr (the API wall time, plus the search count when it
searched; `--stats` replaces it with its fuller line), and `--no-latency` hides
it. `--no-search` turns off web search (below), for both. `ask -h`/`chat -h`
(`--help`) print usage for each, with no API key or network needed.

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
- `/help` — list these commands (the startup banner points to it). `chat -h`
  shows the same list plus every flag and the picker keys.
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

### Starter prompts (chat)

A starter prompt is standing context for a whole session, kept as a text file in
`chat-prompts/` at the repo root (`.md` or `.txt`; the file name is the prompt's
name). An optional first line `description: ...` labels it in the picker; the
rest is the prompt itself, e.g. `I have neuroanatomy questions while I study for
my A&P class.` You rarely need to create these by hand: the picker can add, edit
and delete them (below).

`chat` opens a full-screen picker (stdlib only: raw
terminal mode and ANSI, no fzf or curses) before the first prompt. The chosen
text is appended to the system prompt, shown under a `starter: NAME` divider at
startup and by `/system`, and re-sent with every request, so it survives `/reset`
and `/compact`. Nothing is sent when you pick: it is context, not a question.
The top row, `(none)`, starts with just the global prompt. The picker opens even
when the library is empty (only `(none)`), so `a` can add the first prompt. Keys:

- `↑`/`↓`, `j`/`k`, `Ctrl-P`/`Ctrl-N` — move; `Enter` — select; `Esc` or
  `Ctrl-C` — skip the picker
- `Ctrl-D`/`Ctrl-U` (half a pane), `PgDn`/`PgUp` — scroll the text pane
- `m` — mark the highlighted prompt, then move to another one to see a word-level
  diff against it (red strikethrough: only in the marked prompt; green
  underline: only in the highlighted one); `m` on the marked prompt clears it,
  and `Enter` still selects the highlighted prompt
- `a` — add a prompt: asks for a name (lowercase letters, digits and hyphens;
  must not exist yet), then opens a new `chat-prompts/NAME.md` in
  `$VISUAL`/`$EDITOR` (else `nvim`, else `vi`). Saving it empty discards it.
- `e` — edit the highlighted prompt in the editor
- `d` — delete the highlighted prompt after a `y/N` confirmation. It goes to the
  Trash via the `trash` command; without `trash` on `PATH` nothing is deleted.
  `e` and `d` do nothing on `(none)`.

After `a`/`e`/`d` the picker reloads the library, clears any `m` mark, and keeps
running. Without a terminal (piped input) it is skipped as before.

`chat -s NAME` (`--starter`) picks a prompt by name (case-insensitive; a unique
prefix is enough) and skips the picker; `chat --no-starter` skips it and uses
none. A starter counts toward the context size, so a long one triggers
auto-compaction sooner.

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
