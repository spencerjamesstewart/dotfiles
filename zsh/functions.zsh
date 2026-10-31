# Shell functions

# ask: one-shot question to an LLM.
# Requires: python3 + an OpenRouter API key in $OPENROUTER_API_KEY; mdcat for
# the rendered look. One model for everything (ask, chat, and chat's
# compaction): deepseek/deepseek-v4.1-flash-1017 via OpenRouter, defined once as
# MODEL in bin/chat-repl.py. There is no model selection.
#
#   ask "..."            one-shot, very terse, effort low
#   ask -e <level> "..." effort: low (default) | medium | high (--effort) —
#                        the thinking-depth/token-spend dial (OpenRouter
#                        reasoning.effort)
#   ask -v "..."         fuller-but-tight answer (--verbose)
#   ask --stats "..."    dim [stats] lines on stderr: latency + token counts from
#                        the API call (cached= when the backend reports it), and
#                        — interactive only — a total= line under the answer:
#                        the full hit-enter → answer-on-screen roundtrip.
#                        Independent of -v; stderr-only, piped stdout stays clean.
#   ask --file f "..."   prepend a file's contents as context ahead of the
#                        question (text/Markdown/code as UTF-8, or a PDF via the
#                        LOCAL `pdftotext` — brew install poppler; no cloud
#                        parsing, no OCR). Reading/validation happens in the
#                        Python backend; only the path crosses argv/env. A bad
#                        path (missing, a directory, an unsupported image/audio/
#                        video extension, non-UTF-8 binary) fails fast with no
#                        API call. One file only; --file with no question is an
#                        error.
#   ask --no-search "..." no web search. By default the model may search the web
#                        (OpenRouter's server-side search; ~$0.007/search, +~3s)
#                        and a trailing "Sources:" list follows the answer, on
#                        stdout, so a piped answer keeps it. --stats adds
#                        searches= and cost= when it searched.
#
# In an interactive shell the answer is Markdown-RENDERED through chat's backend —
# same look as `chat`: a model badge, a coloured gutter bar, and orange headings
# (rendering needs the whole answer, so it buffers instead of streaming; a `…` hint
# covers the wait). Any NON-interactive use — output piped/redirected (stdout not a
# TTY) or a tool caller (ASK_TOOL set) — stays on the raw path: no rendering, the
# same text as before, but when the model searched the answer ends with a Sources:
# list (--no-search or ASK_NO_SEARCH=1 for a plain answer). For a multi-turn
# version of the same look, use `chat` (below).
#
# ISOLATION — ask backs tools (e.g. the Anki "Ask" panel) and more to come.
# ask is now purely one-shot and stateless: it has no session concept at all,
# so there is nothing for a tool to start, see, join, or pollute. Guarantees:
#   * _ask_oneshot is a stateless query core with zero session logic. Tools
#     call it directly, or call plain `ask` — which is just flag-parsing + that.
#   * No shared or persistent conversation state exists on this path: each call
#     is an independent request with nothing on disk to clobber.
#     Multi-turn lives entirely in `chat`, a separate, interactive-only process
#     with in-memory history.
#
# ask and chat POST directly via bin/chat-repl.py — no llm CLI, no SDKs — to
# OpenRouter (OpenAI Chat Completions format, keyed by $OPENROUTER_API_KEY —
# export it in ~/.zshrc.local). Every request tries Together first and falls back
# only within a verified provider pool (PROVIDERS in bin/chat-repl.py).

