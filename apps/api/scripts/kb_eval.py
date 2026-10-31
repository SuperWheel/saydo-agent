"""知识问答评测（WS9，F-GN-14）：命中率（Hit@3）、超范围拒答率、个人数据走工具。

用法：
  uv run python scripts/kb_eval.py          只测检索（不耗模型 API）；.env 配了 EMBEDDING_PROVIDER 时测 BM25 + 向量混合检索
  uv run python scripts/kb_eval.py --bm25   只测检索，强制纯 BM25（和混合模式对比用）
  uv run python scripts/kb_eval.py --llm    用 .env 里的真实模型走完整对话：该查知识库的调用了 kb.search、
                                            知识卡里有应命中的条目；超范围的不出知识卡；个人数据调对应工具、不查知识库
用例在 evals/kb.yaml。命中率或拒答率低于 90%、或个人数据问题走了知识库时退出码为 1。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES = ROOT / "evals" / "kb.yaml"
TARGET = 0.9
MODES: dict[str, int] = {}  # 离线评测里每题实际用的检索方式：hybrid / bm25 / 回退原因


def load_cases() -> list[dict]:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    for c in cases:
        e = c["expect"]
        c["kind"] = "oos" if e == "none" else "route" if str(e).startswith("tool:") else "hit"
        c["want"] = [str(x) for x in (e if isinstance(e, list) else [e])]
    return cases


def offline_case(case: dict) -> tuple[bool, str]:
    """检索层：(是否通过, 说明)。"""
    from app.knowledge import search
    from app.nlu import rule_intent

    found = search(case["ask"])
    ids = [e.id for e in found.entries]
    mode = found.fallback and f"回退（{found.fallback}）" or found.mode
    MODES[mode] = MODES.get(mode, 0) + 1
    sim = f"，相似度 {found.similarity:.2f}" if found.similarity is not None else ""
    note = f"命中 {ids or '无'}（覆盖率 {found.coverage:.2f}{sim}{'，向量没用上：' + found.fallback if found.fallback else ''}）"
    if case["kind"] == "hit":
        return any(w in ids for w in case["want"]), note
    if case["kind"] == "oos":
        return not ids, note
    intent = rule_intent(case["ask"]).intent
    return intent != "faq" and not ids, f"规则意图 {intent}，知识库命中 {ids or '无'}"


def llm_case(client, case: dict) -> tuple[bool, str]:
    client.post("/dev/seed")
    # 进入前人脸核身（模拟）：重置演示数据后旧的进入凭证失效，要重新拿
    client.headers["X-Entry-Token"] = client.post("/api/auth/entry-face", json={"value": "face-ok:entry"}).json()["token"]
    r = client.post("/api/chat", json={"text": case["ask"]}).json()
    events = client.get(f"/admin/audit?trace_id={r['trace_id']}").json()
    tools = [e["payload"]["tool"] for e in events if e["event"] == "tool_call"]
    kb = next((c for c in r["cards"] if c["type"] == "knowledge"), None)
    ids = [i["id"] for i in kb["items"]] if kb else []
    grounding = [e["payload"]["problem"] for e in events if e["event"] == "grounding_failed"]
    note = f"工具 {tools or '无'}  条目 {ids or '无'}  （{r['reply']['source']}）回复：{r['reply']['text'][:80]}"
    if grounding:
        note += f"\n     ⚠️ 事实校验：{'；'.join(grounding)}"
    if case["kind"] == "hit":
        alt = case.get("or_card") and any(c["type"] == case["or_card"] for c in r["cards"])
        return bool(alt) or ("kb.search" in tools and any(w in ids for w in case["want"])), note
    if case["kind"] == "oos":
        return kb is None, note
    # 确定性工作流（回复来源 rule）不调模型、不产生 tool_call 事件：没出知识卡就说明没走知识库
    routed = case["want"][0].removeprefix("tool:") in tools or r["reply"]["source"] == "rule"
    return routed and kb is None and "kb.search" not in tools, note


def main() -> int:
    use_llm = "--llm" in sys.argv
    if "--bm25" in sys.argv:
        os.environ["EMBEDDING_PROVIDER"] = ""
    if use_llm:
        os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/kb_eval.db"
        # 同一个企业微信机器人只能有一个长连接：评测时不连，免得把正在演示的后端踢下线
        os.environ["WECOM_BOT_ID"] = os.environ["WECOM_BOT_SECRET"] = ""
    from app.config import get_settings

    cases = load_cases()
    client = None
    if use_llm:
        from fastapi.testclient import TestClient

        from app.main import create_app

        s = get_settings()
        if not s.llm_ready:
            print(f"没有可用的模型配置：当前供应商是 {s.llm_provider}，请在仓库根目录 .env 里填写对应密钥")
            return 1
        print(f"模型：{s.llm_provider} / {s.llm_model}，{len(cases)} 条用例\n")
        client = TestClient(create_app()).__enter__()
    else:
        from app.knowledge import warm_embeddings

        s = get_settings()
        if not s.embedding_ready:
            mode = "纯 BM25"
        elif warm_embeddings(block=True):
            mode = f"BM25 + 向量混合（{s.embedding_provider} / {s.embedding_model}）"
        else:
            mode = f"配了向量通道（{s.embedding_provider} / {s.embedding_model}）但条目向量没算出来，实际是纯 BM25"
        print(f"只测检索，{mode}，{len(cases)} 条用例\n")

    rows, t0 = [], time.perf_counter()
    for case in cases:
        ok, note = llm_case(client, case) if use_llm else offline_case(case)
        rows.append((case["kind"], ok))
        print(f"{'✅' if ok else '❌'} {case['id']:<15} {case['ask']}\n     {note}")
    if client is not None:
        client.__exit__(None, None, None)

    def rate(kind):
        got = [ok for k, ok in rows if k == kind]
        return (sum(got) / len(got) if got else 1.0), sum(got), len(got)

    hit, oos, route = rate("hit"), rate("oos"), rate("route")
    print(f"\n命中率 {hit[0]:.0%}（{hit[1]}/{hit[2]}，目标 ≥ {TARGET:.0%}）  "
          f"超范围拒答率 {oos[0]:.0%}（{oos[1]}/{oos[2]}，目标 ≥ {TARGET:.0%}）  "
          f"个人数据走工具 {route[1]}/{route[2]}  用时 {time.perf_counter() - t0:.1f} 秒")
    if MODES.keys() - {"bm25"}:  # 配了向量通道：混合了几题、回退了几题（冷却期内的、没切出词的算 bm25）
        print("检索方式：" + "，".join(f"{k} {v} 题" for k, v in sorted(MODES.items())))
    return 0 if hit[0] >= TARGET and oos[0] >= TARGET and route[0] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
