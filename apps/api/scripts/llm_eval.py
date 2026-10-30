"""真实模型回归集：统计意图准确率（该调的工具调没调）和任务成功率（该出的卡片出没出）。

用法：`make eval-llm`，或 `uv run python scripts/llm_eval.py transfer-`（只跑 id 含 transfer- 的用例）。
用例在 evals/regression.yaml。使用临时数据库，每条用例前重置演示数据。意图准确率低于 90% 时退出码为 1。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES = ROOT / "evals" / "regression.yaml"
TARGET = 0.9
MAX_REPLY = 60  # 提示词要求回复不超过 60 字


def _as_list(v) -> list[str]:
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/eval.db"
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    s = get_settings()
    if not s.llm_ready:
        print(f"没有可用的模型配置：当前供应商是 {s.llm_provider}，请在仓库根目录 .env 里填写对应密钥")
        return 1
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    if len(sys.argv) > 1:
        cases = [c for c in cases if sys.argv[1] in c["id"]]
    print(f"模型：{s.llm_provider} / {s.llm_model}，{len(cases)} 条用例\n")

    rows, total, long_replies, n_workflow = [], 0.0, 0, 0
    with TestClient(create_app()) as c:
        for case in cases:
            c.post("/dev/seed")
            sid, t0 = None, time.perf_counter()
            for text in case["say"]:
                r = c.post("/api/chat", json={"text": str(text), "session_id": sid}).json()
                sid = r["session_id"]
            cost = time.perf_counter() - t0
            total += cost
            events = c.get(f"/admin/audit?trace_id={r['trace_id']}").json()
            tools = [e["payload"]["tool"] for e in events if e["event"] == "tool_call"]
            cards = ", ".join(f"{x['type']}/{x['level']}" if x["type"] == "confirm" else x["type"] for x in r["cards"]) or "无"
            want_tools, want_cards = _as_list(case["tool"]), _as_list(case["card"])
            task_ok = cards in want_cards
            # 工作流（规则）轮次不调模型、不产生 tool_call 事件：卡片对了就说明走对了流程
            workflow = r["reply"]["source"] == "rule"
            intent_ok = any((t == "none" and not tools) or t in tools for t in want_tools) or (workflow and task_ok)
            n_workflow += workflow
            notes = [e["payload"]["problem"] for e in events if e["event"] == "grounding_failed"]
            notes += [f"模型报错 {e['payload']['error'][:80]}" for e in events if e["event"] == "llm_error"]
            rows.append((case["id"], intent_ok, task_ok))
            mark = "✅" if intent_ok and task_ok else "❌"
            print(f"{mark} {case['id']:<12} {cost:4.1f}s  工具 {tools or '无'}  卡片 {cards}  （{r['reply']['source']}）")
            if not intent_ok:
                print(f"     意图不对：应调用 {' / '.join(want_tools)}")
            if not task_ok:
                print(f"     卡片不对：应为 {' / '.join(want_cards)}")
            if notes:
                print(f"     ⚠️ {'；'.join(notes)}")
            if not (intent_ok and task_ok):
                print(f"     回复：{r['reply']['text']}")
            elif r["reply"]["source"] == "llm" and len(r["reply"]["text"]) > MAX_REPLY:  # 模板回复不受提示词字数要求约束
                long_replies += 1
                print(f"     📏 回复 {len(r['reply']['text'])} 字：{r['reply']['text']}")

    n = len(rows)
    intent = sum(r[1] for r in rows) / n
    task = sum(r[2] for r in rows) / n
    print(f"\n意图准确率 {intent:.0%}（{sum(r[1] for r in rows)}/{n}，目标 ≥ {TARGET:.0%}）  "
          f"任务成功率 {task:.0%}（{sum(r[2] for r in rows)}/{n}）  平均每条 {total / n:.1f} 秒  "
          f"模型回复超 {MAX_REPLY} 字 {long_replies} 条  走工作流（不调模型）{n_workflow} 条")
    return 0 if intent >= TARGET else 1


if __name__ == "__main__":
    raise SystemExit(main())
