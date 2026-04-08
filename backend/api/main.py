"""
FastAPI 主入口 — 生产级
────────────────────────
REST API   : 任务管理、通话记录、黑名单
WebSocket  : /ws/monitor 实时推送
Metrics    : /metrics  Prometheus 格式
Auth       : Bearer Token（API_TOKEN 环境变量）
Graceful   : SIGTERM → 等待活跃通话完成 → 关闭连接池
"""
import asyncio
import logging
import os
import signal
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi import status as http_status
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator
from websockets.exceptions import ConnectionClosed
# from dotenv import load_dotenv
# load_dotenv()

from backend.core.config import config
from backend.core.call_agent import CallAgent
from backend.core.scheduler import TaskScheduler
from backend.core.state_machine import CallContext, CallResult
from backend.services.esl_service import AsyncESLPool, ESLSocketServer, ESLSocketCallSession
from backend.services.asr_service import create_asr_client
from backend.services.tts_service import create_tts_client
from backend.services.llm_service import LLMService
from backend.services.crm_service import crm
from backend.api.scripts_api import router as scripts_router  # 新增：话术脚本API路由
from backend.core.auth import require_auth  #
from backend.utils.db import (
    init_db, dispose_db, list_call_records, get_call_stats,
)


logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 全局单例
# ─────────────────────────────────────────────────────────────
_esl_pool: Optional[AsyncESLPool] = None
_asr = None
_tts = None
_llm = None
_scheduler: Optional[TaskScheduler] = None

# 活跃通话 uuid → CallAgent
_active_calls: dict[str, CallAgent] = {}
# WebSocket 监控客户端
_ws_clients: list[WebSocket] = []

# Prometheus-style 计数器（轻量，不依赖 prometheus_client）
_metrics = {
    "calls_total": 0,
    "calls_active": 0,
    "calls_completed": 0,
    "calls_transferred": 0,
    "calls_error": 0,
    "tasks_created": 0,
    "tts_errors": 0,
    "asr_errors": 0,
    "llm_errors": 0,
}
_start_time = time.time()

# ─────────────────────────────────────────────────────────────
# API Token 鉴权
# ─────────────────────────────────────────────────────────────
# _API_TOKEN = os.environ.get("API_TOKEN", "")


# ─────────────────────────────────────────────────────────────
# ESL Outbound — 每路通话的入口
# ─────────────────────────────────────────────────────────────
async def _handle_call_session(session: ESLSocketCallSession):
    """FreeSWITCH 接通后主动连入，每路通话走这里"""
    # 完成握手，获取 channel 变量
    try:
        await session.connect()
    except Exception as e:
        logger.error(f"ESL 握手失败: {e}")
        return

    # 优先使用 originate 时注入的 origination_uuid
    call_uuid = (
        session.channel_vars.get("origination_uuid")
        or session.channel_vars.get("orig_uuid")
        or str(uuid.uuid4())
    )
    task_id = session.channel_vars.get("task_id", "unknown")
    script_id = session.channel_vars.get("script_id", "finance_product_a")
    phone_number = session.channel_vars.get("caller_id_number", "unknown")

    ctx = CallContext(
        uuid=call_uuid,
        task_id=task_id,
        phone_number=phone_number,
        script_id=script_id,
    )

    agent = CallAgent(
        session=session,
        context=ctx,
        asr=_asr,
        tts=_tts,
        llm=_llm,
    )

    _active_calls[call_uuid] = agent
    _metrics["calls_total"] += 1
    _metrics["calls_active"] += 1
    await _broadcast_stats()

    try:
        await agent.run()
    finally:
        _active_calls.pop(call_uuid, None)
        _metrics["calls_active"] -= 1

        # 更新结果计数
        if ctx.result == CallResult.COMPLETED:
            _metrics["calls_completed"] += 1
        elif ctx.result == CallResult.TRANSFERRED:
            _metrics["calls_transferred"] += 1
        elif ctx.result == CallResult.ERROR:
            _metrics["calls_error"] += 1

        # 通知任务调度器该号码已完成
        if _scheduler and ctx.task_id != "unknown":
            _scheduler.on_call_finished(
                ctx.task_id, ctx.phone_number, ctx.result,
                dial_attempts=ctx.dial_attempts,
                hangup_cause=ctx.hangup_cause,
                sip_code=ctx.sip_code,
            )

        await _broadcast_stats()


