# Shell functions

# ask: one-shot question to an LLM.
# Requires: python3 + an Anthropic API key (see the key file below); mdcat for
# the rendered look.
#
#   ask "..."            one-shot, Sonnet 5 (default), very terse, effort low
#   ask -h "..."         drop to Haiku 4.5       (--haiku, fastest/cheapest)
#   ask -o "..."         escalate to Opus 4.8    (--opus)
#   ask -f "..."         escalate to Fable 5     (--fable, most capable)
#   ask -e <level> "..." effort: low (default) | medium | high (--effort) —
#                        the API's thinking-depth/token-spend dial; not valid
#                        with -h (Haiku rejects the parameter)
#   ask -v "..."         fuller-but-tight answer (--verbose); composes with any model flag
#
# Default terseness is model-dependent: Haiku and Sonnet get an extra-terse
# prompt (they run long otherwise); Opus and Fable keep the standard terse one.
# -v overrides both.
#
# In an interactive shell the answer is Markdown-RENDERED through chat's backend —
# same look as `chat`: a model badge, a coloured gutter bar, and orange headings
# (rendering needs the whole answer, so it buffers instead of streaming; a `…` hint
# covers the wait). Any NON-interactive use — output piped/redirected (stdout not a
# TTY) or a tool caller (ASK_TOOL set) — stays on the raw streaming path, byte-for-
# byte unchanged. For a multi-turn version of the same look, use `chat` (below).
#
# ISOLATION — ask backs tools (e.g. the Anki "Ask" panel) and more to come.
# ask is now purely one-shot and stateless: it has no session concept at all,
# so there is nothing for a tool to start, see, join, or pollute. Guarantees:
#   * _ask_oneshot is a stateless query core with zero session logic. Tools
#     call it directly, or call plain `ask` — which is just flag-parsing + that.
#   * No shared or persistent conversation state exists on this path: each call
#     is an independent Messages API request with nothing on disk to clobber.
#     Multi-turn lives entirely in `chat`, a separate, interactive-only process
#     with in-memory history.
#
# Model ids are real Anthropic API ids (dashes, e.g. claude-sonnet-5); ask and
# chat POST to the Messages API directly via bin/chat-repl.py — no llm CLI.

# Shared system prompts — single source of truth; the one-shot path (`ask`) and
# the REPL (`chat`) reuse these verbatim, so there is no copy-paste drift. `chat`
# passes the chosen one into its Python backend via --system, so the text is
# never duplicated outside this file.
# Three tiers:
#   VERY_TERSE — default for Haiku and Sonnet, which run to verbosity otherwise:
#                the answer and nothing else, one line when possible.
#   TERSE      — default for Opus and Fable (already well-calibrated): tight but
#                lightly structured Markdown.
#   VERBOSE    — -v on any model: fuller but still no filler.
_ASK_SYS_VERY_TERSE='You are a terminal assistant. Be extremely terse: give only the answer itself, in as few words as it allows — a single line whenever possible. No preamble, no sign-off, no restating the question, no caveats, and no explanation or surrounding context unless explicitly asked. Use Markdown only where it genuinely helps: fenced code blocks with a language tag for commands and code, a short bullet list for several parallel items. Everything else is plain text.'
_ASK_SYS_TERSE='You are a terminal assistant. Keep answers tight — no preamble, no sign-off, no restating the question, no caveats unless essential. Always format the answer as Markdown, and use light structure where it helps: bullet or numbered lists for multiple items, **bold** for key terms, and fenced code blocks with a language tag for commands and code. Only a genuinely trivial reply — a single word or a short one-liner — should be plain text.'
_ASK_SYS_VERBOSE='You are a terminal assistant. Answer very concisely, but completely. No preamble or filler. Always format the answer as Markdown; wrap code in fenced blocks with a language tag.'

# _ask_default_sys: pick the default system prompt for a model — the very-terse
# tier for Haiku/Sonnet, the standard terse tier for everything else. Used by
# ask and chat when -v wasn't given; the choice must happen AFTER flag parsing,
# since the model isn't known until then.
_ask_default_sys() {
  case "$1" in
    claude-haiku-*|claude-sonnet-*) print -r -- "$_ASK_SYS_VERY_TERSE" ;;
    *)                              print -r -- "$_ASK_SYS_TERSE" ;;
  esac
}

# Absolute path to chat's Python backend, resolved from THIS file's own location
# (the same idiom zshrc uses to find the repo) so `chat` works regardless of
# whether $DOTFILES is set. functions.zsh lives in zsh/, so the repo root is two
# directories up; the backend lives in bin/.
_CHAT_BACKEND="${${(%):-%x}:A:h:h}/bin/chat-repl.py"

