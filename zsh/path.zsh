# PATH additions
# Personal binaries (pipx, local installs, scripts)
export PATH="$HOME/.local/bin:$PATH"

# Rust toolchain — rustup shims (rustc, cargo, rustup) installed via Homebrew
export PATH="/opt/homebrew/opt/rustup/bin:$PATH"

# Cargo-installed binaries (`cargo install`, e.g. ttyper)
export PATH="$HOME/.cargo/bin:$PATH"
