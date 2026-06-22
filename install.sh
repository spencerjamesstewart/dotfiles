#!/usr/bin/env bash
# Idempotent installer: symlinks ~/.zshrc to this repo and seeds ~/.zshrc.local.
set -euo pipefail
DOTFILES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
link() {
  local src="$1" dst="$2"
  if [ -e "$dst" ] && [ ! -L "$dst" ]; then
    mv "$dst" "$dst.backup.$(date +%Y%m%d%H%M%S)"
    echo "Backed up existing $dst"
  fi
  ln -sfn "$src" "$dst"
  echo "Linked $dst -> $src"
}
link "$DOTFILES/zsh/zshrc" "$HOME/.zshrc"
# Secret-scanning pre-commit hook. It lives in .git/hooks/, which isn't tracked
# and isn't cloned, so symlink it from the version-controlled git-hooks/ dir on
# every install — this keeps the guard active on every machine, not just one.
chmod +x "$DOTFILES/git-hooks/pre-commit"
link "$DOTFILES/git-hooks/pre-commit" "$DOTFILES/.git/hooks/pre-commit"
# aichat config (backs `ask --chat`). Lives at the macOS default location; ln
# needs the parent dir to exist first. Holds no secrets — the key is passed at
# launch (see aichat/config.yaml), so it's safe to track and symlink.
AICHAT_DIR="$HOME/Library/Application Support/aichat"
mkdir -p "$AICHAT_DIR"
link "$DOTFILES/aichat/config.yaml" "$AICHAT_DIR/config.yaml"
if [ ! -e "$HOME/.zshrc.local" ]; then
  cp "$DOTFILES/zsh/zshrc.local.example" "$HOME/.zshrc.local"
  echo "Created ~/.zshrc.local — add your real secrets there (it is gitignored)."
fi
echo
echo "Done. Open a new terminal, or run:  source ~/.zshrc"
