# dotfiles

My personal macOS / zsh configuration, kept modular and version-controlled.
See `install.sh` and `zsh/`. Secrets live in `~/.zshrc.local` (gitignored),
not in this repo. License: MIT.

## ask / chat — terminal LLM helpers

Two zsh functions (`zsh/functions.zsh`; the `chat` REPL is backed by
`bin/chat-repl.py`), both defaulting to **Claude Sonnet 4.6**:

- `ask "..."` — one-shot question, streamed to the terminal as raw Markdown.
- `chat` — interactive, multi-turn REPL with replies rendered via `mdcat`.

Flags (same for both): `-o`/`--opus` switches to **Claude Opus 4.8**;
`-v`/`--verbose` gives fuller-but-tight answers and composes with `-o`.

`ask` runs through the `llm` CLI; `chat` calls the Anthropic Messages API
directly (needs `ANTHROPIC_API_KEY`, or a key from `llm keys set anthropic`).