# Anthropic API key file: a single line, chmod 600. An explicit ANTHROPIC_API_KEY
# in the environment overrides it. (Replaces the old `llm keys` lookup.)
_ANTHROPIC_KEY_FILE="${ANTHROPIC_KEY_FILE:-$HOME/.config/anthropic/key}"

# _anthropic_key: print the API key — env override first, else the key file's
# first line. Silent (empty output) if neither is set; callers report the error.
_anthropic_key() {
  if [[ -n "$ANTHROPIC_API_KEY" ]]; then
    print -r -- "$ANTHROPIC_API_KEY"
  elif [[ -r "$_ANTHROPIC_KEY_FILE" ]]; then
    local k; read -r k < "$_ANTHROPIC_KEY_FILE" && print -r -- "$k"
  fi
}

# _ask_oneshot: the stateless core. Knows nothing about sessions — it resolves
# the key, then POSTs a single Messages API request through the Python backend's
# --oneshot mode, printing the raw answer. This is what tools call directly,
# e.g.  ASK_TOOL=anki-ask _ask_oneshot claude-sonnet-5 "$sys" "the prompt"
# NOTE: <model> must be a real Anthropic API id (dashes), NOT an llm alias.
# Effort rides the ASK_EFFORT env var (low|medium|high; default low) rather than
# a new positional arg, so existing tool callers keep working unchanged. Haiku
# doesn't support the effort parameter (the API rejects it with a 400), so it is
# omitted for claude-haiku-* models regardless of ASK_EFFORT.
# Usage: [ASK_EFFORT=<level>] _ask_oneshot <model> <system-prompt> <prompt...>
_ask_oneshot() {
  local model="$1" sys="$2"; shift 2
  local key; key="$(_anthropic_key)"
  [[ -n "$key" ]] || {
    print -u2 "ask: no Anthropic key — set ANTHROPIC_API_KEY or write it to $_ANTHROPIC_KEY_FILE."
    return 1
  }
  local -a effort_args
  [[ "$model" == claude-haiku-* ]] || effort_args=(--effort "${ASK_EFFORT:-low}")
  # `--` stops the backend's option parsing, so a prompt starting with `-` is safe.
  ANTHROPIC_API_KEY="$key" python3 "$_CHAT_BACKEND" \
    --oneshot --model "$model" --system "$sys" "${effort_args[@]}" -- "$*"
}

ask() {
  local sys=""                      # resolved after flag parsing (model-dependent)
  local model="claude-sonnet-5"     # default: balanced speed + intelligence
  local effort="low" effort_explicit=""   # low = terse/fast/cheap; -e raises it

  # Parse leading flags; combinable in any order. All are stateless, so they are
  # honored everywhere — interactive shells and tool callers alike.
  while [[ "$1" == -* ]]; do
    case "$1" in
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      -h|--haiku)   model="claude-haiku-4-5" ;;  # fastest/cheapest
      -o|--opus)    model="claude-opus-4-8" ;;
      -f|--fable)   model="claude-fable-5" ;;   # escalate to the most capable
      -e|--effort)
        shift
        case "$1" in
          low|medium|high) effort="$1"; effort_explicit=1 ;;
          *) print -u2 "ask: -e needs low, medium, or high."; return 1 ;;
        esac ;;
      *) break ;;
    esac
    shift
  done

  # No -v → model-dependent default: very terse for Haiku/Sonnet, terse for the rest.
  [[ -n "$sys" ]] || sys="$(_ask_default_sys "$model")"

  # Haiku doesn't support the effort parameter — the API rejects the request
  # with a 400. Fail loudly on an explicit -e rather than silently ignoring it;
  # without -e, Haiku just runs effort-less (the default can't apply to it).
  if [[ "$model" == claude-haiku-* && -n "$effort_explicit" ]]; then
    print -u2 "ask: -e can't be used with -h — Haiku 4.5 doesn't support the API's effort parameter (the request would be rejected). Drop -e, or pick Sonnet (default), -o, or -f."
    return 1
  fi

  # Interactive shell → render the answer with the chat look (badge + gutter bar +
  # headings) via the shared backend's --render mode. Everything else — a tool
  # (ASK_TOOL set) or piped/redirected output (stdout not a TTY) — falls through to
  # the raw one-shot path untouched, so tool callers and pipelines are unaffected.
  # (Piping into --render naturally buffers the full answer first.)
  if [[ -t 1 && -z "$ASK_TOOL" && -f "$_CHAT_BACKEND" ]] && command -v python3 >/dev/null 2>&1; then
    ASK_EFFORT="$effort" _ask_oneshot "$model" "$sys" "$*" \
      | python3 "$_CHAT_BACKEND" --render --model "$model"
  else
    ASK_EFFORT="$effort" _ask_oneshot "$model" "$sys" "$*"   # raw — tools/pipes land here
  fi
}