# Shared system prompts — single source of truth; the one-shot path (`ask`) and
# the REPL (`chat`) reuse these verbatim, so there is no copy-paste drift. `chat`
# passes the chosen one into its Python backend via --system, so the text is
# never duplicated outside this file.
#   DEFAULT — the answer and nothing else, one line when possible (the model
#             runs to verbosity otherwise).
#   VERBOSE — -v: fuller but still no filler.
_ASK_SYS_DEFAULT='You are a terminal assistant. Be extremely terse: give only the answer itself, in as few words as it allows — a single line whenever possible. No preamble, no sign-off, no restating the question, no caveats, and no explanation or surrounding context unless explicitly asked. Use Markdown only where it genuinely helps: fenced code blocks with a language tag for commands and code, a short bullet list for several parallel items. Everything else is plain text.'
_ASK_SYS_VERBOSE='You are a terminal assistant. Answer very concisely, but completely. No preamble or filler. Always format the answer as Markdown; wrap code in fenced blocks with a language tag.'

# Absolute path to chat's Python backend, resolved from THIS file's own location
# (the same idiom zshrc uses to find the repo) so `chat` works regardless of
# whether $DOTFILES is set. functions.zsh lives in zsh/, so the repo root is two
# directories up; the backend lives in bin/.
_CHAT_BACKEND="${${(%):-%x}:A:h:h}/bin/chat-repl.py"

# _ask_help / _chat_help: the single source of each command's --help text.
# Plain text to stdout; handled before any TTY/key checks, so no network, key,
# or terminal is needed. Keep in sync with the flag lists in the headers above.
_ask_help() {
  cat <<'EOF'
Usage: ask [flags] "question"

One-shot question to an LLM (deepseek via OpenRouter).

Flags:
  -h, --help          show this help
  -e, --effort LEVEL  thinking depth: low (default) | medium | high
  -v, --verbose       fuller-but-tight answer instead of the terse default
  --stats             dim [stats] lines on stderr (latency, tokens, total)
  --file PATH         prepend a text/Markdown/code file (or PDF) as context
  --no-search         don't let the model search the web (on by default)

Requires OPENROUTER_API_KEY (export it in ~/.zshrc.local).
Output is Markdown-rendered in a terminal, raw when piped or ASK_TOOL is set.

Examples:
  ask "tar flags to extract a .tgz"
  ask -e high --file notes.md "summarize the open questions"
EOF
}

_chat_help() {
  cat <<'EOF'
Usage: chat [flags]

Interactive multi-turn LLM REPL with Markdown-rendered replies.
Interactive terminals only; tools should use `ask`.

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

In the REPL:
  /edit [text]      compose the next message in $EDITOR (multi-line input)
  /reset            clear the conversation context
  /compact          summarize older turns now
  /usage            show the last turn's token counts
  /system           show the system prompt in use (also printed at startup)
  /paste            queue the clipboard image for the next message
  /detach           drop queued images
  /search [on|off]  show or set web search (on by default)
  exit, quit        leave (Ctrl-D also works)

Images: drag an image file onto the prompt (PNG, JPEG, GIF, WebP, HEIC) or use
/paste. Images are downscaled locally to 1344px on the long side, shown as
`attached:` lines (dragged ones ask `send? [Y/n]`), and stay in context for
later turns.

Web search: the model decides when to search (~$0.007/search, +~3s); replies
that used it end with a Sources list. --stats and /usage add searches= and cost=.
Requests go to Together first, falling back within a verified provider pool
(PROVIDERS in bin/chat-repl.py).

Requires OPENROUTER_API_KEY (export it in ~/.zshrc.local).

Example:
  chat -e medium --file spec.md
EOF
}

