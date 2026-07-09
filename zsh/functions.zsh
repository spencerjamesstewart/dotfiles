# Shell functions

# ask: one-shot question to an LLM.
# Requires: python3 + an OpenRouter API key in $OPENROUTER_API_KEY (used by the
# default and by -g/-x/-d) — or, for -s/-h/-o/-f, an Anthropic API key (see the
# key file below); mdcat for the rendered look.
#
#   ask "..."            one-shot, gpt-oss-120b (default), very terse, effort low
#   ask -h "..."         drop to Haiku 4.5       (--haiku, fastest/cheapest)
#   ask -s "..."         switch to Sonnet 5      (--sonnet, balanced speed + intelligence)
#   ask -o "..."         escalate to Opus 4.8    (--opus)
#   ask -f "..."         escalate to Fable 5     (--fable, most capable)
#   ask -g "..."         gpt-oss-120b (--gpt-oss; same as the default, explicit —
#                        via OpenRouter, pinned to the fast Groq/Cerebras providers)
#   ask -x "..."         switch to Grok 4.3      (--grok; via OpenRouter → xAI)
#   ask -d "..."         switch to DeepSeek V4 Flash (--deepseek; via OpenRouter,
#                        the cheapest of the lot)
#   ask -e <level> "..." effort: low (default) | medium | high (--effort) —
#                        the thinking-depth/token-spend dial (Anthropic
#                        output_config.effort; OpenRouter reasoning.effort);
#                        not valid with -h (Haiku rejects the parameter)
#   ask -v "..."         fuller-but-tight answer (--verbose); composes with any model flag
#   ask --stats "..."    dim [stats] lines on stderr: ttft + token counts from
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
#
# Default terseness is model-dependent: Haiku, Sonnet, gpt-oss, and DeepSeek
# get an extra-terse prompt (they run long otherwise); Opus, Fable, and Grok
# keep the standard terse one. -v overrides both.
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
# Model ids are real API ids (e.g. claude-sonnet-5, openai/gpt-oss-120b,
# x-ai/grok-4.3, deepseek/deepseek-v4-flash); ask and chat POST directly via
# bin/chat-repl.py — no llm CLI, no SDKs. The backend rides the model id: a
# slash means OpenRouter (OpenAI Chat Completions format, keyed by
# $OPENROUTER_API_KEY — export it in ~/.zshrc.local); Anthropic ids have no
# slash and go to the Messages API.

# Shared system prompts — single source of truth; the one-shot path (`ask`) and
# the REPL (`chat`) reuse these verbatim, so there is no copy-paste drift. `chat`
# passes the chosen one into its Python backend via --system, so the text is
# never duplicated outside this file.
# Three tiers:
#   VERY_TERSE — default for Haiku, Sonnet, gpt-oss, and DeepSeek, which run to
#                verbosity otherwise: the answer and nothing else, one line when
#                possible.
#   TERSE      — default for Opus, Fable, and Grok (already well-calibrated):
#                tight but lightly structured Markdown.
#   VERBOSE    — -v on any model: fuller but still no filler.
_ASK_SYS_VERY_TERSE='You are a terminal assistant. Be extremely terse: give only the answer itself, in as few words as it allows — a single line whenever possible. No preamble, no sign-off, no restating the question, no caveats, and no explanation or surrounding context unless explicitly asked. Use Markdown only where it genuinely helps: fenced code blocks with a language tag for commands and code, a short bullet list for several parallel items. Everything else is plain text.'
_ASK_SYS_TERSE='You are a terminal assistant. Keep answers tight — no preamble, no sign-off, no restating the question, no caveats unless essential. Always format the answer as Markdown, and use light structure where it helps: bullet or numbered lists for multiple items, **bold** for key terms, and fenced code blocks with a language tag for commands and code. Only a genuinely trivial reply — a single word or a short one-liner — should be plain text.'
_ASK_SYS_VERBOSE='You are a terminal assistant. Answer very concisely, but completely. No preamble or filler. Always format the answer as Markdown; wrap code in fenced blocks with a language tag.'

