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
stderr: `ttft=` (API wall time) and token counts (`cached=` when reported),
plus `total=` — the full roundtrip from submitting the message to the reply
being on screen (pager dwell excluded). In `chat` it is one line under every
reply; in interactive `ask` the `total=` line follows the rendered answer (only
the render stage can time it). Independent of `-v`, and piped stdout stays
clean. `chat` additionally takes `--no-compact` and `--compact-threshold N`
(see below).

The default system prompt is extra-terse (just the answer, one line when
possible). `-v` swaps it for a fuller-but-tight one.

Neither uses the `llm` CLI; both POST directly to OpenRouter (OpenAI Chat
Completions format, stdlib-only `urllib`). The key comes from
`OPENROUTER_API_KEY` — export it in `~/.zshrc.local`.

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
- `/usage` — print the last turn's token counts (input, output, cached) and the
  serving provider.
- `exit` / `quit` / `Ctrl-D` — leave.

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
  still works). Summaries live only in memory — relaunching is still a clean
  reset.

`ask` is a stateless one-shot and is unaffected by both.