# _ask_oneshot: the stateless core. Knows nothing about sessions — it checks
# the key, then POSTs a single request through the Python backend's --oneshot
# mode, printing the raw answer. This is what tools call directly,
# e.g.  ASK_TOOL=anki-ask _ask_oneshot "$sys" "the prompt"
# Effort rides the ASK_EFFORT env var (low|medium|high; default low) rather than
# a positional arg. ASK_STATS=1 (set by ask --stats) turns on the backend's
# per-call [stats] line — stderr-only.
# ASK_NO_SEARCH=1 (set by ask --no-search) forwards --no-search; web search is ON
# by default, so tool callers that want a plain, source-free answer set it.
# ASK_FILE (set by ask --file) forwards a path to the backend's --file, which
# does all the reading/validation — only the path crosses this boundary, never
# file contents, so a huge PDF never risks ARG_MAX.
# Usage: [ASK_EFFORT=<level>] [ASK_STATS=1] [ASK_NO_SEARCH=1] [ASK_FILE=<path>] _ask_oneshot <system-prompt> <prompt...>
_ask_oneshot() {
  local sys="$1"; shift
  local -a stats_args file_args search_args
  [[ -n "$ASK_STATS" ]] && stats_args=(--stats)
  [[ -n "$ASK_NO_SEARCH" ]] && search_args=(--no-search)
  [[ -n "$ASK_FILE" ]] && file_args=(--file "$ASK_FILE")
  [[ -n "$OPENROUTER_API_KEY" ]] || {
    print -u2 "ask: no OpenRouter key — export OPENROUTER_API_KEY (in ~/.zshrc.local)."
    return 1
  }
  # The key must travel via the environment only (never argv, which `ps` can
  # see); self-assignment exports it to the child even if ~/.zshrc.local forgot
  # the `export`. `--` stops the backend's option parsing, so a prompt starting
  # with `-` is safe.
  OPENROUTER_API_KEY="$OPENROUTER_API_KEY" python3 "$_CHAT_BACKEND" \
    --oneshot --system "$sys" --effort "${ASK_EFFORT:-low}" \
    "${stats_args[@]}" "${file_args[@]}" "${search_args[@]}" -- "$*"
}

ask() {
  local sys="$_ASK_SYS_DEFAULT"
  local effort="low"                # low = terse/fast/cheap; -e raises it
  local stats=""                    # --stats → per-call [stats] line on stderr
  local file=""                     # --file <path> → forwarded as ASK_FILE
  local no_search=""                # --no-search → forwarded as ASK_NO_SEARCH
  local -a codes                    # pipestatus snapshot below; declared here —
                                     # NOT after the pipe — because `local` is
                                     # itself a command and running one resets
                                     # $pipestatus before it can be read
  local code                        # loop var for the snapshot scan (kept out
                                     # of the interactive shell's namespace)

  # Parse leading flags; combinable in any order. All are stateless, so they are
  # honored everywhere — interactive shells and tool callers alike.
  while [[ "$1" == -* ]]; do
    case "$1" in
      -h|--help)    _ask_help; return 0 ;;
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      --stats)      stats=1 ;;
      --no-search)  no_search=1 ;;
      --file)
        shift
        [[ -n "$1" ]] || { print -u2 "ask: --file needs a path."; return 1 }
        file="$1" ;;
      -e|--effort)
        shift
        case "$1" in
          low|medium|high) effort="$1" ;;
          *) print -u2 "ask: -e needs low, medium, or high."; return 1 ;;
        esac ;;
      *) break ;;
    esac
    shift
  done

  # Interactive shell → render the answer with the chat look (badge + gutter bar +
  # headings) via the shared backend's --render mode. Everything else — a tool
  # (ASK_TOOL set) or piped/redirected output (stdout not a TTY) — falls through to
  # the raw one-shot path untouched, so tool callers and pipelines are unaffected.
  # (Piping into --render naturally buffers the full answer first.)
  if [[ -t 1 && -z "$ASK_TOOL" && -f "$_CHAT_BACKEND" ]] && command -v python3 >/dev/null 2>&1; then
    # --stats also rides the render stage: it prints the total= roundtrip line
    # (enter → answer on screen), which only the downstream process can time.
    ASK_EFFORT="$effort" ASK_STATS="$stats" ASK_FILE="$file" ASK_NO_SEARCH="$no_search" _ask_oneshot "$sys" "$*" \
      | python3 "$_CHAT_BACKEND" --render ${stats:+--stats}
    # A left-side failure (bad --file, no key, API error) exits the LEFT
    # command non-zero, but `$?` after a pipeline is the RIGHT command's exit
    # code by default — which is 0 even when the left side printed an error
    # and produced no output. $pipestatus holds every stage's code; return the
    # first non-zero one so a pipeline failure is never reported as success.
    codes=("${pipestatus[@]}")
    for code in "${codes[@]}"; do
      (( code != 0 )) && return $code
    done
    return 0
  else
    ASK_EFFORT="$effort" ASK_STATS="$stats" ASK_FILE="$file" ASK_NO_SEARCH="$no_search" _ask_oneshot "$sys" "$*"   # raw — tools/pipes land here
  fi
}

