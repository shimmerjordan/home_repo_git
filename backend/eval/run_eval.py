"""AI 意图解析准确度评测台。

用法 (在装了依赖的环境里, 比如 repo_git-backend 镜像):
  python -m eval.run_eval                      # 打真实 LLM, 顺便录 cassette
  python -m eval.run_eval --replay             # 离线重跑 (CI / 无网)
  python -m eval.run_eval --max-tokens 512     # 复现"截断丢操作"
  python -m eval.run_eval --thinking disabled  # 对比关掉思考的准确率与耗时
  python -m eval.run_eval --only multi,mixed   # 只跑某几类
  python -m eval.run_eval --compare            # 512 vs 4096 两档对比

评的是 parse_intent + plan_operations 的联合结果 —— 也就是用户在
「最新识别结果」里真正会看到的那份方案。默认**不落库**: 每个 case 都用一份
全新的内存 sqlite, 互不干扰。带 `decisions` 字段的 case (cat="apply") 是例外——
它们会在 plan 之后真的跑一次 apply_operations 并 commit, 用来验证"确认之后
库里到底变成什么样"; 落库仍然只发生在该 case 自己的内存 sqlite 里, 不会跨
case 互相污染。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from _fixtures import make_session, seed                    # noqa: E402
from app.config import AppConfig, store                     # noqa: E402
from app.llm import client as llm_client                    # noqa: E402
from app.llm import intent as I                             # noqa: E402
from eval.cases import CASES                                # noqa: E402

CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"


# ---- cassette: 把真实响应录下来, 之后能离线重跑 ----------------------------

def _key(cfg, messages, tools, max_tokens) -> str:
    blob = json.dumps({
        "model": cfg.model, "thinking": cfg.thinking, "effort": cfg.effort,
        "max_tokens": max_tokens or cfg.max_tokens,
        "messages": messages, "tools": bool(tools),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def install_cassette(mode: str):
    """mode: record | replay | off"""
    if mode == "off":
        return
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    original = llm_client.LLMClient.chat

    async def patched(self, messages, *, tools=None, force_json=False,
                      force_tool="", max_tokens=None):
        k = _key(self.cfg, messages, tools, max_tokens)
        path = CASSETTE_DIR / f"{k}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if mode == "replay":
            raise llm_client.LLMError(
                f"cassette 缺失 {k} —— 先不带 --replay 跑一次把它录下来")
        out = await original(self, messages, tools=tools, force_json=force_json,
                            force_tool=force_tool, max_tokens=max_tokens)
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out

    llm_client.LLMClient.chat = patched


# ---- 打分 -----------------------------------------------------------------

def _target_of(op) -> tuple[str, str]:
    """把方案里一条 op 的落点归一成 (kind, 描述), kind ∈ item/new/skip。"""
    sel = op.get("selected") or ""
    if not op.get("pending"):          # find 这类只读行
        return ("item", op.get("item_name") or "")
    if sel == "new":
        return ("new", (op.get("new_name_default") or "").strip())
    if sel == "skip":
        return ("skip", "")
    for o in op.get("options") or []:
        if o["key"] == sel:
            return ("item", o.get("label") or "")
    return ("skip", "")


def _match(exp, op) -> bool:
    if exp["intent"] != op["intent"]:
        # create_item 与 put_in+新建 在用户看来是同一件事, 视作等价。
        pair = {exp["intent"], op["intent"]}
        if pair != {"create_item", "put_in"} or not exp.get("new"):
            return False
    kind, name = _target_of(op)
    if exp.get("skip"):
        if kind != "skip":
            return False
    elif exp.get("new"):
        if kind != "new" or name != exp["new"]:
            return False
    elif exp.get("item"):
        if kind != "item" or name != exp["item"]:
            return False
        if exp.get("loc"):
            path = op.get("location_path") or ""
            # 位置只在同名多处时才校验, 用包含判断避免路径写法差异。
            if exp["loc"] not in path:
                return False
    if "qty" in exp and op.get("quantity") != exp["qty"]:
        return False
    return True


def _find_fixture_item(db, spec: str):
    """"物品名" 或 "物品名@位置名" → Item 对象。同名多处时必须带位置。"""
    from app import models
    name, _, loc_name = spec.partition("@")
    rows = db.query(models.Item).filter(models.Item.name == name).all()
    if loc_name:
        rows = [r for r in rows if r.location and r.location.name == loc_name]
    if len(rows) != 1:
        raise AssertionError(f"case 里的 {spec!r} 在 fixture 里命中 {len(rows)} 个, 必须唯一")
    return rows[0]


def _resolve_decision(db, d: dict) -> dict:
    out = dict(d)
    key = out.get("option_key") or ""
    if key.startswith("i:"):
        out["option_key"] = f"i:{_find_fixture_item(db, key[2:]).id}"
    return out


def _check_after(db, expects: list[dict]) -> bool:
    """apply 之后核对库里的真实状态。"""
    from app import models
    for exp in expects:
        name, _, loc_name = exp["item"].partition("@")
        q = db.query(models.Item).filter(models.Item.name == name)
        rows = [r for r in q.all()
                if not loc_name or (r.location and r.location.name == loc_name)]
        if "loc" in exp:
            rows = [r for r in rows if r.location and r.location.name == exp["loc"]]
        if exp.get("exists") is False:
            if rows:
                return False
            continue
        if len(rows) != 1:
            return False
        if "qty" in exp and rows[0].quantity != exp["qty"]:
            return False
    return True


def score_case(case, result, after_ok=None) -> dict:
    ops = result.get("operations") or []
    exp_ops = case["ops"]
    if case.get("expect_readonly"):
        # 只读 case: 只要没有任何待确认的写操作就算对。
        ok = not any(o.get("pending") for o in ops)
        return dict(count_ok=True, target_ok=ok, exact=ok, matched=len(exp_ops),
                    got=len(ops), after_ok=after_ok)
    count_ok = len(ops) == len(exp_ops)
    pool = list(ops)
    matched = 0
    misses = []
    for exp in exp_ops:
        hit = next((o for o in pool if _match(exp, o)), None)
        if hit is not None:
            pool.remove(hit)
            matched += 1
        else:
            misses.append(exp)
    target_ok = matched == len(exp_ops)
    return dict(count_ok=count_ok, target_ok=target_ok,
                exact=count_ok and target_ok, matched=matched,
                got=len(ops), misses=misses,
                extra=[dict(intent=o["intent"], target=_target_of(o),
                            qty=o.get("quantity")) for o in pool],
                after_ok=after_ok)


# ---- 跑一轮 ---------------------------------------------------------------

async def run_once(cfg: AppConfig, cases, verbose=False) -> dict:
    rows = []
    for case in cases:
        db = make_session()
        seed(db)
        t0 = time.time()
        err = None
        after_ok = None
        try:
            out = await I.parse_intent(case["text"], db, cfg)
            result = I.execute_intent(db, case["text"], out["parsed"], cfg, plan_only=True)
            if case.get("decisions"):
                # option_key 里的 "i:物品名@位置名" 要翻译成真实 id —— fixture 每个 case
                # 重新建库, id 不稳定, 所以 case 里只能写名字。
                decisions = [_resolve_decision(db, d) for d in case["decisions"]]
                base = dict(result)
                result = I.apply_operations(db, case["text"], decisions, base)
                db.commit()
            if case.get("after"):
                after_ok = _check_after(db, case["after"])
        except Exception as exc:                        # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            result = {"operations": []}
        ms = (time.time() - t0) * 1000
        s = score_case(case, result, after_ok=after_ok)
        s.update(id=case["id"], cat=case["cat"], text=case["text"], ms=ms, err=err,
                 stage=result.get("stage"))
        rows.append(s)
        if verbose:
            flag = "OK " if s["exact"] and not err and s.get("after_ok") is not False else "BAD"
            print(f"  [{flag}] {case['id']:22s} {ms:6.0f}ms  "
                  f"期望{len(case['ops'])}条/实得{s['got']}条"
                  + (f"  ← {err}" if err else ""))
        db.close()
    return {"rows": rows}


def _after_pct(rs: list[dict]) -> str:
    """有 after 字段的 case 中 after_ok 为真的比例; 一条 after 都没有就显示 '-'。"""
    withafter = [r for r in rs if r.get("after_ok") is not None]
    if not withafter:
        s = "-"
    else:
        pct = sum(1 for r in withafter if r["after_ok"]) / len(withafter) * 100
        s = f"{pct:.0f}%"
    return f"{s:>8s}"


def report(label: str, rows: list[dict]) -> None:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["cat"]].append(r)
    print(f"\n===== {label} =====")
    print(f"{'分类':<12}{'条数':>4}{'全对':>7}{'条数对':>8}{'目标对':>8}{'落库对':>8}{'均耗时':>9}")
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        n = len(rs)
        print(f"{cat:<12}{n:>4}"
              f"{sum(r['exact'] for r in rs) / n * 100:>6.0f}%"
              f"{sum(r['count_ok'] for r in rs) / n * 100:>7.0f}%"
              f"{sum(r['target_ok'] for r in rs) / n * 100:>7.0f}%"
              f"{_after_pct(rs)}"
              f"{sum(r['ms'] for r in rs) / n:>8.0f}ms")
    n = len(rows)
    print(f"{'合计':<12}{n:>4}"
          f"{sum(r['exact'] for r in rows) / n * 100:>6.0f}%"
          f"{sum(r['count_ok'] for r in rows) / n * 100:>7.0f}%"
          f"{sum(r['target_ok'] for r in rows) / n * 100:>7.0f}%"
          f"{_after_pct(rows)}"
          f"{sum(r['ms'] for r in rows) / n:>8.0f}ms")
    bad = [r for r in rows
           if not r["exact"] or r["err"] or r.get("after_ok") is False]
    if bad:
        print(f"\n--- 失败明细 ({len(bad)}/{n}) ---")
        for r in bad:
            print(f"  {r['id']}: {r['text']}")
            if r["err"]:
                print(f"      异常: {r['err']}")
            for m in r.get("misses") or []:
                print(f"      漏: {m}")
            for e in r.get("extra") or []:
                print(f"      多: {e}")


EVAL_CONFIG = Path(__file__).resolve().parent / "eval_config.json"


def build_cfg(args) -> AppConfig:
    """评测用的配置 = eval_config.json 钉死的模型参数 + 真实配置里的 base_url/api_key。

    为什么不直接用 data/config.json: cassette 的 key 含 model/thinking/effort/max_tokens,
    跟着线上配置飘的话, 用户在设置页动一下模型, --replay 就全部 miss。
    反过来 base_url/api_key 不能进版本库, 所以只从运行时配置或环境变量取,
    而 --replay 压根不发请求, 缺了也无所谓。
    """
    cfg = store.get()
    pinned = json.loads(EVAL_CONFIG.read_text(encoding="utf-8")).get("llm", {})
    for field, value in pinned.items():
        if hasattr(cfg.llm, field):
            setattr(cfg.llm, field, value)
    cfg.llm.base_url = os.getenv("EVAL_LLM_BASE_URL") or cfg.llm.base_url
    cfg.llm.api_key = os.getenv("EVAL_LLM_API_KEY") or cfg.llm.api_key
    # CLI 显式给的最后覆盖 (--compare / 调参时用)
    if args.max_tokens:
        cfg.llm.max_tokens = args.max_tokens
    if args.thinking is not None:
        cfg.llm.thinking = args.thinking
    if args.model:
        cfg.llm.model = args.model
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true", help="只用 cassette, 不联网")
    ap.add_argument("--no-record", action="store_true", help="联网但不写 cassette")
    ap.add_argument("--max-tokens", type=int)
    ap.add_argument("--thinking", choices=["", "adaptive", "disabled"])
    ap.add_argument("--model")
    ap.add_argument("--only", help="只跑这些分类, 逗号分隔")
    ap.add_argument("--case", help="只跑这些 case id, 逗号分隔")
    ap.add_argument("--compare", action="store_true",
                    help="512 与 4096 两档 max_tokens 对比")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    cases = CASES
    if args.only:
        want = {c.strip() for c in args.only.split(",")}
        cases = [c for c in cases if c["cat"] in want]
    if args.case:
        want = {c.strip() for c in args.case.split(",")}
        cases = [c for c in cases if c["id"] in want]
    if not cases:
        print("没有匹配的 case")
        return 2

    install_cassette("replay" if args.replay else ("off" if args.no_record else "record"))

    if args.compare:
        for mt in (512, 4096):
            args.max_tokens = mt
            cfg = build_cfg(args)
            out = asyncio.run(run_once(cfg, cases, verbose=not args.quiet))
            report(f"max_tokens={mt}", out["rows"])
        return 0

    cfg = build_cfg(args)
    print(f"模型={cfg.llm.model} max_tokens={cfg.llm.max_tokens} "
          f"thinking={cfg.llm.thinking or '(不传)'} cases={len(cases)}")
    out = asyncio.run(run_once(cfg, cases, verbose=not args.quiet))
    report("总览", out["rows"])
    rows = out["rows"]
    return 0 if all(r["exact"] and not r["err"] and r.get("after_ok") is not False
                    for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
