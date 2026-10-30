"""言行 SayDo Agent 后端入口。启动：uv run uvicorn app.main:app --reload"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .core_mock.seed import seed
from .db import Base, init_engine, new_session
from .routes import admin, api, dev
from .tools import bank  # noqa: F401  注册银行工具
from .tools import bill  # noqa: F401  注册账单分析工具
from .tools import card  # noqa: F401  注册卡片工具
from .tools import automation  # noqa: F401  注册跨场景工具（心愿罐、订花订蛋糕、定时转账）
from .tools import wealth  # noqa: F401  注册理财工具
from .tools import subscription  # noqa: F401  注册订阅代扣工具
from .tools import bill_analysis  # noqa: F401  注册自由分析（沙箱）工具


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        engine = init_engine(get_settings().database_url)
        Base.metadata.create_all(engine)
        with new_session() as db:
            seed(db)
        yield

    app = FastAPI(title="言行 SayDo Agent", version="0.1.0-m0", lifespan=lifespan)
    app.include_router(api)
    app.include_router(admin)
    app.include_router(dev)
    return app


app = create_app()
