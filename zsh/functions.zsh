# Shell functions

# ask: one-shot question to an LLM, streamed straight to the terminal.
# Requires: llm (with the anthropic plugin).
#
#   ask "..."            one-shot, Sonnet 4.6 (default), very terse
#   ask -o "..."         escalate to Opus        (--opus)
#   ask -v "..."         fuller-but-tight answer (--verbose); composes with -o
#
# Answers are Markdown (raw syntax in the terminal). For an interactive,
# Markdown-RENDERED, multi-turn REPL, use `chat` (defined below) instead.
#
# ISOLATION — ask backs tools (e.g. the Anki "Ask" panel) and more to come.
# ask is now purely one-shot and stateless: it has no session concept at all,
# so there is nothing for a tool to start, see, join, or pollute. Guarantees:
#   * _ask_oneshot is a stateless query core with zero session logic. Tools
#     call it directly, or call plain `ask` — which is just flag-parsing + that.
#   * No shared or persistent conversation state exists on this path: no
#     `llm -c`/`--continue` "most recent conversation" pointer (which any
#     process could clobber), and nothing on disk. Multi-turn lives entirely in
#     `chat`, a separate, interactive-only process with in-memory history.
#
# Model aliases verified against `llm models` (anthropic plugin) at build time.

# Shared system prompts — single source of truth; the one-shot path (`ask`) and
# the REPL (`chat`) reuse these verbatim, so there is no copy-paste drift. `chat`
# passes the chosen one into its Python backend via --system, so the text is
# never duplicated outside this file. Both always request Markdown; the terse
# one stays tight so short replies don't bloat.
_ASK_SYS_TERSE='You are a terminal assistant. Answer in the fewest words possible. No preamble, no sign-off, no restating the question, no caveats unless essential. Always format the answer as Markdown, even one-liners; wrap code in fenced blocks with a language tag.'
_ASK_SYS_VERBOSE='You are a terminal assistant. Answer very concisely, but completely. No preamble or filler. Always format the answer as Markdown; wrap code in fenced blocks with a language tag.'

# Absolute path to chat's Python backend, resolved from THIS file's own location
# (the same idiom zshrc uses to find the repo) so `chat` works regardless of
# whether $DOTFILES is set. functions.zsh lives in zsh/, so the repo root is two
# directories up; the backend lives in bin/.
_CHAT_BACKEND="${${(%):-%x}:A:h:h}/bin/chat-repl.py"

# _ask_oneshot: the stateless core. Knows nothing about sessions — bare `llm`
# with an explicit model + system prompt. This is what tools call directly,
# e.g.  ASK_TOOL=anki-ask _ask_oneshot claude-sonnet-4.6 "$sys" "the prompt"
# Usage: _ask_oneshot <model> <system-prompt> <prompt...>
_ask_oneshot() {
  local model="$1" sys="$2"; shift 2
  llm -m "$model" -s "$sys" "$*"
}

ask() {
  local sys="$_ASK_SYS_TERSE"
  local model="claude-sonnet-4.6"   # default (llm alias)

  # Parse leading flags; combinable in any order. All are stateless, so they are
  # honored everywhere — interactive shells and tool callers alike.
  while [[ "$1" == -* ]]; do
    case "$1" in
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      -o|--opus)    model="claude-opus-4.8" ;;    # most capable
      *) break ;;
    esac
    shift
  done

  # One-shot, stateless. Tools land here too.
  _ask_oneshot "$model" "$sys" "$*"
}

# chat: interactive, multi-turn LLM REPL with Markdown-RENDERED replies — the
# readable counterpart to ask's raw one-shot output.
# Requires: python3 + mdcat. Reuses ask's shared prompts and Anthropic key.
#
#   chat            multi-turn, Sonnet 4.6 (default), terse
#   chat -o         escalate to Opus        (--opus)
#   chat -v         fuller-but-tight answers (--verbose); composes with -o
#
# Model + verbosity are fixed at launch (mirroring ask's flags). In the REPL:
# `/reset` wipes the context; `exit`/`quit`/Ctrl-D leave. Nothing is written to
# disk, so relaunching is a clean reset. The backend holds history in memory and
# re-sends it each turn (so Opus gets pricey on long chats — see the heads-up).
#
# ISOLATION — like ask, chat must never be startable by a tool, so it is
# interactive-ONLY: it refuses unless stdin+stdout are a TTY and ASK_TOOL is
# unset. The backend (bin/chat-repl.py) is a separate process holding its own
# in-memory history; it shares nothing with the one-shot path. The Anthropic key
# is passed via the environment (never argv, which `ps` can see; never to disk).
chat() {
  # Hard interactive guard — a tool (no TTY and/or ASK_TOOL set) can never reach
  # the REPL; it should be using one-shot `ask` instead.
  [[ -t 0 && -t 1 && -z "$ASK_TOOL" ]] || {
    print -u2 "chat: interactive use only — tools should call \`ask\` (one-shot)."
    return 1
  }

  local sys="$_ASK_SYS_TERSE"
  # Real Anthropic API ids (NOT llm aliases): the backend calls the Messages API
  # directly. Verified against `llm models`; the same ids the aliases resolve to.
  local model="claude-sonnet-4-6"   # default

  while [[ "$1" == -* ]]; do
    case "$1" in
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      -o|--opus)    model="claude-opus-4-8" ;;    # most capable
      *) break ;;
    esac
    shift
  done

  [[ "$model" == claude-opus-* ]] && \
    print -u2 "chat: heads-up — every turn re-sends the whole history, so Opus gets pricey; the default (Sonnet) is cheaper for long chats."

  # Reuse llm's stored Anthropic key (an explicit env override wins). Resolved
  # here and handed to the backend via the environment only.
  local key="${ANTHROPIC_API_KEY:-$(llm keys get anthropic 2>/dev/null)}"
  [[ -n "$key" ]] || {
    print -u2 "chat: no Anthropic key — set ANTHROPIC_API_KEY or run \`llm keys set anthropic\`."
    return 1
  }

  [[ -f "$_CHAT_BACKEND" ]] || {
    print -u2 "chat: backend not found at $_CHAT_BACKEND"
    return 1
  }

  ANTHROPIC_API_KEY="$key" python3 "$_CHAT_BACKEND" --model "$model" --system "$sys"
}

# scan2pdf: turn photos of pages into a cleaned-up, scanned-looking PDF.
# Usage: scan2pdf homework.pdf page1.jpg page2.jpg   (or scan2pdf homework.pdf *.jpg)
# Requires: imagemagick (magick) and img2pdf.
scan2pdf() {
  local out="$1"; shift
  local tmp; tmp=$(mktemp -d)
  local i=1
  for f in "$@"; do
    magick "$f" \( +clone -blur 0x20 \) -compose divide -composite \
      -normalize -sigmoidal-contrast 3,50% -alpha off \
      "$tmp/$(printf '%03d' $i).png"
    ((i++))
  done
  img2pdf "$tmp"/*.png -o "$out"
  rm -rf "$tmp"
  echo "Created $out"
}

# ia: open files in iA Writer (which ships no CLI of its own).
# Usage: ia notes.md            (open a file)
#        ia draft.md ideas.md   (open several)
#        ia                      (just launch iA Writer)
# Requires: iA Writer.app.
ia() {
  if [[ $# -eq 0 ]]; then
    open -a "iA Writer"
  else
    open -a "iA Writer" "$@"
  fi
}
