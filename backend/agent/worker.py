"""LiveKit Agent Worker 入口

启动方式：
    python -m livekit_backend.agent.worker
"""
import logging
import asyncio
from livekit import agents
from backend.core.config import settings
from backend.agent.outbound_agent import OutboundCallAgent

logger = logging.getLogger(__name__)


class OutboundAgentWorker:
    """外呼 Agent Worker"""

    def run(self):
        """启动 Worker"""
        logging.basicConfig(
            level=getattr(logging, settings.log_level.upper(), logging.INFO),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )

        logger.info(
            f"启动 OutboundAgentWorker: ws_url={settings.livekit_url}, "
            f"log_level={settings.log_level}"
        )

        # 定义 worker 选项
        worker_options = agents.WorkerOptions(
            entrypoint_fnc=self._handle_request,
            ws_url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
            worker_type=agents.WorkerType.ROOM,
        )

        # 运行（阻塞）
        agents.cli.run_app(worker_options)

    async def _handle_request(self, ctx: agents.JobContext):
        """处理 Agent 请求（每个 Room 一个）"""
        logger.info(f"收到 Agent 请求: room={ctx.room.name}")

        # 创建并运行外呼 Agent
        agent = OutboundCallAgent(ctx)
        await agent.run()


if __name__ == "__main__":
    worker = OutboundAgentWorker()
    worker.run()
