.PHONY: install api web dev web-lan dev-lan test smoke-llm eval-llm seed

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

# 同上，但前端对局域网开放（手机和电脑在同一网络时，手机能打开 App；企业微信演示用）。后端仍只监听本机，由前端代理
web-lan:
	cd apps/web && WEB_HOST=0.0.0.0 npm run dev

dev-lan:
	$(MAKE) -j2 api web-lan

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

# 红队测试集（规则模式 + 假模型，不耗 API）
.PHONY: redteam
redteam:
	cd apps/api && uv run pytest -q tests/redteam

# 端到端测试：演示主线，规则模式（不调模型），自己起 8765 / 5174 两个端口，不读 .env
# 首次运行前装浏览器：cd apps/web && npx playwright install chromium
.PHONY: e2e
e2e:
	cd apps/web && npm run e2e
