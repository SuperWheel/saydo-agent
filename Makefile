.PHONY: install api web dev test smoke-llm eval-llm seed

install:
	cd apps/api && uv sync
	cd apps/web && npm install

api:
	cd apps/api && uv run uvicorn app.main:app --reload --port 8000

web:
	cd apps/web && npm run dev

# 同时启动前后端
dev:
	$(MAKE) -j2 api web

test:
	cd apps/api && uv run pytest -q

# 用 .env 里的真实模型跑几句对话，检查工具调用是否正常（需要模型密钥）
smoke-llm:
	cd apps/api && uv run python scripts/llm_smoke.py

# 真实模型回归集（apps/api/evals/regression.yaml），意图准确率低于 90% 时失败
eval-llm:
	cd apps/api && uv run python scripts/llm_eval.py

# 重置演示数据（后端需在运行）
seed:
	curl -s -X POST http://127.0.0.1:8000/dev/seed
