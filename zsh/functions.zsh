# Shell functions

# ask: one-shot question to an LLM, streamed straight to the terminal.
# Requires: llm (with the anthropic plugin).
# Default is VERY terse. Pass -v / --verbose for a fuller but
# still tight answer (a short paragraph or a few bullets).
ask() {
  # Very terse by default.
  local sys="You are a terminal assistant. Answer in the fewest words possible. No preamble, no sign-off, no restating the question, no caveats unless essential. Use markdown only when it genuinely helps; wrap code in fenced blocks with a language tag."

  # Default model is the fast, cheap Haiku.
  local model="claude-haiku-4.5"

  # Parse leading flags; they can be combined in any order.
  while [[ "$1" == -* ]]; do
    case "$1" in
      # -v / --verbose: a bit more depth, still concise.
      -v|--verbose)
        sys="You are a terminal assistant. Answer very concisely, but completely. No preamble or filler. Use markdown; wrap code in fenced blocks with a language tag."
        ;;
      # -s: switch to the more capable Sonnet model.
      -s)
        model="claude-sonnet-4.5"
        ;;
      *)
        break
        ;;
    esac
    shift
  done

  llm -m "$model" -s "$sys" "$*"
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
