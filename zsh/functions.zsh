# Shell functions

# ask: question to an LLM, streamed straight to the terminal.
# Requires: llm (with the anthropic plugin).
#
#   ask "..."            one-shot, Haiku (fast/cheap), very terse
#   ask -s "..."         escalate to Sonnet      (--sonnet)
#   ask -o "..."         escalate to Opus        (--opus)
#   ask -v "..."         fuller-but-tight answer (--verbose); composes with -s/-o
#   ask --chat           interactive multi-turn session (terminal only)
#
# Every answer comes back as Markdown (read as raw syntax in the terminal).
#
# ISOLATION — ask backs tools (e.g. the Anki "Ask" panel) and more to come.
# A tool must NEVER start, see, join, or pollute a session. Guarantees:
#   * _ask_oneshot is a stateless query core with zero session logic. Tools
#     call this (or plain `ask`, which routes here when non-interactive).
#   * --chat is only RECOGNIZED from an interactive TTY with ASK_TOOL unset.
#     Tools run via a subprocess with piped stdio (no TTY) and/or set ASK_TOOL,
#     so they can never trip it; a stray "--chat ..." from a tool is just text.
#   * `llm -c`/`--continue` (the shared "most recent conversation" pointer that
#     any process could clobber) appears NOWHERE. Sessions use llm chat's own
#     in-process context. No session-state file is ever written.
#
# Model aliases verified against `llm models` (anthropic plugin) at build time.

# Shared system prompts — single source of truth; the one-shot path and the
# session path reuse these verbatim, so there is no copy-paste drift. Both
# always request Markdown; the terse one stays tight so short replies don't bloat.
_ASK_SYS_TERSE='You are a terminal assistant. Answer in the fewest words possible. No preamble, no sign-off, no restating the question, no caveats unless essential. Always format the answer as Markdown, even one-liners; wrap code in fenced blocks with a language tag.'
_ASK_SYS_VERBOSE='You are a terminal assistant. Answer very concisely, but completely. No preamble or filler. Always format the answer as Markdown; wrap code in fenced blocks with a language tag.'

# _ask_oneshot: the stateless core. Knows nothing about sessions — bare `llm`
# with an explicit model + system prompt. This is what tools call directly,
# e.g.  ASK_TOOL=anki-ask _ask_oneshot claude-haiku-4.5 "$sys" "the prompt"
# Usage: _ask_oneshot <model> <system-prompt> <prompt...>
_ask_oneshot() {
  local model="$1" sys="$2"; shift 2
  llm -m "$model" -s "$sys" "$*"
}

ask() {
  local sys="$_ASK_SYS_TERSE"
  local model="claude-haiku-4.5"   # fast, cheap default
  local chat=0

  # May we run session ops? Only from an interactive terminal with no tool
  # sentinel. `llm chat` needs a TTY anyway; this turns "no TTY / is a tool"
  # into a hard, explicit guard so a tool can never open a session.
  local interactive=0
  [[ -t 0 && -t 1 && -z "$ASK_TOOL" ]] && interactive=1

  # Parse leading flags; combinable in any order.
  while [[ "$1" == -* ]]; do
    case "$1" in
      # Stateless flags — honored anywhere (tools included).
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      -s|--sonnet)  model="claude-sonnet-4.6" ;;  # more capable
      -o|--opus)    model="claude-opus-4.8" ;;    # most capable; last of -s/-o wins
      # Stateful flag — a session trigger ONLY in the interactive context.
      # From a tool / non-TTY, stop parsing so it's treated as plain prompt text
      # and the query runs one-shot. (Never opens a session.)
      --chat) (( interactive )) && { chat=1; shift; }; break ;;
      *) break ;;
    esac
    shift
  done

  # Session mode: hand off to llm chat's own in-process REPL. Separate process,
  # own in-memory history, no shared pointer — invisible to every tool. Model
  # and verbosity are fixed at launch; close with `exit`/`quit`, start fresh by
  # running `ask --chat` again (a new process = a clean context = a cheap reset).
  if (( chat )); then
    [[ "$model" == claude-opus-* ]] && \
      print -u2 "ask: heads-up — sessions re-send history each turn, so Opus gets pricey; the default or -s is cheaper for long chats."
    llm chat -m "$model" -s "$sys"
    return
  fi

  # One-shot, stateless. Tools land here too.
  _ask_oneshot "$model" "$sys" "$*"
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