# chat: interactive, multi-turn LLM REPL with Markdown-RENDERED replies — the
# readable counterpart to ask's raw one-shot output.
# Requires: python3 + mdcat. Reuses ask's shared prompts and OpenRouter key; same
# single model (see ask above).
#
#   chat            multi-turn, very terse, effort low
#   chat -e <level> effort: low (default) | medium | high (--effort)
#   chat -v         fuller-but-tight answers (--verbose)
#   chat --stats    one dim [stats] line under every reply: latency (API wall time),
#                   total (message sent → reply on screen; pager dwell excluded),
#                   and token counts. stderr-only.
#   chat --file f   attach a file's contents as context to the FIRST message
#                   typed in the session only — not re-sent on later turns (it
#                   lives on in history from there, like any other turn). Same
#                   extraction rules as ask --file (text/Markdown/code as UTF-8,
#                   PDF via local pdftotext). A bad path fails at startup, before
#                   the REPL opens; the banner shows the attached filename.
#
# The startup banner states the effort in play for the session (plus an
# `auto-compact off` / `latency off` / `search off` note when those flags are
# given) and is followed by the system prompt in use, as a dim `system · …` block.
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
#
# Verbosity and effort are fixed at launch (mirroring ask's flags). In the REPL:
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
# ISOLATION — like ask, chat must never be startable by a tool, so it is
# interactive-ONLY: it refuses unless stdin+stdout are a TTY and ASK_TOOL is
# unset. The backend (bin/chat-repl.py) is a separate process holding its own
# in-memory history; it shares nothing with the one-shot path. The API key is
# passed via the environment (never argv, which `ps` can see; never to disk).
chat() {
  # --help first: chat takes no positional args, so any -h/--help wins, even
  # without a TTY or key.
  local arg
  for arg in "$@"; do
    [[ "$arg" == (-h|--help) ]] && { _chat_help; return 0 }
  done

  # Hard interactive guard — a tool (no TTY and/or ASK_TOOL set) can never reach
  # the REPL; it should be using one-shot `ask` instead.
  [[ -t 0 && -t 1 && -z "$ASK_TOOL" ]] || {
    print -u2 "chat: interactive use only — tools should call \`ask\` (one-shot)."
    return 1
  }

  local sys="$_ASK_SYS_DEFAULT"
  local effort="low"              # low = terse/fast/cheap; -e raises it
  local stats=""                  # --stats → per-turn [stats] line on stderr
  local file=""                   # --file <path> → forwarded as --file to the backend
  local -a compact_args           # compaction flags forwarded to the backend
  local -a latency_args           # --no-latency, forwarded to the backend
  local -a search_args            # --no-search, forwarded to the backend

  while [[ "$1" == -* ]]; do
    case "$1" in
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      --stats)      stats=1 ;;
      --file)
        shift
        [[ -n "$1" ]] || { print -u2 "chat: --file needs a path."; return 1 }
        file="$1" ;;
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
      *) break ;;
    esac
    shift
  done

  local -a stats_args file_args
  [[ -n "$stats" ]] && stats_args=(--stats)
  [[ -n "$file" ]] && file_args=(--file "$file")

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
    "${compact_args[@]}" "${latency_args[@]}" "${search_args[@]}" "${stats_args[@]}" "${file_args[@]}"
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
