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
capable model); `-v`/`--verbose` gives fuller-but-tight answers and composes
with `-f`.

Both call the Anthropic Messages API directly (no `llm` CLI). The key comes from
`ANTHROPIC_API_KEY`, else `~/.config/anthropic/key` (one line, `chmod 600`).

### chat REPL commands

Inside the `chat` REPL:

- `/edit` — compose the next message in `$EDITOR` (`$VISUAL` takes precedence;
  neovim here). Save and quit to send; quit with an empty buffer (or a non-zero
  exit like vim `:cq`) to cancel. `/edit some text` pre-fills the buffer. This is
  the way to send a **multi-line paste or code block**: macOS readline (libedit)
  has no bracketed-paste support, so pasting straight into the prompt submits it
  line by line — composing in the editor sidesteps that.
- `/reset` — clear the conversation context without leaving the process.
- `exit` / `quit` / `Ctrl-D` — leave.

Replies render inline; one that overflows the current pane opens in `less`
(top-anchored, `q` to continue) instead of scrolling you to the bottom. Nothing
is persisted — relaunching `chat` is a clean reset.
