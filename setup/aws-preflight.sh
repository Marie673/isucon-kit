#!/usr/bin/env bash
# setup/aws-preflight.sh [profile] — 競技用 AWS アカウントの事前確認（読み取りのみ。何も作らない）
#   当日「EC2 が 3 台立たない」が最悪の事故。新しいアカウントは vCPU 上限が低いことがある。
#   確認: 認証 / リージョン / オンデマンド vCPU 上限 / キーペア / デフォルト VPC / 既存インスタンス
set -uo pipefail
P=${1:-${AWS_PROFILE:-}}
R=${AWS_REGION:-ap-northeast-1}
a() { aws ${P:+--profile "$P"} --region "$R" "$@"; }
ok()   { printf '\033[32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }

id=$(a sts get-caller-identity --query Account --output text 2>/dev/null) || { warn "認証できません（aws configure / aws sso login）"; exit 1; }
ok "account $id / region $R"

# Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances の vCPU 上限
q=$(a service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A --query Quota.Value --output text 2>/dev/null)
if [ -n "$q" ]; then
  # 例: c5.large(2vCPU) x 3 = 6、予備や練習で余裕を見て 16 以上
  if [ "${q%.*}" -ge 16 ]; then ok "on-demand vCPU 上限: $q"; else warn "on-demand vCPU 上限が $q（不足しうる。Service Quotas で引き上げ申請を）"; fi
else
  warn "vCPU 上限を取得できませんでした（権限不足の可能性）"
fi

n=$(a ec2 describe-key-pairs --query 'length(KeyPairs)' --output text 2>/dev/null)
[ "${n:-0}" -gt 0 ] && ok "key pairs: $(a ec2 describe-key-pairs --query 'KeyPairs[].KeyName' --output text)" || warn "キーペアがありません（当日マニュアルの指示に合わせて用意）"

v=$(a ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text 2>/dev/null)
[ -n "$v" ] && [ "$v" != None ] && ok "default VPC: $v" || warn "デフォルト VPC がありません（CloudFormation が前提にしている場合がある）"

r=$(a ec2 describe-instances --filters Name=instance-state-name,Values=running --query 'length(Reservations[].Instances[])' --output text 2>/dev/null)
[ "${r:-0}" = 0 ] && ok "起動中のインスタンスなし" || warn "起動中のインスタンス ${r} 台（既存リソースは運営のサポート対象外。費用にも注意）"

ok "Budgets のアラート設定（任意）: 当日の EC2 費用は自己負担"