# ─────────────────────────────────────────────────────────────
# 生命周期
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _esl_pool, _asr, _tts, _llm, _scheduler

    logger.info("━" * 50)
    logger.info("  智能外呼系统启动")
    logger.info("━" * 50)

    # 数据库
    try:
        await init_db()
        logger.info("✓ 数据库初始化完成")
    except Exception as e:
        logger.warning(f"✗ 数据库连接失败（将在写入时重试）: {e}")

    # CRM 黑名单预热（从 DB 加载到内存）
    await crm.startup()
    logger.info("✓ CRM 服务就绪")

    # 服务单例
    _asr = create_asr_client()
    _tts = create_tts_client()
    _llm = LLMService()
    logger.info("✓ ASR / TTS / LLM 服务初始化完成")

    # ESL 连接池
    _esl_pool = AsyncESLPool(
        host=config.freeswitch.host,
        port=config.freeswitch.port,
        password=config.freeswitch.password,
        pool_size=5,
    )
    try:
        await _esl_pool.start()
        logger.info("✓ ESL 连接池就绪")
    except Exception as e:
        logger.warning(f"✗ ESL 连接池启动失败（FreeSWITCH 未运行？）: {e}")

    # 任务调度器
    _scheduler = TaskScheduler(esl_pool=_esl_pool)

    # ESL Socket Server（接受 FS 主动连入）
    esl_server = ESLSocketServer(
        host="0.0.0.0",
        port=config.freeswitch.socket_port,
        call_handler=_handle_call_session,
        max_connections=config.max_concurrent_calls + 50,
    )
    server_task = asyncio.create_task(esl_server.start())
    logger.info(f"✓ ESL Socket Server 监听 :{config.freeswitch.socket_port}")

    # TTS 缓存后台清理
    from backend.utils.tts_cache import start_cache_cleaner
    cache_cleaner_task = asyncio.create_task(start_cache_cleaner())
    logger.info("✓ TTS 缓存清理器已启动")

    # SIGTERM 优雅退出
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_graceful_shutdown()))

    logger.info("  系统就绪，等待外呼任务")
    logger.info("━" * 50)

    yield

    # ── 关闭流程 ──────────────────────────────────────────────
    logger.info("开始优雅关闭...")
    server_task.cancel()
    cache_cleaner_task.cancel()
    await asyncio.gather(server_task, cache_cleaner_task, return_exceptions=True)

    if _active_calls:
        logger.info(f"等待 {len(_active_calls)} 路活跃通话结束...")
        await asyncio.wait_for(
            asyncio.gather(*[a.session.hangup() for a in _active_calls.values()],
                           return_exceptions=True),
            timeout=10.0,
        )

    if _esl_pool:
        await _esl_pool.stop()

    await dispose_db()
    logger.info("服务已关闭")


async def _graceful_shutdown():
    logger.info("收到 SIGTERM，准备优雅退出")
    for agent in list(_active_calls.values()):
        try:
            await agent.session.hangup("MANAGER_REQUEST")
        except Exception:
            pass


app = FastAPI(
    title="智能外呼平台",
    description="FreeSWITCH + Claude LLM 智能外呼系统",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if os.environ.get("DEBUG") else None,  # 生产关闭 Swagger
    redoc_url=None,
)

# 新增：话术脚本路由
app.include_router(scripts_router)


# ─────────────────────────────────────────────────────────────
# Pydantic 模型
# ─────────────────────────────────────────────────────────────
class CreateTaskRequest(BaseModel):
    name: str
    phone_numbers: list[str]
    script_id: str = "finance_product_a"
    concurrent_limit: int = 5
    max_retries: int = 1
    caller_id: str = ""

    @field_validator("phone_numbers")
    @classmethod
    def validate_phones(cls, v):
        # import re
        # valid = [p.strip() for p in v if re.match(r"^1[3-9]\d{9}$", p.strip())]
        # if not valid:
        #     raise ValueError("至少需要一个有效的手机号码（11位，1开头）")
        # return valid
        return v

    @field_validator("concurrent_limit")
    @classmethod
    def validate_concurrent(cls, v):
        return max(1, min(v, config.max_concurrent_calls))


