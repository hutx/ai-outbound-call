"""FastAPI 主入口 - LiveKit 智能外呼系统

负责应用生命周期管理、路由注册、CORS 配置、静态文件挂载。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.core.config import settings
from backend.utils import db
from backend.api.scripts_api import router as scripts_router
from backend.api.tasks_api import router as tasks_router
from backend.api.monitor_api import router as monitor_router
from backend.api.calls_api import router as calls_router
from backend.services import task_service
from backend.services.sip_service import SipService
from backend.services.minio_service import minio_service

logger = logging.getLogger(__name__)

# 全局 SIP 服务实例（与 task_service 模块共享同一实例）
sip_service: SipService = task_service.sip_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # ── 启动 ──
    logging.basicConfig(level=getattr(logging, settings.log_level))
    logger.info("正在启动 LiveKit 智能外呼系统...")

    # 初始化数据库连接池
    await db.get_pool()
    logger.info("数据库连接池已就绪")

    # 初始化 MinIO bucket（确保 lk-recordings 存在）
    try:
        await minio_service.ensure_bucket()
        logger.info("MinIO bucket 已就绪")
    except Exception as e:
        logger.warning(f"MinIO bucket 初始化失败（录音存储可能不可用）: {e}")

    # 初始化 SIP 服务（task_service 模块共享同一实例）
    await sip_service.initialize()
    logger.info("LiveKit SIP 服务已就绪")

    # 将服务实例注入到 app.state，供其他模块通过 request.app.state 获取
    app.state.sip_service = sip_service
    app.state.task_service = task_service

    logger.info(
        "系统启动完成 - API: %s:%s",
        settings.api_host,
        settings.api_port,
    )

    yield

    # ── 关闭 ──
    logger.info("正在关闭系统...")
    await sip_service.close()
    await db.close_pool()
    logger.info("系统已关闭")


app = FastAPI(
    title="LiveKit 智能外呼系统",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(scripts_router)
app.include_router(tasks_router)
app.include_router(monitor_router)
app.include_router(calls_router)

# 静态文件（前端）
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists() and any(frontend_dir.iterdir()):
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
async def root():
    """根路由 - 返回前端页面或 API 信息"""
    index_file = frontend_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {
        "code": 0,
        "data": {
            "message": "LiveKit 智能外呼系统 API",
            "docs": "/docs",
            "version": "0.1.0",
        },
        "message": "ok",
    }
