# Shell functions

# ask: one-shot question to an LLM, rendered as markdown in the terminal.
# Requires: llm (with the anthropic plugin) and glow.
ask() {
  llm -m claude-haiku-4.5 -s "Answer concisely. Use markdown; wrap code in fenced blocks with a language tag." "$*" \
    | glow -s ~/.config/glow/gruvbox.json -w $(( ${COLUMNS:-80} - 4 )) -
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