class BlacklistRequest(BaseModel):
    phone: str
    reason: str = "手动添加"


# ─────────────────────────────────────────────────────────────
# 任务管理 API
# ─────────────────────────────────────────────────────────────
@app.post("/api/tasks", dependencies=[Depends(require_auth)])
async def create_task(req: CreateTaskRequest):
    """创建并启动外呼任务"""
    if not _scheduler:
        raise HTTPException(503, "调度器未初始化")

    task = _scheduler.create_task(
        name=req.name,
        phone_numbers=req.phone_numbers,
        script_id=req.script_id,
        concurrent_limit=req.concurrent_limit,
        max_retries=req.max_retries,
    )
    # 覆盖 caller_id
    if req.caller_id:
        for pr in task.phones:
            pr.__dict__.setdefault("caller_id", req.caller_id)

    await _scheduler.start_task(task.task_id)  # 不需要保存返回值
    _metrics["tasks_created"] += 1

    logger.info(f"创建任务 {task.task_id}: {req.name} ({len(req.phone_numbers)} 个号码)")
    return {
        "task_id": task.task_id,
        "name": task.name,
        "status": task.status.name,
        "total": task.total,
        "message": f"已创建，共 {task.total} 个号码",
    }


@app.get("/api/tasks", dependencies=[Depends(require_auth)])
async def list_tasks():
    if not _scheduler:
        return {"tasks": []}
    return {"tasks": _scheduler.list_tasks()}


