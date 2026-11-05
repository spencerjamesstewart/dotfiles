# Shell functions

# chat: interactive, multi-turn LLM REPL (see chat() below).
# Requires: python3 + an OpenRouter API key in $OPENROUTER_API_KEY; mdcat for
# the rendered look. One model for everything (chat and its compaction):
# deepseek/deepseek-v4.1-flash-1017 via OpenRouter, defined once as MODEL in
# bin/chat-repl.py. There is no model selection.
#
# chat POSTs directly via bin/chat-repl.py — no llm CLI, no SDKs — to
# OpenRouter (OpenAI Chat Completions format, keyed by $OPENROUTER_API_KEY —
# export it in ~/.zshrc.local). Every request tries Together first and falls back
# only within a verified provider pool (PROVIDERS in bin/chat-repl.py).

# System prompts — single source of truth. `chat` passes the chosen one into its
# Python backend via --system, so the text is never duplicated outside this file.
#   DEFAULT — the answer and nothing else, one line when possible (the model
#             runs to verbosity otherwise).
#   VERBOSE — -v: fuller but still no filler.
_CHAT_SYS_DEFAULT='You are a terminal assistant. Be extremely terse: give only the answer itself, in as few words as it allows — a single line whenever possible. No preamble, no sign-off, no restating the question, no caveats, and no explanation or surrounding context unless explicitly asked. Use Markdown only where it genuinely helps: fenced code blocks with a language tag for commands and code, a short bullet list for several parallel items. Everything else is plain text.'
_CHAT_SYS_VERBOSE='You are a terminal assistant. Answer very concisely, but completely. No preamble or filler. Always format the answer as Markdown; wrap code in fenced blocks with a language tag.'

# Absolute path to chat's Python backend, resolved from THIS file's own location
# (the same idiom zshrc uses to find the repo) so `chat` works regardless of
# whether $DOTFILES is set. functions.zsh lives in zsh/, so the repo root is two
# directories up; the backend lives in bin/.
_CHAT_BACKEND="${${(%):-%x}:A:h:h}/bin/chat-repl.py"

# _chat_help: the single source of chat's --help text.
# Plain text to stdout; handled before any TTY/key checks, so no network, key,
# or terminal is needed. Keep in sync with the flag list in the chat header below.
_chat_help() {
  cat <<'EOF'
Usage: chat [flags]

Interactive multi-turn LLM REPL with Markdown-rendered replies.
Interactive terminals only.

Flags:
  -h, --help                  show this help
  -e, --effort LEVEL          thinking depth: low (default) | medium | high
  -v, --verbose               fuller-but-tight answers instead of terse
  --stats                     dim [stats] line under every reply
  --file PATH                 attach a text/Markdown/code file (or PDF) to the
                              first message
  --no-compact                disable automatic history compaction
  --compact-threshold N       auto-compact at N input tokens (default 35000)
  --no-latency                hide per-reply [latency Ns] footer (on by default)
  --no-search                 don't let the model search the web (on by default)
  -s, --starter NAME          start with that starter prompt (skips the picker)
  --no-starter                no starter prompt and no picker

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
  exit, quit        leave (Ctrl-D also works)

Images: drag an image file onto the prompt (PNG, JPEG, GIF, WebP, HEIC) or use
/paste. Images are downscaled locally to 1344px on the long side, shown as
`attached:` lines (dragged ones ask `send? [Y/n]`), and stay in context for
later turns.

Web search: the model decides when to search (~$0.007/search, +~3s); replies
that used it end with a Sources list. --stats and /usage add searches= and cost=.
Requests go to Together first, falling back within a verified provider pool
(PROVIDERS in bin/chat-repl.py).

Starter prompts: each .md/.txt file in chat-prompts/ (repo root) is standing
context for the session; an optional first line `description: ...` labels it.
A full-screen picker opens at startup (even when empty, so `a` can add one);
the top row, (none), is no starter. Keys:
  Up/Down, j/k, Ctrl-P/Ctrl-N   move
  PgUp/PgDn, Ctrl-U/Ctrl-D      scroll the text pane
  Enter                         select the highlighted row
  Esc, Ctrl-C                   skip the picker (no starter)
  m                             mark a prompt; move to another to see a
                                word-level diff against it (m again clears)
  a                             add: asks a name, then opens the editor
  e                             edit the highlighted prompt in the editor
  d                             delete it to the Trash, after a y/N confirm
Add/edit use $VISUAL, else $EDITOR, else nvim, else vi.
The chosen text is appended to the system prompt and kept across /reset and
/compact. -s picks by name (a unique prefix works, any case).

Requires OPENROUTER_API_KEY (export it in ~/.zshrc.local).

Example:
  chat -e medium --file spec.md
EOF
}