# _ask_default_sys: pick the default system prompt for a model — the very-terse
# tier for Haiku/Sonnet/gpt-oss/DeepSeek, the standard terse tier for everything
# else (Opus, Fable, and Grok are already well-calibrated). Used by ask and chat
# when -v wasn't given; the choice must happen AFTER flag parsing, since the
# model isn't known until then.
_ask_default_sys() {
  case "$1" in
    claude-haiku-*|claude-sonnet-*|openai/gpt-oss-*|deepseek/*)
      print -r -- "$_ASK_SYS_VERY_TERSE" ;;
    *)
      print -r -- "$_ASK_SYS_TERSE" ;;
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
# the key, then POSTs a single request through the Python backend's --oneshot
# mode, printing the raw answer. This is what tools call directly,
# e.g.  ASK_TOOL=anki-ask _ask_oneshot claude-sonnet-5 "$sys" "the prompt"
# NOTE: <model> must be a real API id (dashes), NOT an llm alias. The backend
# is inferred from it — a slash (openai/*, x-ai/*, deepseek/*) → OpenRouter,
# anything else → Anthropic — so the tool-facing signature never changes and
# existing callers stay Anthropic.
# Effort rides the ASK_EFFORT env var (low|medium|high; default low) rather than
# a new positional arg, so existing tool callers keep working unchanged. Haiku
# doesn't support the effort parameter (the API rejects it with a 400), so it is
# omitted for claude-haiku-* models regardless of ASK_EFFORT. ASK_STATS=1 (set
# by ask --stats) turns on the backend's per-call [stats] line — stderr-only.
# ASK_FILE (set by ask --file) forwards a path to the backend's --file, which
# does all the reading/validation — only the path crosses this boundary, never
# file contents, so a huge PDF never risks ARG_MAX.
# Usage: [ASK_EFFORT=<level>] [ASK_STATS=1] [ASK_FILE=<path>] _ask_oneshot <model> <system-prompt> <prompt...>
_ask_oneshot() {
  local model="$1" sys="$2"; shift 2
  local -a effort_args stats_args file_args
  [[ "$model" == claude-haiku-* ]] || effort_args=(--effort "${ASK_EFFORT:-low}")
  [[ -n "$ASK_STATS" ]] && stats_args=(--stats)
  [[ -n "$ASK_FILE" ]] && file_args=(--file "$ASK_FILE")
  # Two parallel invocations rather than one parameterized one: each backend
  # has its own key source, and the key must travel via the environment only
  # (never argv, which `ps` can see). `--` stops the backend's option parsing,
  # so a prompt starting with `-` is safe.
  if [[ "$model" == */* ]]; then
    [[ -n "$OPENROUTER_API_KEY" ]] || {
      print -u2 "ask: no OpenRouter key — export OPENROUTER_API_KEY (in ~/.zshrc.local)."
      return 1
    }
    # Self-assignment exports the key to the child even if ~/.zshrc.local
    # forgot the `export`.
    OPENROUTER_API_KEY="$OPENROUTER_API_KEY" python3 "$_CHAT_BACKEND" \
      --oneshot --backend openrouter --model "$model" --system "$sys" \
      "${effort_args[@]}" "${stats_args[@]}" "${file_args[@]}" -- "$*"
  else
    local key; key="$(_anthropic_key)"
    [[ -n "$key" ]] || {
      print -u2 "ask: no Anthropic key — set ANTHROPIC_API_KEY or write it to $_ANTHROPIC_KEY_FILE."
      return 1
    }
    ANTHROPIC_API_KEY="$key" python3 "$_CHAT_BACKEND" \
      --oneshot --backend anthropic --model "$model" --system "$sys" \
      "${effort_args[@]}" "${stats_args[@]}" "${file_args[@]}" -- "$*"
  fi
}