@app.get("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def get_task(task_id: str):
    if not _scheduler:
        raise HTTPException(503, "调度器未初始化")
    task = _scheduler.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task.to_dict()


@app.post("/api/tasks/{task_id}/pause", dependencies=[Depends(require_auth)])
async def pause_task(task_id: str):
    if not _scheduler or not _scheduler.pause_task(task_id):
        raise HTTPException(404, "任务不存在或无法暂停")
    return {"message": "已暂停"}


@app.post("/api/tasks/{task_id}/resume", dependencies=[Depends(require_auth)])
async def resume_task(task_id: str):
    if not _scheduler or not _scheduler.resume_task(task_id):
        raise HTTPException(404, "任务不存在或无法恢复")
    return {"message": "已恢复"}


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def cancel_task(task_id: str):
    if not _scheduler or not _scheduler.cancel_task(task_id):
        raise HTTPException(404, "任务不存在")
    return {"message": "已取消"}


# ─────────────────────────────────────────────────────────────
# 通话记录 API
# ─────────────────────────────────────────────────────────────
@app.get("/api/calls", dependencies=[Depends(require_auth)])
async def list_calls(
    task_id: Optional[str] = None,
    phone: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    try:
        records = await list_call_records(
            task_id=task_id, phone=phone, result=result,
            limit=limit, offset=offset,
        )
        return {"records": records, "total": len(records)}
    except Exception as e:
        logger.error(f"查询通话记录失败: {e}")
        return {"records": [], "total": 0}


@app.get("/api/calls/stats", dependencies=[Depends(require_auth)])
async def call_stats(task_id: Optional[str] = None):
    """聚合统计：接通率、意向率、平均时长"""
    try:
        return await get_call_stats(task_id=task_id)
    except Exception as e:
        logger.error(f"统计查询失败: {e}")
        return {"total": 0, "connected": 0, "intent": 0,
                "connect_rate": 0.0, "intent_rate": 0.0}


# ─────────────────────────────────────────────────────────────
# 黑名单管理 API
# ─────────────────────────────────────────────────────────────
@app.get("/api/blacklist", dependencies=[Depends(require_auth)])
async def list_blacklist():
    """查看所有黑名单号码"""
    try:
        items = await crm.list_blacklist()
        return {"items": items, "total": len(items)}
    except Exception as e:
        return {"items": [], "total": 0}


@app.post("/api/blacklist", dependencies=[Depends(require_auth)])
async def add_blacklist(req: BlacklistRequest):
    await crm.add_to_blacklist(req.phone, req.reason)
    return {"message": f"{req.phone} 已加入黑名单"}


@app.delete("/api/blacklist/{phone}", dependencies=[Depends(require_auth)])
async def remove_blacklist(phone: str):
    await crm.remove_from_blacklist(phone)
    return {"message": f"{phone} 已从黑名单移除"}


# ─────────────────────────────────────────────────────────────
# 测试通话功能 API
# ─────────────────────────────────────────────────────────────
class TestCallRequest(BaseModel):
    phone_number: str = "test_001"
    script_id: str = "default"
    customer_info: dict = {}


@app.post("/api/test-call/start", dependencies=[Depends(require_auth)])
async def start_test_call(req: TestCallRequest):
    """启动测试通话 - 模拟FreeSWITCH连接后端服务进行AI通话"""
    from backend.core.call_agent import CallAgent
    from backend.core.state_machine import CallContext, CallState, CallIntent, CallResult

    # 定义本地 MockESLCallSession 类，确保包含所有 CallAgent 需要的方法
    class MockESLCallSession:
        """模拟ESL通话会话，用于测试AI通话功能"""

        def __init__(self, uuid: str, channel_vars: dict, asr_client, tts_client):
            self.uuid = uuid
            self.channel_vars = channel_vars
            self.asr_client = asr_client
            self.tts_client = tts_client
            self._connected = True
            self.call_state = "ACTIVE"
            self.ws_connection = None  # WebSocket连接用于实时交互
            self._playback_done = asyncio.Event()
            self._speech_active = False  # 标记是否正在播放/录音中
            self._event_queue = asyncio.Queue(maxsize=200)
            self._audio_queue = asyncio.Queue(maxsize=500)
            self._ws_ready = asyncio.Event()  # WebSocket 连接就绪信号
            self._first_listen = True  # 首次监听标记，不清空预存的音频

        async def connect(self):
            """模拟连接ESL服务器"""
            logger.info(f"Mock ESL connected for call {self.uuid}")
            self._connected = True
            return self.channel_vars  # 返回channel变量

        async def answer(self):
            """模拟接听电话"""
            logger.info(f"Mock call {self.uuid} answered")
            self.call_state = "ANSWERED"

        async def hangup(self, cause="NORMAL_CLEARING"):
            """模拟挂断电话"""
            logger.info(f"Mock call {self.uuid} hung up: {cause}")
            self._connected = False
            self.call_state = "HUNG_UP"

            # 发送挂断事件
            await self._safe_put(self._event_queue, {"type": "hangup", "cause": cause})

        async def playback(self, file_path: str):
            """模拟播放音频"""
            logger.info(f"Mock playback {file_path} in call {self.uuid}")

        async def play(self, audio_path: str, timeout: float = 60.0):
            """模拟播放音频文件，阻塞直到播放完成或超时"""
            logger.info(f"Mock play {audio_path} in call {self.uuid}")
            self._playback_done.clear()
            self._speech_active = True

            # 等待 WebSocket 连接就绪（最多等 5 秒）
            try:
                await asyncio.wait_for(self._ws_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.uuid}] play() 等待 WebSocket 超时，跳过发送")

            if self.ws_connection:
                text_to_send = getattr(self, "_current_tts_text", "")
                if text_to_send:
                    # 先发送文本（用于聊天展示），标记音频即将到达
                    try:
                        await self.ws_connection.send_json({
                            "type": "ai_response", "text": text_to_send, "has_audio": True
                        })
                        logger.info(f"[{self.uuid}] play() 发送 ai_response: {text_to_send[:50]}...")
                    except Exception:
                        pass
                    # 再发送真实 TTS 音频 bytes（前端用 Audio 元素播放）
                    try:
                        import aiofiles
                        async with aiofiles.open(audio_path, "rb") as f:
                            audio_data = await f.read()
                        await self.ws_connection.send_bytes(audio_data)
                        logger.info(f"[{self.uuid}] play() 发送 TTS 音频 {len(audio_data)} bytes → {audio_path}")
                    except Exception as e:
                        logger.warning(f"[{self.uuid}] play() 发送音频失败: {e}")

            # 模拟播放时间
            await asyncio.sleep(min(len(audio_path) * 0.01, timeout))
            self._speech_active = False
            self._playback_done.set()

        async def stop_playback(self):
            """打断当前播放（barge-in）"""
            logger.info(f"Mock stop playback in call {self.uuid}")
            self._playback_done.set()

        async def speak_text(self, text: str) -> str:
            """模拟TTS语音合成并播放"""
            logger.info(f"Mock TTS: {text}")
            # 模拟TTS处理时间
            await asyncio.sleep(len(text) * 0.05)  # 估算播放时间

            # 如果存在WebSocket连接，将TTS结果发送到前端
            if hasattr(self, 'ws_connection') and self.ws_connection:
                try:
                    await self.ws_connection.send_json({"type": "ai_response", "text": text})
                except:
                    pass  # WebSocket可能已关闭

            return f"MOCK_TTS_{self.uuid}"

        async def record_audio(self, timeout: int = 10) -> tuple[bool, str]:
            """模拟录音（等待用户输入）"""
            logger.info(f"Mock recording in call {self.uuid}, timeout: {timeout}s")

            # 如果存在WebSocket连接，等待来自前端的语音输入
            if hasattr(self, 'ws_connection') and self.ws_connection:
                try:
                    # 在实际应用中，这里会等待前端通过WebSocket发送语音数据
                    # 现在我们返回一个占位符
                    pass
                except:
                    pass

            # 模拟录音超时
            await asyncio.sleep(timeout)
            return False, ""  # 模拟没有检测到语音

        def get_variable(self, var_name: str) -> str:
            """获取通道变量"""
            return self.channel_vars.get(var_name, "")

        async def set_variable(self, var_name: str, value: str):
            """设置通道变量"""
            self.channel_vars[var_name] = value
            logger.info(f"Set channel var {var_name}={value} in call {self.uuid}")

        async def start_audio_capture(self) -> asyncio.Queue:
            """开始捕获通话音频，返回PCM数据队列"""
            logger.info(f"Mock start audio capture in call {self.uuid}")
            return self._audio_queue

        async def read_events(self):
            """持续读取并分发ESL事件"""
            logger.info(f"Mock read events for call {self.uuid}")
            while self._connected:
                await asyncio.sleep(1)

        async def transfer_to_human(self, extension: str = "8001"):
            """模拟转接人工坐席"""
            logger.info(f"Mock transfer to human {extension} for call {self.uuid}")
            await self.hangup("TRANSFER_TO_HUMAN")

        async def wait_for_hangup(self, timeout: float = 3600.0) -> str:
            """等待通话挂断，返回hangup cause"""
            # 简单实现，等待一定时间后返回
            try:
                event = await asyncio.wait_for(self.wait_for_event("hangup"), timeout=timeout)
                return event.get("cause", "UNKNOWN") if event else "UNKNOWN"
            except asyncio.TimeoutError:
                return "TIMEOUT"

        async def wait_for_event(self, event_type: str, timeout: float = 30.0) -> Optional[dict]:
            """等待特定类型事件"""
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
                if event.get("type") == event_type:
                    return event
                return None
            except asyncio.TimeoutError:
                return None

        @staticmethod
        async def _safe_put(queue: asyncio.Queue, item):
            """non-blocking put，队满时丢弃最老的"""
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass

    # 创建模拟的通话上下文
    call_uuid = str(uuid.uuid4())
    ctx = CallContext(
        uuid=call_uuid,
        task_id="test_task_" + call_uuid[:8],
        phone_number=req.phone_number,
        script_id=req.script_id,
        state=CallState.DIALING,
        intent=CallIntent.UNKNOWN,
        result=CallResult.NOT_ANSWERED,  # 使用已有的状态
        created_at=datetime.now(),
        answered_at=None,
        ended_at=None,
        user_utterances=0,
        ai_utterances=0,
        recording_path=None,
        messages=[],
        customer_info=req.customer_info,  # 使用传入的客户信息
    )

    # 创建模拟的ESL会话
    session = MockESLCallSession(  # 现在使用函数内定义的类
        uuid=call_uuid,
        channel_vars={
            "destination_number": req.phone_number,
            "context": "test_context",
            "task_id": ctx.task_id,
            "script_id": req.script_id,
        },
        asr_client=_asr,
        tts_client=_tts
    )

    # 创建AI通话代理
    agent = CallAgent(
        session=session,
        context=ctx,
        asr=_asr,
        tts=_tts,
        llm=_llm,
    )

    # 将通话添加到活跃通话列表
    _active_calls[call_uuid] = agent

    # 更新统计
    _metrics["calls_total"] += 1
    _metrics["calls_active"] += 1
    await _broadcast_stats()

    #  Monkey-patch _say 以保存文本供 play() 发送到前端
    _original_say = agent._say

    async def _patched_say(text: str, record: bool = True):
        agent.session._current_tts_text = text  # 保存文本供 mock play 使用
        await _original_say(text, record)

    agent._say = _patched_say

    # 在后台运行AI通话（走 CallAgent 真实生命周期）
    asyncio.create_task(_run_test_call_with_agent(agent, call_uuid))

    logger.info(f"启动测试通话 {call_uuid} -> {req.phone_number}")
    return {
        "call_id": call_uuid,
        "phone_number": req.phone_number,
        "message": "测试通话已启动"
}


async def _run_test_call_with_agent(agent: CallAgent, call_uuid: str):
    """在后台运行测试通话，使用真实的 CallAgent 生命周期"""
    try:
        logger.info(f"测试通话 {call_uuid} 已启动")
        await agent.run()  # 使用真实的 CallAgent 生命周期
    except Exception as e:
        logger.error(f"测试通话 {call_uuid} 出现异常: {e}", exc_info=True)
    finally:
        # 从活跃通话列表中移除
        _active_calls.pop(call_uuid, None)
        _metrics["calls_active"] -= 1

        # 更新结果计数
        ctx = agent.ctx
        if ctx.result == CallResult.COMPLETED:
            _metrics["calls_completed"] += 1
        elif ctx.result == CallResult.TRANSFERRED:
            _metrics["calls_transferred"] += 1
        elif ctx.result == CallResult.ERROR:
            _metrics["calls_error"] += 1

        await _broadcast_stats()


# ─────────────────────────────────────────────────────────────
# 实时测试通话 WebSocket - 处理前端麦克风音频流
# ─────────────────────────────────────────────────────────────
@app.websocket("/ws/test-call/{call_id}")
async def test_call_ws(websocket: WebSocket, call_id: str):
    """测试通话WebSocket - 实时语音交互（走 CallAgent 真实 ASR 路径）"""
    token = websocket.query_params.get("token", "")
    if config.api_token.strip() and token != config.api_token.strip():
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    logger.info(f"测试通话WebSocket连接: {call_id}")

    # 查找对应的通话代理
    agent = None
    max_wait_time = 10
    wait_interval = 0.5
    waited_time = 0

    while waited_time < max_wait_time:
        agent = _active_calls.get(call_id)
        if agent:
            break
        await asyncio.sleep(wait_interval)
        waited_time += wait_interval

    if not agent:
        logger.warning(f"测试通话WebSocket连接失败: 通话不存在或超时 - {call_id}")
        await websocket.close(code=4004, reason="通话不存在或尚未初始化")
        return

    # 将WebSocket连接绑定到会话对象上
    agent.session.ws_connection = websocket
    agent.session._ws_ready.set()  # 通知 play() WebSocket 已就绪

    try:
        # 发送初始连接成功的消息
        await websocket.send_json({"type": "call_status", "status": "connected", "message": "通话连接成功"})

        # 实时接收来自前端的数据，仅作为管道
        # CallAgent.run() 在后台驱动 ASR → LLM → TTS → play() 的完整流程
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=1.0)

                if data['type'] == 'websocket.receive':
                    if 'text' in data:
                        msg = json.loads(data['text'])
                        if msg.get("type") == "audio_end":
                            # 用户说完话，发送空信号触发 ASR 结束
                            logger.info(f"[{call_id}] 收到 audio_end，触发 ASR 结算")
                            await agent.session._audio_queue.put(b"")
                        elif msg.get("type") == "hangup":
                            # 前端请求挂断
                            logger.info(f"[{call_id}] 收到 hangup 请求")
                            await agent.session.hangup()
                            break
                        # 其他文本消息忽略（用户音频通过二进制发送）

                    elif 'bytes' in data:
                        # 二进制音频数据 → 送入队列，由 CallAgent 的 ASR 处理
                        audio_data = data['bytes']
                        logger.info(f"[{call_id}] 收到音频 {len(audio_data)} bytes, queue_size={agent.session._audio_queue.qsize()}")
                        await agent.session._audio_queue.put(audio_data)
                    else:
                        logger.debug(f"[{call_id}] 收到未知数据: {list(data.keys())}")

            except asyncio.TimeoutError:
                # 检查通话是否仍然活跃
                if call_id not in _active_calls:
                    logger.info(f"通话已结束，WebSocket连接关闭: {call_id}")
                    break
                continue
            except (WebSocketDisconnect, ConnectionClosed):
                logger.info(f"测试通话WebSocket断开: {call_id}")
                break
            except RuntimeError as e:
                # WebSocket 已断开时调用 receive() 会抛 RuntimeError
                if "disconnect" in str(e).lower():
                    logger.info(f"测试通话WebSocket已断开: {call_id}")
                else:
                    logger.error(f"测试通话WebSocket错误: {e}")
                break
            except Exception as e:
                logger.error(f"测试通话WebSocket接收数据错误: {e}")
                break

    except (WebSocketDisconnect, ConnectionClosed):
        logger.info(f"测试通话WebSocket正常断开: {call_id}")
    except RuntimeError as e:
        if "disconnect" not in str(e).lower():
            logger.error(f"测试通话WebSocket异常: {e}")
    except Exception as e:
        logger.error(f"测试通话WebSocket异常: {e}")
    finally:
        # 清理WebSocket连接引用
        if agent and hasattr(agent.session, 'ws_connection'):
            agent.session.ws_connection = None

        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/api/test-call/{call_id}/hangup", dependencies=[Depends(require_auth)])
