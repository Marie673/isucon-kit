# shellcheck shell=bash
# bin/isu-* 共通ライブラリ。source して使う。bash 3.2 互換。
set -euo pipefail

ISU_ROOT=$(git rev-parse --show-toplevel)
# worktree をまたいで共有する状態（ssh 設定・known_hosts など）は git の共通ディレクトリに置く
ISU_STATE="$(git rev-parse --path-format=absolute --git-common-dir)/isu"
# メインチェックアウト。results/ と .dots/ はここに集約する（全員が同じ Mac 上にいる前提）
ISU_MAIN=$(git worktree list --porcelain | awk 'NR==1{print $2}')
mkdir -p "$ISU_STATE"

ISU_ENV_FILE=${ISU_ENV_FILE:-$ISU_ROOT/isu.env}
[ -f "$ISU_ENV_FILE" ] || { echo "isu.env がありません: $ISU_ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ISU_ENV_FILE"

ISU_SSH_CONFIG="$ISU_STATE/ssh_config"
ISU_RESULTS="$ISU_MAIN/results"
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
in_list() { local x="$1"; shift; for y in $*; do [ "$x" = "$y" ] && return 0; done; return 1; }

# isu.env から ssh_config を生成（isu.env が新しければ作り直す）
isu_gen_ssh_config() {
  if [ -f "$ISU_SSH_CONFIG" ] && [ "$ISU_SSH_CONFIG" -nt "$ISU_ENV_FILE" ]; then return 0; fi
  local h ip
  {
    echo "# generated from $ISU_ENV_FILE — 手で編集しない"
    for h in $ISU_HOSTS; do
      ip=$(isu_ip "$h")
      [ -n "$ip" ] || continue
      cat <<EOF
Host $h
  HostName $ip
  User $ISU_SSH_USER
  IdentityFile $ISU_SSH_KEY
EOF
    done
    cat <<EOF
Host *
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  UserKnownHostsFile $ISU_STATE/known_hosts
  ControlMaster auto
  ControlPath ~/.ssh/cm-isu-%C
  ControlPersist 10m
  ServerAliveInterval 15
  ConnectTimeout 10
  LogLevel ERROR
EOF
  } > "$ISU_SSH_CONFIG"
}
isu_gen_ssh_config

isu_ssh() { local h="$1"; shift; [ -n "$(isu_ip "$h")" ] || die "$h の IP が isu.env にありません"; ssh -F "$ISU_SSH_CONFIG" "$h" "$@"; }
# リモートで bash スクリプトを実行（stdin でスクリプトを渡す）。引数は $1.. で参照できる
isu_sh() {
  local h="$1" q="" a; shift
  for a in "$@"; do q="$q $(printf %q "$a")"; done   # ssh は引数を連結するのでリモート用にクォートする
  isu_ssh "$h" "bash -s --$q"
}
isu_scp() { scp -q -F "$ISU_SSH_CONFIG" "$@"; }

# ---------- ロック ----------
ISU_LOCK_DIR=/home/isucon/.isu-lock
isu_lock_owner() { isu_ssh "$ISU_LOCK_HOST" "cat $ISU_LOCK_DIR/owner 2>/dev/null || true"; }
isu_lock_try() { # $1=what
  isu_ssh "$ISU_LOCK_HOST" "mkdir $ISU_LOCK_DIR 2>/dev/null && echo '$ISU_WHO|$(date +%s)|$1' > $ISU_LOCK_DIR/owner"
}
isu_lock_held_by_me() { local o; o=$(isu_lock_owner); [ "${o%%|*}" = "$ISU_WHO" ]; }
isu_lock_release() {
  isu_lock_held_by_me || { warn "ロックを持っていません（owner: $(isu_lock_owner)）"; return 0; }
  isu_ssh "$ISU_LOCK_HOST" "rm -rf $ISU_LOCK_DIR"
}
isu_lock_describe() {
  local o; o=$(isu_lock_owner)
  [ -n "$o" ] || { echo "free"; return; }
  local who="${o%%|*}" rest="${o#*|}"; local t="${rest%%|*}" what="${rest#*|}"
  echo "held by $who for $(( $(date +%s) - t ))s: $what"
}

# 外部待ちを herdr のサイドバーに出す（herdr がなければ何もしない）
isu_wait_mark() { command -v herdr-wait >/dev/null 2>&1 && [ "${HERDR_ENV:-}" = 1 ] && herdr-wait set "$1" >/dev/null 2>&1 || true; }
isu_wait_clear() { command -v herdr-wait >/dev/null 2>&1 && [ "${HERDR_ENV:-}" = 1 ] && herdr-wait clear >/dev/null 2>&1 || true; }

# 共有物（results/ docs/ .dots/）をメインチェックアウトでそのパスだけコミットする
isu_commit_shared() {
  local msg="$1"; shift
  git -C "$ISU_MAIN" add -- "$@" 2>/dev/null || return 0
  git -C "$ISU_MAIN" commit -q -m "$msg" -- "$@" >/dev/null 2>&1 || true
}