# chat: interactive, multi-turn LLM REPL with Markdown-RENDERED replies — a
# readable terminal chat with the model.
# Requires: python3 + mdcat, and the OpenRouter key (see the header above).
#
#   chat            multi-turn, very terse, effort low
#   chat -e <level> effort: low (default) | medium | high (--effort)
#   chat -v         fuller-but-tight answers (--verbose)
#   chat --stats    one dim [stats] line under every reply: latency (API wall time),
#                   total (message sent → reply on screen; pager dwell excluded),
#                   and token counts. stderr-only.
#   chat --file f   attach a file's contents as context to the FIRST message
#                   typed in the session only — not re-sent on later turns (it
#                   lives on in history from there, like any other turn). Text/
#                   Markdown/code is read as UTF-8, PDF via local pdftotext
#                   (brew install poppler; no cloud parsing, no OCR). A bad
#                   path fails at startup, before the REPL opens; the banner
#                   shows the attached filename.
#
# The startup banner states the effort in play for the session (plus an
# `auto-compact off` / `latency off` / `search off` note when those flags are
# given, and `starter: NAME` when a starter prompt is in use) and is followed by
# the system prompt in use, as a dim `system · …` block (a starter prompt follows
# it under a `starter: NAME` divider).
#   chat --no-compact           keep the whole history; disable auto-compaction
#   chat --compact-threshold N  auto-compact once effective input ≥ N tokens
#                               (default 35000)
#   chat --no-latency           hide the dim `[latency Ns]` footer (API wall time,
#                               stderr-only) printed under every reply by default;
#                               --stats folds it into its [stats] line instead
#   chat --no-search            start with web search off. By default the model
#                               may search the web (OpenRouter's server-side
#                               search; ~$0.007/search, +~3s); replies that used it
#                               end with a "Sources:" list, the footer counts the
#                               searches, and --stats and /usage add searches=/cost=.
#                               `/search [on|off]` shows or flips it in the REPL.
#                               Auto-compaction skips a search turn (its token
#                               count includes results that never enter history).
#   chat -s NAME                start with starter prompt NAME (--starter; skips the picker)
#   chat --no-starter           no starter prompt and no picker
#
# Verbosity and effort are fixed at launch. In the REPL:
# `/reset` wipes the context; `/edit` composes in $EDITOR; `/compact` summarizes
# older turns now; `/usage` prints the last turn's token counts; `/system` re-shows
# the system prompt (no API call); `/search [on|off]` shows or sets web search;
# `/paste` queues the clipboard image for the next message and `/detach` drops
# queued images (an image file dragged onto the prompt attaches too; both downscale
# to 1344px via sips and stay in context); `exit`/`quit`/Ctrl-D leave. Nothing is written to disk, so relaunching is a clean
# reset.
#
# The backend holds history in memory and re-sends it each turn; the provider
# prefix-caches automatically server-side, and older turns are auto-compacted
# into a summary (by the same model) once the context crosses the threshold, so
# long chats stay cheap.
#
# ISOLATION — chat must never be startable by a tool, so it is interactive-ONLY:
# it refuses unless stdin+stdout are a TTY. The backend (bin/chat-repl.py) is a
# separate process holding its own in-memory history. The API key is passed via
# the environment (never argv, which `ps` can see; never to disk).
chat() {
  # --help first: chat takes no positional args, so any -h/--help wins, even
  # without a TTY or key.
  local arg
  for arg in "$@"; do
    [[ "$arg" == (-h|--help) ]] && { _chat_help; return 0 }
  done

  # Hard interactive guard — a tool (no TTY) can never reach the REPL.
  [[ -t 0 && -t 1 ]] || {
    print -u2 "chat: interactive use only (needs a terminal on stdin and stdout)."
    return 1
  }

  local sys="$_CHAT_SYS_DEFAULT"
  local effort="low"              # low = terse/fast/cheap; -e raises it
  local stats=""                  # --stats → per-turn [stats] line on stderr
  local file=""                   # --file <path> → forwarded as --file to the backend
  local -a compact_args           # compaction flags forwarded to the backend
  local -a latency_args           # --no-latency, forwarded to the backend
  local -a search_args            # --no-search, forwarded to the backend
  local starter=""                # -s/--starter <name> → forwarded as --starter to the backend
  local -a starter_args=()        # --starter / --no-starter, forwarded to the backend

  while [[ "$1" == -* ]]; do
    case "$1" in
      -v|--verbose) sys="$_CHAT_SYS_VERBOSE" ;;
      --stats)      stats=1 ;;
      --file)
        shift
        [[ -n "$1" ]] || { print -u2 "chat: --file needs a path."; return 1 }
        file="$1" ;;
      -s|--starter)
        shift
        [[ -n "$1" ]] || { print -u2 "chat: --starter needs a name."; return 1 }
        starter="$1" ;;
      -e|--effort)
        shift
        case "$1" in
          low|medium|high) effort="$1" ;;
          *) print -u2 "chat: -e needs low, medium, or high."; return 1 ;;
        esac ;;
      --no-compact) compact_args+=(--no-compact) ;;   # off by request; no short form
      --compact-threshold)
        shift
        [[ "$1" == <-> ]] || {   # zsh glob for an integer; catches missing/non-numeric
          print -u2 "chat: --compact-threshold needs an integer token count."
          return 1
        }
        compact_args+=(--compact-threshold "$1") ;;
      --no-latency) latency_args+=(--no-latency) ;;   # no short form
      --no-search)  search_args+=(--no-search) ;;     # no short form
      --no-starter) starter_args+=(--no-starter) ;;   # no short form
      *) break ;;
    esac
    shift
  done

  local -a stats_args file_args
  [[ -n "$stats" ]] && stats_args=(--stats)
  [[ -n "$file" ]] && file_args=(--file "$file")
  [[ -n "$starter" ]] && starter_args+=(--starter "$starter")

  [[ -n "$OPENROUTER_API_KEY" ]] || {
    print -u2 "chat: no OpenRouter key — export OPENROUTER_API_KEY (in ~/.zshrc.local)."
    return 1
  }

  [[ -f "$_CHAT_BACKEND" ]] || {
    print -u2 "chat: backend not found at $_CHAT_BACKEND"
    return 1
  }

  # Self-assignment hands the key to the Python process via the environment
  # only, even if ~/.zshrc.local forgot the `export`.
  OPENROUTER_API_KEY="$OPENROUTER_API_KEY" python3 "$_CHAT_BACKEND" \
    --system "$sys" --effort "$effort" \
    "${compact_args[@]}" "${latency_args[@]}" "${search_args[@]}" "${stats_args[@]}" "${file_args[@]}" "${starter_args[@]}"
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