async def hangup_test_call(call_id: str):
    """挂断测试通话"""
    agent = _active_calls.get(call_id)
    if not agent:
        raise HTTPException(404, "通话不存在")

    try:
        await agent.session.hangup()
        return {"message": f"通话 {call_id} 已挂断"}
    except Exception as e:
        logger.error(f"挂断通话 {call_id} 失败: {e}")
        raise HTTPException(500, f"挂断失败: {e}")


@app.get("/api/test-call/{call_id}/messages", dependencies=[Depends(require_auth)])
async def get_test_call_messages(call_id: str):
    """获取测试通话的消息历史"""
    agent = _active_calls.get(call_id)
    if not agent:
        raise HTTPException(404, "通话不存在")

    return {
        "call_id": call_id,
        "messages": agent.ctx.messages,
        "state": agent.ctx.state.name,
        "intent": agent.ctx.intent.value,
        "result": agent.ctx.result.value
    }





# ─────────────────────────────────────────────────────────────
# TTS 测试 API
# ─────────────────────────────────────────────────────────────
class TTSRequest(BaseModel):
    text: str = Field(default="您好，这是AI智能外呼系统的语音测试")


@app.post("/api/tts/synthesize")
async def synthesize_tts(req: TTSRequest):
    """调用当前配置的 TTS 服务合成语音，返回 WAV 音频流"""
    if not req.text.strip():
        raise HTTPException(400, "文本为空")

    wav_path = await _tts.synthesize(req.text)

    if not wav_path or not os.path.exists(wav_path):
        raise HTTPException(500, "TTS 合成失败")

    return FileResponse(
        wav_path,
        media_type="audio/wav",
        filename="tts_output.wav",
    )


