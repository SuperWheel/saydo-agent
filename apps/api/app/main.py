"""言行 SayDo Agent 后端入口。启动：uv run uvicorn app.main:app --reload"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .channels import wecom
from .config import get_settings
from .core_mock.seed import catch_up, seed
from .db import Base, init_engine, new_session
from .routes import admin, api, dev, public
from .tools import bank  # noqa: F401  注册银行工具
from .tools import bill  # noqa: F401  注册账单分析工具
from .tools import card  # noqa: F401  注册卡片工具
from .tools import automation  # noqa: F401  注册跨场景工具（心愿罐、订花订蛋糕、定时转账）
from .tools import wealth  # noqa: F401  注册理财工具
from .tools import subscription  # noqa: F401  注册订阅代扣工具
from .tools import bill_analysis  # noqa: F401  注册自由分析（沙箱）工具
from .tools import knowledge  # noqa: F401  注册知识问答工具


def _log_to_console() -> None:
    """saydo.* 的 INFO 日志打到控制台（企业微信连上、收到消息等）。uvicorn 只配了它自己的 logger。"""
    logger = logging.getLogger("saydo")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def create_app() -> FastAPI:
    _log_to_console()
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        engine = init_engine(get_settings().database_url)
        Base.metadata.create_all(engine)
        with new_session() as db:
            seed(db)
            catch_up(db)
        await wecom.restart(get_settings())  # 企业微信渠道（可选）：没配 BotID / Secret 就不启动
        yield
        await wecom.shutdown()

    app = FastAPI(title="言行 SayDo Agent", version="0.1.0-m0", lifespan=lifespan)
    @app.middleware("http")
    async def _calendar(request: Request, call_next):
        # 跨过零点（快进、隔天接着用）后，先把新过去的日历流水补进来；同一天里只读一个标记
        with new_session() as db:
            catch_up(db)
        return await call_next(request)

    app.include_router(public)
    app.include_router(api)
    app.include_router(admin)
    app.include_router(dev)
    return app


app = create_app()
