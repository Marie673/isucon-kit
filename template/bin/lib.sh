# shellcheck shell=bash
# bin/isu-* 共通ライブラリ。source して使う。bash 3.2 互換。
set -euo pipefail

ISU_ROOT=$(git rev-parse --show-toplevel)
# worktree をまたいで共有する状態（ssh 設定・known_hosts など）は git の共通ディレクトリに置く
ISU_STATE="$(git rev-parse --path-format=absolute --git-common-dir)/isu"
# メインチェックアウト。results/ はここに集約する（全員が同じ Mac 上にいる前提）
ISU_MAIN=$(git worktree list --porcelain | awk 'NR==1{print $2}')
mkdir -p "$ISU_STATE"

ISU_ENV_FILE=${ISU_ENV_FILE:-$ISU_ROOT/isu.env}
[ -f "$ISU_ENV_FILE" ] || { echo "isu.env がありません: $ISU_ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ISU_ENV_FILE"

ISU_SSH_CONFIG="$ISU_STATE/ssh_config"
export ISU_RESULTS="$ISU_MAIN/results"
# ロックの持ち主。既定はブランチ名（worktree ごとに別になる）
ISU_WHO=${ISU_WHO:-$(git branch --show-current 2>/dev/null || echo detached)}

c_dim=$'\033[2m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_off=$'\033[0m'
[ -t 2 ] || { c_dim=; c_red=; c_grn=; c_ylw=; c_off=; }
log()  { printf '%s[isu]%s %s\n' "$c_dim" "$c_off" "$*" >&2; }
ok()   { printf '%s[ok]%s %s\n' "$c_grn" "$c_off" "$*" >&2; }
warn() { printf '%s[warn]%s %s\n' "$c_ylw" "$c_off" "$*" >&2; }
die()  { printf '%s[err]%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

isu_var() { local n="$1"; printf '%s' "${!n:-}"; }
isu_ip() { isu_var "ISU_IP_$1"; }
isu_services() { isu_var "ISU_SERVICES_$1"; }
isu_hosts() { printf '%s\n' $ISU_HOSTS; }
in_list() { local x="$1"; shift; for y in "$@"; do [ "$x" = "$y" ] && return 0; done; return 1; }

# ssh_config は Python の共通処理だけで生成する。
uv run --no-project python "$ISU_ROOT/bin/isu_runtime.py" ssh-config

isu_deadline() { uv run --no-project python "$ISU_ROOT/bin/isu_runtime.py" time-guard; }
isu_ssh() { isu_deadline; local h="$1"; shift; [ -n "$(isu_ip "$h")" ] || die "$h の IP が isu.env にありません"; ssh -F "$ISU_SSH_CONFIG" "$h" "$@"; }
# リモートで bash スクリプトを実行（stdin でスクリプトを渡す）。引数は $1.. で参照できる
isu_sh() {
  local h="$1" q="" a; shift
  for a in "$@"; do q="$q $(printf %q "$a")"; done   # ssh は引数を連結するのでリモート用にクォートする
  isu_ssh "$h" "bash -s --$q"
}
isu_scp() { isu_deadline; scp -q -F "$ISU_SSH_CONFIG" "$@"; }

# 外部待ちを herdr のサイドバーに出す（herdr がなければ何もしない）
isu_wait_mark() { command -v herdr-wait >/dev/null 2>&1 && [ "${HERDR_ENV:-}" = 1 ] && herdr-wait set "$1" >/dev/null 2>&1 || true; }
isu_wait_clear() { command -v herdr-wait >/dev/null 2>&1 && [ "${HERDR_ENV:-}" = 1 ] && herdr-wait clear >/dev/null 2>&1 || true; }

# 共有物は失敗を記録して再試行できるようにする。
isu_commit_shared() {
  local msg="$1"; shift
  if [ -f "$(git -C "$ISU_MAIN" rev-parse --git-path MERGE_HEAD)" ]; then
    warn "マージ中です。共有物のコミットを後で再試行してください: $*"
    printf '%s\n' "$msg" "$@" > "$ISU_STATE/commit-pending"
    return 0
  fi
  git -C "$ISU_MAIN" add -- "$@" || { warn "共有物を stage できません: $*"; return 0; }
  git -C "$ISU_MAIN" diff --cached --quiet -- "$@" && return 0
  git -C "$ISU_MAIN" commit -q -m "$msg" -- "$@" || {
    warn "共有物をコミットできません。後で再試行してください: $*"
    printf '%s\n' "$msg" "$@" > "$ISU_STATE/commit-pending"
  }
}
isu_canonical() { uv run --no-project python "$ISU_ROOT/bin/isu_runtime.py" canonical-guard; }
isu_health() { uv run --no-project python "$ISU_ROOT/bin/isu_runtime.py" health-guard; }
isu_target() { printf '%s://%s:%s' "${ISU_TARGET_SCHEME:-https}" "$ISU_TARGET_HOST" "${ISU_TARGET_PORT:-443}"; }