@app.get("/api/stats")
async def get_stats():
    tasks = _scheduler.list_tasks() if _scheduler else []
    return {
        "active_calls":      _metrics["calls_active"],
        "calls_total":       _metrics["calls_total"],
        "calls_completed":   _metrics["calls_completed"],
        "calls_transferred": _metrics["calls_transferred"],
        "calls_error":       _metrics["calls_error"],
        "active_tasks":      sum(1 for t in tasks if t["status"] == "RUNNING"),
        "total_tasks":       len(tasks),
        "max_concurrent":    config.max_concurrent_calls,
        "uptime_seconds":    int(time.time() - _start_time),
        "tasks":             tasks[:20],
    }


@app.get("/health")
async def health():
    esl_ok = bool(
        _esl_pool is not None
        and _esl_pool._conns
        and any(c.is_connected for c in _esl_pool._conns)
    )
    return {
        "status":       "ok" if esl_ok else "degraded",
        "esl_pool":     esl_ok,
        "active_calls": _metrics["calls_active"],
        "timestamp":    datetime.now().isoformat(),
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus 文本格式指标"""
    tasks = _scheduler.list_tasks() if _scheduler else []
    active_tasks = sum(1 for t in tasks if t["status"] == "RUNNING")
    uptime = int(time.time() - _start_time)
    lines = [
        "# HELP outbound_calls_total Total outbound calls initiated",
        "# TYPE outbound_calls_total counter",
        f'outbound_calls_total {_metrics["calls_total"]}',
        "# HELP outbound_calls_active Currently active calls",
        "# TYPE outbound_calls_active gauge",
        f'outbound_calls_active {_metrics["calls_active"]}',
        "# HELP outbound_calls_completed_total Completed calls",
        "# TYPE outbound_calls_completed_total counter",
        f'outbound_calls_completed_total {_metrics["calls_completed"]}',
        "# HELP outbound_calls_transferred_total Transferred to human calls",
        "# TYPE outbound_calls_transferred_total counter",
        f'outbound_calls_transferred_total {_metrics["calls_transferred"]}',
        "# HELP outbound_calls_error_total Error calls",
        "# TYPE outbound_calls_error_total counter",
        f'outbound_calls_error_total {_metrics["calls_error"]}',
        "# HELP outbound_tasks_active Active running tasks",
        "# TYPE outbound_tasks_active gauge",
        f'outbound_tasks_active {active_tasks}',
        "# HELP outbound_tasks_total Total tasks created",
        "# TYPE outbound_tasks_total counter",
        f'outbound_tasks_total {_metrics["tasks_created"]}',
        "# HELP outbound_uptime_seconds Service uptime in seconds",
        "# TYPE outbound_uptime_seconds gauge",
        f'outbound_uptime_seconds {uptime}',
        "",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# WebSocket 实时监控
# ─────────────────────────────────────────────────────────────
@app.websocket("/ws/monitor")
async def monitor_ws(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    if config.api_token.strip() and token != config.api_token.strip():
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    _ws_clients.append(websocket)
    logger.debug(f"监控 WS 连接，当前 {len(_ws_clients)} 个客户端")

    try:
        await websocket.send_json(await _build_monitor_data())
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if msg == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                await websocket.send_text("ping")
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


async def _broadcast_stats():
    if not _ws_clients:
        return
    data = await _build_monitor_data()
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


async def _build_monitor_data() -> dict:
    tasks = _scheduler.list_tasks() if _scheduler else []
    return {
        "type":         "stats",
        "ts":           datetime.now().isoformat(),
        "active_calls": _metrics["calls_active"],
        "calls_total":  _metrics["calls_total"],
        "active_tasks": sum(1 for t in tasks if t["status"] == "RUNNING"),
        "tasks":        tasks[:10],
    }


# ─────────────────────────────────────────────────────────────
# 回拨计划 API
# ─────────────────────────────────────────────────────────────
@app.get("/api/callbacks", dependencies=[Depends(require_auth)])
async def list_callbacks():
    """查看待回拨名单"""
    try:
        from backend.utils.db import db_list_callbacks
        items = await db_list_callbacks(status="pending")
        return {"items": items, "total": len(items)}
    except:
        return {"items": [], "total": 0}


# ─────────────────────────────────────────────────────────────
# 前端静态文件
# ─────────────────────────────────────────────────────────────
@app.get("/")
async def serve_console():
    path = "frontend/index.html"
    if os.path.exists(path):
        return FileResponse(path)
    return {"message": "Frontend not found"}