ask() {
  local sys=""                      # resolved after flag parsing (model-dependent)
  local model="openai/gpt-oss-120b" # default: fast + cheap, via OpenRouter (Groq/Cerebras)
  local effort="low" effort_explicit=""   # low = terse/fast/cheap; -e raises it
  local stats=""                    # --stats → per-call [stats] line on stderr
  local file=""                     # --file <path> → forwarded as ASK_FILE
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
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      -h|--haiku)   model="claude-haiku-4-5" ;;  # fastest/cheapest
      -s|--sonnet)  model="claude-sonnet-5" ;;   # balanced speed + intelligence
      -o|--opus)    model="claude-opus-4-8" ;;
      -f|--fable)   model="claude-fable-5" ;;   # escalate to the most capable
      -g|--gpt-oss) model="openai/gpt-oss-120b" ;;  # default; explicit alias — OpenRouter → Groq/Cerebras
      -x|--grok)    model="x-ai/grok-4.3" ;;        # OpenRouter → xAI
      -d|--deepseek) model="deepseek/deepseek-v4-flash" ;;  # OpenRouter, cheapest
      --stats)      stats=1 ;;
      --file)
        shift
        [[ -n "$1" ]] || { print -u2 "ask: --file needs a path."; return 1 }
        file="$1" ;;
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
    print -u2 "ask: -e can't be used with -h — Haiku 4.5 doesn't support the API's effort parameter (the request would be rejected). Drop -e, or pick -s, -o, -f, -g (default), -x, or -d."
    return 1
  fi

  # Interactive shell → render the answer with the chat look (badge + gutter bar +
  # headings) via the shared backend's --render mode. Everything else — a tool
  # (ASK_TOOL set) or piped/redirected output (stdout not a TTY) — falls through to
  # the raw one-shot path untouched, so tool callers and pipelines are unaffected.
  # (Piping into --render naturally buffers the full answer first.)
  if [[ -t 1 && -z "$ASK_TOOL" && -f "$_CHAT_BACKEND" ]] && command -v python3 >/dev/null 2>&1; then
    # --stats also rides the render stage: it prints the total= roundtrip line
    # (enter → answer on screen), which only the downstream process can time.
    ASK_EFFORT="$effort" ASK_STATS="$stats" ASK_FILE="$file" _ask_oneshot "$model" "$sys" "$*" \
      | python3 "$_CHAT_BACKEND" --render --model "$model" ${stats:+--stats}
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
    ASK_EFFORT="$effort" ASK_STATS="$stats" ASK_FILE="$file" _ask_oneshot "$model" "$sys" "$*"   # raw — tools/pipes land here
  fi
}

