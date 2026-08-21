#!/usr/bin/env python3
"""确认 docker-compose.yml (源码编译) 和 deploy/compose.release.yml (发布镜像) 没有跑偏。

为什么需要这个: 两个文件描述的是同一套部署, 只差 build/image 一行。谁改了一边忘了另一边,
用户按 release 页部署出来的东西就跟仓库里的不是一回事 —— 而且这种偏差不会报错,
只会表现成"某个环境变量没生效""端口不对""数据挂错地方"。CI 里跑一遍最省事。

用法: python deploy/check_compose_parity.py   (exit 0 = 一致)
"""
from __future__ import annotations

import sys

import yaml

SRC = "docker-compose.yml"
REL = "deploy/compose.release.yml"

# 必须逐字一致的字段。build/image 是故意不同的, pull_policy 只有发布版需要。
MUST_MATCH = ("environment", "ports", "volumes", "container_name", "restart",
              "expose", "profiles", "healthcheck")
IGNORE = {"build", "image", "pull_policy"}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]


def main() -> int:
    src, rel = load(SRC), load(REL)
    problems: list[str] = []

    if set(src) != set(rel):
        problems.append(f"service 列表不同: {SRC}={sorted(src)} vs {REL}={sorted(rel)}")

    for svc in sorted(set(src) & set(rel)):
        a, b = src[svc], rel[svc]
        for key in MUST_MATCH:
            if a.get(key) != b.get(key):
                problems.append(
                    f"{svc}.{key} 不一致\n"
                    f"    {SRC}: {a.get(key)!r}\n"
                    f"    {REL}: {b.get(key)!r}"
                )
        # 有一边多出的键也要报 —— 不然新加的字段会静静地只存在于一个文件里
        extra = (set(a) | set(b)) - set(MUST_MATCH) - IGNORE
        for key in sorted(extra):
            if a.get(key) != b.get(key):
                problems.append(
                    f"{svc}.{key} 只存在于一边或取值不同 (若是故意的, 加进本脚本的 IGNORE)\n"
                    f"    {SRC}: {a.get(key)!r}\n"
                    f"    {REL}: {b.get(key)!r}"
                )

    # 发布版必须用 image 且带占位符; 源码版必须用 build
    if "build" not in src.get("app", {}):
        problems.append(f"{SRC} 的 app 应该用 build:")
    rel_image = rel.get("app", {}).get("image", "")
    if "__IMAGE__" not in str(rel_image):
        problems.append(
            f"{REL} 的 app.image 应该是 __IMAGE__ 占位符 (发布流水线负责替换), 实际是 {rel_image!r}"
        )

    if problems:
        print("compose 文件不一致:\n")
        for p in problems:
            print("  -", p)
        return 1
    print(f"OK: {SRC} 与 {REL} 一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
