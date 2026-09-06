#!/usr/bin/env bash
# CHANGELOG.md 是版本号的**唯一来源**。
#
#   scripts/version.sh          → 打印版本号, 例如 0.10.0
#   scripts/version.sh --check  → 再校验 frontend/package.json 与它一致
#
# 为什么不靠 git tag: tag 是发布的**结果**而不是来源。靠 tag 意味着"先打 tag 再
# 构建", 而 tag 打错了还得删了重来; 更糟的是 tag 可以指向任意 commit, 于是
# "门禁测的那份代码"和"要发布的那份代码"可能不是同一个。CHANGELOG 是写变更时
# 顺手就改的地方, 让它当唯一来源, 版本号与变更说明天然同步。
#
# 标题格式 (标题后面写什么都行, 解析只认那个版本号):
#
#   ## v0.10.0 可信任的 AI            要发的版本
#   ## Unreleased                     还没定版本号的开发中内容 (不参与解析)
#
# 两段式 (v0.8) 也认, 会归一化成 0.8.0 —— 仓库早期用的是那个写法。
set -euo pipefail
cd "$(dirname "$0")/.."

# 取所有 `## ` 标题里**最大**的版本号。
#
# `sort -V` 不能换成 `sort`: 字典序会认为 0.9.0 > 0.10.0, 于是发了 0.10.0 之后
# 再跑一次, 版本号会倒退回 0.9.0 —— 而且一路静默。
#
# 取"最大"而不是"最上面那个": 最上面通常是 `## Unreleased`, 而且历史段落的排列
# 顺序不该决定版本号。
VERSION=$(
  grep -E '^## ' CHANGELOG.md \
    | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' \
    | awk -F. '{ printf "%s.%s.%s\n", $1, $2, ($3 == "" ? 0 : $3) }' \
    | sort -V \
    | tail -1
)

if [ -z "$VERSION" ]; then
  echo "::error::CHANGELOG.md 里没有任何形如 '## v1.2.3 标题' 的版本标题。" >&2
  echo "发版前把最上面那段 '## Unreleased' 改成 '## v<x.y.z> <这一版的主题>'。" >&2
  exit 1
fi

if [ "${1:-}" = --check ]; then
  # frontend/package.json 里存着第二份版本号。目前没有任何代码读它 (前端不显示
  # 版本), 所以这个校验的价值只有一个: 不让它无声地烂在 0.1.0。发版时多改一行,
  # 换"仓库里所有自称版本的地方说的是同一件事"。
  PKG=$(grep -E '"version"' frontend/package.json | head -1 | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
  if [ "$PKG" != "$VERSION" ]; then
    echo "::error file=frontend/package.json::版本漂移: CHANGELOG.md 是 $VERSION, frontend/package.json 是 ${PKG:-<没解析出来>}。" >&2
    exit 1
  fi
  echo "版本一致: $VERSION (frontend/package.json: $PKG)"
  exit 0
fi

echo "$VERSION"
