#!/usr/bin/env bash
# setup/mac.sh — 母艦（herdr を動かす Mac）に必要なものを入れる。何度流してもよい。
#   - Rust のクロスビルド: zig + cargo-zigbuild + linux ターゲット
#   - dots（dot）: タスクボード
#   - jq / gh / gwq / herdr / claude / codex の存在確認
set -euo pipefail
ok()   { printf '\033[32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }

need_brew() { command -v "$1" >/dev/null || brew install "${2:-$1}"; ok "$1: $(command -v "$1")"; }

need_brew jq
need_brew zig
need_brew cargo-zigbuild
for t in x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu; do
  rustup target list --installed | grep -qx "$t" || rustup target add "$t"
done
ok "rust targets: $(rustup target list --installed | tr '\n' ' ')"

# dots: リリースバイナリ（macOS arm64）を ~/.local/bin/dot に置く
if ! command -v dot >/dev/null || ! dot --version 2>/dev/null | grep -q dots; then
  mkdir -p "$HOME/.local/bin"
  gh release download -R joelreymont/dots -p dot-macos-arm64 -O "$HOME/.local/bin/dot" --clobber
  chmod +x "$HOME/.local/bin/dot"
  xattr -d com.apple.quarantine "$HOME/.local/bin/dot" 2>/dev/null || true
fi
ok "dot: $(dot --version)"

for c in gh gwq herdr claude codex go; do
  command -v "$c" >/dev/null && ok "$c" || warn "$c がありません"
done

# 競技サーバ用の SSH 鍵（isu.env の ISU_SSH_KEY）
[ -f "$HOME/.ssh/isucon_ed25519" ] || ssh-keygen -t ed25519 -N "" -C "isucon@$(hostname -s)" -f "$HOME/.ssh/isucon_ed25519" -q
ok "ssh key: ~/.ssh/isucon_ed25519.pub（GitHub に登録しておくと isu-keys で配れる）"
