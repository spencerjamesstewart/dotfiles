# dotfiles

My personal macOS / zsh configuration, kept modular and version-controlled.
See `install.sh` and `zsh/`. Secrets live in `~/.zshrc.local` (gitignored),
not in this repo. License: MIT.

## ask / chat — terminal LLM helpers

Two zsh functions (`zsh/functions.zsh`; the `chat` REPL is backed by
`bin/chat-repl.py`), both defaulting to **Claude Sonnet 5**:

- `ask "..."` — one-shot question, rendered inline as Markdown (model badge +
  gutter bar). Non-interactive/piped/tool use prints raw Markdown instead.
- `chat` — interactive, multi-turn REPL with replies rendered via `mdcat`.

Flags (same for both): `-f`/`--fable` switches to **Claude Fable 5** (the most
capable model); `-g`/`--gpt-oss` switches to **gpt-oss-120b** via OpenRouter,
pinned to the fast Groq/Cerebras providers (cheap and quick); `-v`/`--verbose`
gives fuller-but-tight answers and composes with any model flag; `--stats`
prints one dim `[stats]` line per API call to stderr (wall time + token counts,
`cached=` when reported) — independent of `-v`, and piped stdout stays clean.
`chat` additionally takes `--no-compact` and `--compact-threshold N` (see
below).

Default terseness is model-dependent: Haiku, Sonnet, and gpt-oss get an
extra-terse system prompt (just the answer, one line when possible); Opus and
Fable keep the standard terse prompt. `-v` overrides both.

Both call the Anthropic Messages API directly (no `llm` CLI). The key comes from
`ANTHROPIC_API_KEY`, else `~/.config/anthropic/key` (one line, `chmod 600`).
`-g` talks to OpenRouter instead (OpenAI Chat Completions format, same
stdlib-only `urllib` path); its key comes from `OPENROUTER_API_KEY` — export it
in `~/.zshrc.local`.

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
- `/usage` — print the last turn's token counts (input, output, cache-creation,
  cache-read; for `-g` it's input, output, cached).
- `exit` / `quit` / `Ctrl-D` — leave.

Replies render inline; one that overflows the current pane opens in `less`
(top-anchored, `q` to continue) instead of scrolling you to the bottom. Nothing
is persisted — relaunching `chat` is a clean reset.

### Caching & auto-compaction (chat)

Multi-turn `chat` re-sends the whole history each turn, but keeps it cheap two
ways:

- **Prompt caching** — the re-sent prefix is marked cacheable, so once a
  conversation clears the ~1024-token minimum, repeat turns bill the prefix at
  the cache-read rate (~10% of input price). Check it with `/usage`. Anthropic
  path only: `-g` sends no `cache_control` blocks — Groq/Cerebras prefix-cache
  automatically server-side with nothing to configure.
- **Auto-compaction** — once the effective input crosses `--compact-threshold`
  (default **8000** tokens; **35000** for `-g`, where the motive is interactive
  latency and prefix-cache hygiene rather than cost), the older turns are
  summarized into a single block by a cheap one-shot (Haiku on the Anthropic
  path, gpt-oss itself on `-g`), keeping the last two exchanges verbatim. On by
  default; `chat --no-compact` disables the automatic trigger (`/compact` still
  works). Summaries live only in memory — relaunching is still a clean reset.

`ask` is a stateless one-shot and is unaffected by both.