# chat: interactive, multi-turn LLM REPL with Markdown-RENDERED replies — the
# readable counterpart to ask's raw one-shot output.
# Requires: python3 + mdcat. Reuses ask's shared prompts and keys (OpenRouter,
# used by the default and -g/-x/-d; or Anthropic for -s/-h/-o/-f).
#
#   chat            multi-turn, gpt-oss-120b (default), terse, effort low
#   chat -h         drop to Haiku 4.5       (--haiku, fastest/cheapest)
#   chat -s         switch to Sonnet 5      (--sonnet, balanced speed + intelligence)
#   chat -o         escalate to Opus 4.8    (--opus)
#   chat -f         escalate to Fable 5     (--fable, most capable)
#   chat -g         gpt-oss-120b (--gpt-oss; same as the default, explicit —
#                   via OpenRouter, pinned to the fast Groq/Cerebras providers)
#   chat -x         switch to Grok 4.3      (--grok; via OpenRouter → xAI)
#   chat -d         switch to DeepSeek V4 Flash (--deepseek; via OpenRouter,
#                   the cheapest of the lot)
#   chat -e <level> effort: low (default) | medium | high (--effort); not valid
#                   with -h (Haiku rejects the parameter)
#   chat -v         fuller-but-tight answers (--verbose); composes with any model flag
#   chat --stats    one dim [stats] line under every reply: ttft (API wall time),
#                   total (message sent → reply on screen; pager dwell excluded),
#                   and token counts. stderr-only.
#   chat --file f   attach a file's contents as context to the FIRST message
#                   typed in the session only — not re-sent on later turns (it
#                   lives on in history from there, like any other turn). Same
#                   extraction rules as ask --file (text/Markdown/code as UTF-8,
#                   PDF via local pdftotext). A bad path fails at startup, before
#                   the REPL opens; the banner shows the attached filename.
#
# Default terseness matches ask: extra-terse for Haiku/Sonnet/gpt-oss/DeepSeek,
# standard terse for Opus/Fable/Grok; -v overrides both.
#
# The startup banner states the model and effort in play for the session.
#   chat --no-compact           keep the whole history; disable auto-compaction
#   chat --compact-threshold N  auto-compact once effective input ≥ N tokens
#                               (default 8000; 35000 for -g, where the motive is
#                               interactive latency rather than cost)
#
# Model + verbosity are fixed at launch (mirroring ask's flags). In the REPL:
# `/reset` wipes the context; `/edit` composes in $EDITOR; `/compact` summarizes
# older turns now; `/usage` prints the last turn's token counts; `exit`/`quit`/
# Ctrl-D leave. Nothing is written to disk, so relaunching is a clean reset.
#
# The backend holds history in memory and re-sends it each turn, but marks the
# re-sent prefix cacheable (cache-read billing, ~10% of input price) and
# auto-compacts older turns into a summary once the context crosses the
# threshold — so long chats stay cheap. Fable still costs more per token than
# the default (see the heads-up). The cache_control marking is Anthropic-only:
# the -g path sends none (Groq/Cerebras prefix-cache automatically server-side)
# and summarizes with gpt-oss itself, so a -g session needs only the one key.
#
# ISOLATION — like ask, chat must never be startable by a tool, so it is
# interactive-ONLY: it refuses unless stdin+stdout are a TTY and ASK_TOOL is
# unset. The backend (bin/chat-repl.py) is a separate process holding its own
# in-memory history; it shares nothing with the one-shot path. The API key
# (Anthropic or OpenRouter) is passed via the environment (never argv, which
# `ps` can see; never to disk).
chat() {
  # Hard interactive guard — a tool (no TTY and/or ASK_TOOL set) can never reach
  # the REPL; it should be using one-shot `ask` instead.
  [[ -t 0 && -t 1 && -z "$ASK_TOOL" ]] || {
    print -u2 "chat: interactive use only — tools should call \`ask\` (one-shot)."
    return 1
  }

  local sys=""                    # resolved after flag parsing (model-dependent)
  # Real API ids: the backend calls the Messages API (or OpenRouter) directly.
  local model="openai/gpt-oss-120b" # default: fast + cheap, via OpenRouter (Groq/Cerebras)
  local effort="low" effort_explicit=""   # low = terse/fast/cheap; -e raises it
  local stats=""                  # --stats → per-turn [stats] line on stderr
  local file=""                   # --file <path> → forwarded as --file to the backend
  local -a compact_args           # compaction flags forwarded to the backend

  while [[ "$1" == -* ]]; do
    case "$1" in
      -v|--verbose) sys="$_ASK_SYS_VERBOSE" ;;
      -h|--haiku)   model="claude-haiku-4-5" ;;  # fastest/cheapest
      -s|--sonnet)  model="claude-sonnet-5" ;;   # balanced speed + intelligence
      -o|--opus)    model="claude-opus-4-8" ;;
      -f|--fable)   model="claude-fable-5" ;;    # escalate to the most capable
      -g|--gpt-oss) model="openai/gpt-oss-120b" ;;  # default; explicit alias — OpenRouter → Groq/Cerebras
      -x|--grok)    model="x-ai/grok-4.3" ;;        # OpenRouter → xAI
      -d|--deepseek) model="deepseek/deepseek-v4-flash" ;;  # OpenRouter, cheapest
      --stats)      stats=1 ;;
      --file)
        shift
        [[ -n "$1" ]] || { print -u2 "chat: --file needs a path."; return 1 }
        file="$1" ;;
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

  # Backend rides the model id, same rule as _ask_oneshot: a slash → OpenRouter.
  local backend="anthropic"
  [[ "$model" == */* ]] && backend="openrouter"

  # No -v → model-dependent default: very terse for Haiku/Sonnet/gpt-oss/
  # DeepSeek, terse for the rest.
  [[ -n "$sys" ]] || sys="$(_ask_default_sys "$model")"

  # Same rule as ask: Haiku has no effort parameter, so an explicit -e is an
  # error (the API would reject it), and the low default is simply not sent.
  if [[ "$model" == claude-haiku-* && -n "$effort_explicit" ]]; then
    print -u2 "chat: -e can't be used with -h — Haiku 4.5 doesn't support the API's effort parameter (the request would be rejected). Drop -e, or pick -s, -o, -f, -g (default), -x, or -d."
    return 1
  fi
  local -a effort_args stats_args file_args
  [[ "$model" == claude-haiku-* ]] || effort_args=(--effort "$effort")
  [[ -n "$stats" ]] && stats_args=(--stats)
  [[ -n "$file" ]] && file_args=(--file "$file")

  # Cost heads-up only for models pricier than the default (Haiku is cheaper).
  local pricier=""
  case "$model" in
    claude-sonnet-5) pricier="Sonnet" ;;
    claude-fable-5)  pricier="Fable" ;;
    claude-opus-4-8) pricier="Opus" ;;
  esac
  [[ -n "$pricier" ]] && \
    print -u2 "chat: heads-up — $pricier costs more per token than the default (gpt-oss-120b); prompt caching + auto-compaction soften long-chat cost, but gpt-oss-120b is still cheaper."

  # Resolve the key per backend and hand it to the Python process via the
  # environment only — never argv (which `ps` can see) or disk.
  local key
  if [[ "$backend" == openrouter ]]; then
    [[ -n "$OPENROUTER_API_KEY" ]] || {
      print -u2 "chat: no OpenRouter key — export OPENROUTER_API_KEY (in ~/.zshrc.local)."
      return 1
    }
  else
    key="$(_anthropic_key)"
    [[ -n "$key" ]] || {
      print -u2 "chat: no Anthropic key — set ANTHROPIC_API_KEY or write it to $_ANTHROPIC_KEY_FILE."
      return 1
    }
  fi

  [[ -f "$_CHAT_BACKEND" ]] || {
    print -u2 "chat: backend not found at $_CHAT_BACKEND"
    return 1
  }

  if [[ "$backend" == openrouter ]]; then
    # Self-assignment exports the key to the child even if ~/.zshrc.local
    # forgot the `export`.
    OPENROUTER_API_KEY="$OPENROUTER_API_KEY" python3 "$_CHAT_BACKEND" \
      --backend openrouter --model "$model" --system "$sys" \
      "${effort_args[@]}" "${compact_args[@]}" "${stats_args[@]}" "${file_args[@]}"
  else
    ANTHROPIC_API_KEY="$key" python3 "$_CHAT_BACKEND" \
      --backend anthropic --model "$model" --system "$sys" \
      "${effort_args[@]}" "${compact_args[@]}" "${stats_args[@]}" "${file_args[@]}"
  fi
}

# memory-updates: auto-generate a short, design-level "memory update" doc into
# ~/Claude/memory-updates/ after each push, for later hand-off to Claude.
# Requires: python3 (the summarizer, bin/memory-update-summarize); `ask` above,
# invoked by the summarizer via `zsh -c 'source functions.zsh && ask ...'`
# (ask is a shell function, not a PATH binary, so it can't be exec'd directly).
#
#   memory-updates-init           opt the current repo in (marker + .gitignore)
#   git push                      transparent; backgrounds a summary on success
#   gh pr create                  same, when it pushes an unpushed branch
#   memory-update-summarize --retry <failed-context-file>   regenerate by hand
#
# A repo participates iff `.memory-updates` exists at its root (git rev-parse
# --show-toplevel); `memory-updates-init` creates it and idempotently ignores
# it. Git has no post-push hook, so push interception is a `git()`/`gh()`
# shell wrapper instead — shell functions only catch commands typed at the
# prompt, which is why `gh` (which can push internally) needs its own wrapper
# rather than relying on the `git()` one.
#
# Transparency is the whole point: push/gh's exit code, stdout, stderr, and any
# interactive prompts (gh prompts before pushing in `pr create`) are completely
# unaffected — the summarizer runs backgrounded, disowned, and silenced
# (`&> /dev/null &!`), so a slow or failing summary never delays or pollutes a
# terminal push. Failures are logged + notified instead (see the summarizer's
# own header), never surfaced here.
#
# v1 limits: only a literal `git push` as $1 is intercepted (e.g. `git -C
# somewhere push` bypasses the wrapper, since $1 would be `-C`); `@{push}`
# tracks whichever branch is currently checked out.
_MEMORY_UPDATE_BACKEND="${${(%):-%x}:A:h:h}/bin/memory-update-summarize"

memory-update-summarize() { python3 "$_MEMORY_UPDATE_BACKEND" "$@" }

memory-updates-init() {
  local toplevel
  toplevel=$(command git rev-parse --show-toplevel 2>/dev/null)
  [[ -n "$toplevel" ]] || {
    print -u2 "memory-updates-init: not inside a git repo."
    return 1
  }
  touch "$toplevel/.memory-updates"

  local gi="$toplevel/.gitignore"
  if [[ ! -f "$gi" ]]; then
    print -- '.memory-updates' > "$gi"
  elif ! grep -qxF '.memory-updates' "$gi"; then
    # Fix a missing trailing newline first, so the append doesn't get glued
    # onto the last existing line. Command substitution strips trailing
    # newlines, so a non-empty result means the last byte isn't a newline.
    [[ -s "$gi" && -n "$(tail -c1 "$gi")" ]] && print >> "$gi"
    print -- '.memory-updates' >> "$gi"
  fi
  echo "memory-updates enabled for $toplevel"
}

git() {
  if [[ "$1" == "push" ]]; then
    local toplevel
    toplevel=$(command git rev-parse --show-toplevel 2>/dev/null)
    if [[ -n "$toplevel" && -f "$toplevel/.memory-updates" ]]; then
      # capture the pre-push remote position; may fail (no upstream yet)
      local old_head
      old_head=$(command git rev-parse '@{push}' 2>/dev/null)
      command git "$@"
      local rc=$?
      if (( rc == 0 )); then
        memory-update-summarize "$toplevel" "$old_head" &> /dev/null &!
      fi
      return $rc
    fi
  fi
  command git "$@"
}

gh() {
  local toplevel
  toplevel=$(command git rev-parse --show-toplevel 2>/dev/null)
  if [[ -n "$toplevel" && -f "$toplevel/.memory-updates" ]]; then
    local old_head
    old_head=$(command git rev-parse '@{push}' 2>/dev/null)
    command gh "$@"
    local rc=$?
    if (( rc == 0 )); then
      local new_head
      new_head=$(command git rev-parse '@{push}' 2>/dev/null)
      if [[ -n "$new_head" && "$new_head" != "$old_head" ]]; then
        memory-update-summarize "$toplevel" "$old_head" &> /dev/null &!
      fi
    fi
    return $rc
  fi
  command gh "$@"
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