# chat: interactive, multi-turn LLM REPL with Markdown-RENDERED replies — the
# readable counterpart to ask's raw one-shot output.
# Requires: python3 + mdcat. Reuses ask's shared prompts and Anthropic key.
#
#   chat            multi-turn, Sonnet 5 (default), terse, effort low
#   chat -h         drop to Haiku 4.5       (--haiku, fastest/cheapest)
#   chat -o         escalate to Opus 4.8    (--opus)
#   chat -f         escalate to Fable 5     (--fable, most capable)
#   chat -e <level> effort: low (default) | medium | high (--effort); not valid
#                   with -h (Haiku rejects the parameter)
#   chat -v         fuller-but-tight answers (--verbose); composes with any model flag
#
# Default terseness matches ask: extra-terse for Haiku/Sonnet, standard terse
# for Opus/Fable; -v overrides both.
#
# The startup banner states the model and effort in play for the session.
#   chat --no-compact           keep the whole history; disable auto-compaction
#   chat --compact-threshold N  auto-compact once effective input ≥ N tokens (default 8000)
#
# Model + verbosity are fixed at launch (mirroring ask's flags). In the REPL:
# `/reset` wipes the context; `/edit` composes in $EDITOR; `/compact` summarizes
# older turns now; `/usage` prints the last turn's token counts; `exit`/`quit`/
# Ctrl-D leave. Nothing is written to disk, so relaunching is a clean reset.
#
# The backend holds history in memory and re-sends it each turn, but marks the
# re-sent prefix cacheable (cache-read billing, ~10% of input price) and
# auto-compacts older turns into a Haiku-written summary once the context crosses
# the threshold — so long chats stay cheap. Fable still costs more per token than
# the default (see the heads-up).
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

  local sys=""                    # resolved after flag parsing (model-dependent)
  # Real Anthropic API ids: the backend calls the Messages API directly.
  local model="claude-sonnet-5"   # default: balanced speed + intelligence
  local effort="low" effort_explicit=""   # low = terse/fast/cheap; -e raises it
  local -a compact_args           # compaction flags forwarded to the backend

  while [[ "$1" == -* ]]; do
    case "$1" in
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      -h|--haiku)   model="claude-haiku-4-5" ;;  # fastest/cheapest
      -o|--opus)    model="claude-opus-4-8" ;;
      -f|--fable)   model="claude-fable-5" ;;    # escalate to the most capable
      -e|--effort)
        shift
        case "$1" in
          low|medium|high) effort="$1"; effort_explicit=1 ;;
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
      *) break ;;
    esac
    shift
  done

  # No -v → model-dependent default: very terse for Haiku/Sonnet, terse for the rest.
  [[ -n "$sys" ]] || sys="$(_ask_default_sys "$model")"

  # Same rule as ask: Haiku has no effort parameter, so an explicit -e is an
  # error (the API would reject it), and the low default is simply not sent.
  if [[ "$model" == claude-haiku-* && -n "$effort_explicit" ]]; then
    print -u2 "chat: -e can't be used with -h — Haiku 4.5 doesn't support the API's effort parameter (the request would be rejected). Drop -e, or pick Sonnet (default), -o, or -f."
    return 1
  fi
  local -a effort_args
  [[ "$model" == claude-haiku-* ]] || effort_args=(--effort "$effort")

  # Cost heads-up only for models pricier than the default (Haiku is cheaper).
  local pricier=""
  case "$model" in
    claude-fable-5)  pricier="Fable" ;;
    claude-opus-4-8) pricier="Opus" ;;
  esac
  [[ -n "$pricier" ]] && \
    print -u2 "chat: heads-up — $pricier costs more per token than the default (Sonnet); prompt caching + auto-compaction soften long-chat cost, but Sonnet is still cheaper."

  # Resolve the key (env override, else the key file) and hand it to the backend
  # via the environment only — never argv (which `ps` can see) or disk.
  local key; key="$(_anthropic_key)"
  [[ -n "$key" ]] || {
    print -u2 "chat: no Anthropic key — set ANTHROPIC_API_KEY or write it to $_ANTHROPIC_KEY_FILE."
    return 1
  }

  [[ -f "$_CHAT_BACKEND" ]] || {
    print -u2 "chat: backend not found at $_CHAT_BACKEND"
    return 1
  }

  ANTHROPIC_API_KEY="$key" python3 "$_CHAT_BACKEND" \
    --model "$model" --system "$sys" "${effort_args[@]}" "${compact_args[@]}"
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
