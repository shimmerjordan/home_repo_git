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


TOOLS_HASH_PATH = CASSETTE_DIR / "_tools.sha256"


def _tools_hash() -> str:
    blob = json.dumps(I.TOOLS, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _check_tools_hash(mode: str) -> None:
    """_key() 只把 `bool(tools)` 哈希进 cassette 文件名 —— 改坏 TOOLS 本身 (删字段/
    塞一个模型没见过的枚举值) 不会让任何 cassette miss, --replay 照样满分退出 0,
    对这类回归零感知。故意不把 tools 塞进 _key: 那样已经录好的 47 个 cassette 会
    全部失效, 又要真调 LLM 重录一遍花钱。改用这个旁挂文件校验同样的东西。"""
    current = _tools_hash()
    if mode == "record":
        # 录制 = 用当前 TOOLS 建立新基线。
        TOOLS_HASH_PATH.write_text(current, encoding="utf-8")
        return
    if mode == "replay":
        if not TOOLS_HASH_PATH.exists():
            # 第一次跑: 现有 cassette 就是基线, 把当前 TOOLS 记下来, 不动 cassette。
            TOOLS_HASH_PATH.write_text(current, encoding="utf-8")
            return
        baseline = TOOLS_HASH_PATH.read_text(encoding="utf-8").strip()
        if baseline != current:
            print("TOOLS 变了 (与 cassettes/_tools.sha256 记录的基线不一致): "
                  "现有 cassette 里录的响应反映的是旧的工具契约, --replay 不能拿"
                  "它们当新 TOOLS 的答案用 —— 需要去掉 --replay 重录一遍。",
                  file=sys.stderr)
            sys.exit(1)


def install_cassette(mode: str):
    """mode: record | replay | off"""
    if mode == "off":
        return
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    _check_tools_hash(mode)
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


def _selected_location_path(op) -> str:
    """选中的那条记录**当前实际所在**的位置 —— 不是目的地。

    find 这类只读行的 location_path 本来就是"物品现在在哪"; 但 put_in/take_out/
    consume/create_item 这些待确认写操作的 location_path 是 _resolve_location
    解析出的**目的地**, 跟"选中了同名多处里的哪一条"是两码事。要知道选中的那条
    记录原来在哪, 得去 candidates (方案汇总的候选列表, 序列化时带了各自的
    location_path) 里按选中的 item_id 找回来。
    """
    if not op.get("pending"):
        return op.get("location_path") or ""
    sel_id = op.get("item_id")
    if sel_id is None:
        return ""
    for c in op.get("candidates") or []:
        if c.get("item_id") == sel_id:
            return c.get("location_path") or ""
    return ""


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
        if exp.get("from_loc"):
            # 校验"选中的是哪一条记录", 不是目的地 —— 同名多处时选错了这里会露出来。
            if exp["from_loc"] not in _selected_location_path(op):
                return False
    if exp.get("to"):
        # put_in/create_item 的目标位置。之前从来没被读过, 位置解析整个失效也测不出来。
        path = op.get("location_path") or ""
        if exp["to"] not in path:
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

def _case_ok(r: dict) -> bool:
    """这一条算不算过 (不看 xfail)。"""
    return bool(r["exact"]) and not r["err"] and r.get("after_ok") is not False


def _flag(r: dict) -> str:
    """OK / BAD / XFAIL (已知不过) / XPASS (标了 xfail 却过了 —— 该把标记去掉)。

    xfail 只豁免准确性, 不豁免 err: 回放时抛异常意味着 cassette 缺失或代码炸了,
    那是真问题, 不能被"已知不过"盖过去。
    """
    ok = _case_ok(r)
    if not r.get("xfail"):
        return "OK " if ok else "BAD"
    if r["err"]:
        return "BAD"
    return "XPASS" if ok else "XFAIL"


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
            plan_result = I.execute_intent(db, case["text"], out["parsed"], cfg, plan_only=True)
            result = plan_result
            if case.get("decisions"):
                # option_key 里的 "i:物品名@位置名" 要翻译成真实 id —— fixture 每个 case
                # 重新建库, id 不稳定, 所以 case 里只能写名字。
                # 打分永远用 plan_result: case["ops"] 描述的是方案阶段该长什么样 (含
                # new= 这种"该新建"的期望), apply 之后的 operations 已经没有
                # pending/selected/options 字段, 拿去打分会把"新建"误判成"匹配已有"。
                # apply 的返回值只用于 after 断言, 不覆盖 result。
                decisions = [_resolve_decision(db, d) for d in case["decisions"]]
                I.apply_operations(db, case["text"], decisions, dict(plan_result))
                db.commit()
            if case.get("after"):
                after_ok = _check_after(db, case["after"])
        except Exception as exc:                        # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            result = {"operations": []}
        ms = (time.time() - t0) * 1000
        s = score_case(case, result, after_ok=after_ok)
        s.update(id=case["id"], cat=case["cat"], text=case["text"], ms=ms, err=err,
                 stage=result.get("stage"), xfail=case.get("xfail"))
        rows.append(s)
        if verbose:
            flag = _flag(s)
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
    xfail_all = [r for r in rows if r.get("xfail")]
    print(f"{'合计':<12}{n:>4}"
          f"{sum(r['exact'] for r in rows) / n * 100:>6.0f}%"
          f"{sum(r['count_ok'] for r in rows) / n * 100:>7.0f}%"
          f"{sum(r['target_ok'] for r in rows) / n * 100:>7.0f}%"
          f"{_after_pct(rows)}"
          f"{sum(r['ms'] for r in rows) / n:>8.0f}ms"
          f"  (其中 xfail {len(xfail_all)} 条)")
    # 失败明细只列真正的 BAD —— xfail 的准确性失败不算, 但它的 err 仍然算 (见 _flag)。
    bad = [r for r in rows if _flag(r) == "BAD"]
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
    # 已知不过 (xfail) 单独打一段, 不混进失败明细, 但也不能悄悄躺着不出现。
    still_xfail = [r for r in xfail_all if _flag(r) == "XFAIL"]
    if still_xfail:
        print(f"\n--- 已知不过 (xfail {len(still_xfail)} 条, 不计入退出码) ---")
        for r in still_xfail:
            print(f"  {r['id']}: {r['text']}")
            print(f"      理由: {r['xfail']}")
    # XPASS: 标了 xfail 却通过了 —— 说明有人把它修好了, 必须显式喊出来,
    # 否则这条 case 会变成一块永久豁免的遮羞布, 将来真回退了也不会响。
    xpassed = [r for r in xfail_all if _flag(r) == "XPASS"]
    if xpassed:
        print(f"\n--- XPASS ({len(xpassed)} 条: 标了 xfail 却通过了, 请把 xfail 去掉) ---")
        for r in xpassed:
            print(f"  {r['id']}")


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


def exit_code_for(rows: list[dict]) -> int:
    """整轮的退出码。抽成函数是为了能被测试直接打到 —— 以前测试里手抄了一份
    规则副本, 改坏 main() 里那行它照样全绿, 而那正是 CI 门禁本身。"""
    return 0 if all(_flag(r) in ("OK ", "XFAIL", "XPASS") for r in rows) else 1


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
    return exit_code_for(out["rows"])


if __name__ == "__main__":
    raise SystemExit(main())
