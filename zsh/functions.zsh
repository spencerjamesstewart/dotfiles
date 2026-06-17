# Shell functions

# ask: one-shot question to an LLM, streamed straight to the terminal.
# Requires: llm (with the anthropic plugin).
# Default is VERY terse. Pass -v / --verbose for a fuller but
# still tight answer (a short paragraph or a few bullets).
ask() {
  # Very terse by default.
  local sys="You are a terminal assistant. Answer in the fewest words possible. No preamble, no sign-off, no restating the question, no caveats unless essential. Use markdown only when it genuinely helps; wrap code in fenced blocks with a language tag."

  # -v / --verbose: a bit more depth, still concise.
  if [[ "$1" == "-v" || "$1" == "--verbose" ]]; then
    shift
    sys="You are a terminal assistant. Answer very concisely, but completely. No preamble or filler. Use markdown; wrap code in fenced blocks with a language tag."
  fi

  llm -m claude-haiku-4.5 -s "$sys" "$*"
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
