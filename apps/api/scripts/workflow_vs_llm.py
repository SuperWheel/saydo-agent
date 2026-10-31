"""确定性工作流 vs 全部交给模型：同一批账单问题各问 N 遍，比准确率、一致性和耗时。

用法：`make workflow-vs-llm`，或 `uv run python scripts/workflow_vs_llm.py 3`（每句问几遍，默认 3）。
需要已配置的真实模型。使用临时数据库，演示日期固定 2026-10-30，不连企业微信。
参照答案是工作流第一遍的结果：这些说法规则能完整识别，卡片上的数字由账务引擎按原话算出。
"""

import os
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QUESTIONS = [
    ("近30天的消费记录", "bill_txns"), ("最近3个月花了哪些钱", "bill_txns"), ("近两个月的消费记录", "bill_txns"),
    ("我这一个月的消费记录", "bill_txns"), ("过去两周的消费", "bill_txns"), ("上个月在美团花了多少", "bill_txns"),
    ("这个月给我妈转了多少", "bill_txns"), ("这个月交通花了多少", "bill_txns"), ("我的支出总览", "bill_categories"),
    ("这个月钱都花哪了", "bill_categories"), ("这个月的消费总结", "bill_report"), ("上个月的账单报告", "bill_report"),
]


def _sig(card: dict) -> dict:
    """卡片上用户看得到的结论：类型、区间、合计、笔数、口径，报告卡加上各项指标。"""
    out = {k: card.get(k) for k in ("type", "period", "total", "count", "kind") if card.get(k) is not None}
    if card.get("kpis"):
        out["kpis"] = [k["value"] for k in card["kpis"]]
    return out


def main() -> int:
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    tmp = tempfile.mkdtemp()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/compare.db"
    os.environ["WECOM_BOT_ID"] = os.environ["WECOM_BOT_SECRET"] = ""
    os.environ.setdefault("DEMO_DATE", "2026-10-30")

    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    runs = []
    with TestClient(create_app()) as client:
        if not get_settings().llm_ready:
            print("没有可用的模型：先在「设置」→「模型」里配好密钥")
            return 1
        for workflow in (True, False):
            get_settings().workflow_enabled = workflow
            for text, want in QUESTIONS:
                for i in range(reps):
                    started = time.perf_counter()
                    r = client.post("/api/chat", json={"text": text}).json()
                    sec = time.perf_counter() - started
                    cards = [_sig(c) for c in r["cards"]]
                    runs.append({"workflow": workflow, "text": text, "want": want, "sec": sec, "cards": cards})
                    print(f"{'工作流' if workflow else '模型  '} {sec:5.2f}s {[c['type'] for c in cards]} {text}", flush=True)

    truth = {r["text"]: r["cards"] for r in runs if r["workflow"]}
    print()
    for workflow, name in ((True, "确定性工作流"), (False, "全部交给模型")):
        rs = [r for r in runs if r["workflow"] is workflow]
        typed = sum(1 for r in rs if r["cards"] and r["cards"][0]["type"] == r["want"])
        exact = sum(1 for r in rs if r["cards"] == truth[r["text"]])
        answers = defaultdict(set)
        for r in rs:
            answers[r["text"]].add(repr(r["cards"]))
        stable = sum(1 for v in answers.values() if len(v) == 1)
        secs = [r["sec"] for r in rs]
        print(f"{name}：卡片类型对 {typed}/{len(rs)}，数字与参照一致 {exact}/{len(rs)}，"
              f"{reps} 遍结果都相同的问题 {stable}/{len(answers)}，平均 {statistics.mean(secs):.2f} 秒，"
              f"中位 {statistics.median(secs):.2f} 秒，最慢 {max(secs):.2f} 秒")
    print("\n模型和参照不一致的：")
    for r in runs:
        if not r["workflow"] and r["cards"] != truth[r["text"]]:
            print(f"  {r['text']}：应为 {truth[r['text']][0]}，实际 {r['cards'][0] if r['cards'] else '无卡片'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
