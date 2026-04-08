"""
╔══════════════════════════════════════════════════════════════╗
║  完整可运行 Demo — 生产兼容版                                  ║
║  demo_runner.py                                              ║
║                                                              ║
║  无需 FreeSWITCH / ASR / TTS 即可运行，使用 Mock 服务         ║
║  CallAgent 逻辑与生产代码完全一致                              ║
║                                                              ║
║  运行方式：                                                   ║
║    pip install anthropic python-dotenv                       ║
║    ANTHROPIC_API_KEY=sk-ant-xxx python demo_runner.py        ║
║    python demo_runner.py --multi   # 四场景对比               ║
╚══════════════════════════════════════════════════════════════╝
"""
import asyncio
import logging
import os
import sys
import uuid
from typing import AsyncGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s \033[36m[%(name)s]\033[0m %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("\n❌  请先设置 ANTHROPIC_API_KEY 环境变量：")
    print("    export ANTHROPIC_API_KEY=sk-ant-xxxx\n")
    sys.exit(1)

os.environ.setdefault("ASR_PROVIDER", "mock")
os.environ.setdefault("TTS_PROVIDER", "mock")
os.environ.setdefault("LLM_MODEL", "claude-sonnet-4-20250514")
os.environ.setdefault("MAX_CALL_DURATION", "120")


# ══════════════════════════════════════════════════════════════
# Mock ESL Session — 完全兼容生产版 CallAgent 接口
# ══════════════════════════════════════════════════════════════
class MockESLSession:
    """
    模拟 FreeSWITCH ESL Socket Session
    实现了 CallAgent 所需的全部方法和属性
    """

    USER_SCRIPT: list[str] = [
        "可以，你说吧",
        "收益率是多少，有没有风险",
        "1万起投是吗，可以随时取出来吗",
        "好的，把资料发给我看看",
        "好，我考虑一下，你们到时候再联系我",
    ]

    def __init__(self, call_uuid: str, task_id: str = "demo_task", script_id: str = "finance_product_a"):
        self._uuid = call_uuid
        self._connected = True
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._audio_queue: asyncio.Queue = asyncio.Queue()
        self._playback_done = asyncio.Event()
        self._playback_done.set()
        self._turn = 0
        self._played_texts: list[str] = []
        self._variables: dict[str, str] = {}

        # ★ 关键：channel_vars 属性（CallAgent 从这里读 task_id/script_id）
        self._channel_vars: dict[str, str] = {
            "task_id":   task_id,
            "script_id": script_id,
            "ai_agent":  "true",
        }

    @property
    def uuid(self) -> str:
        return self._uuid

    @property
    def channel_vars(self) -> dict:
        """CallAgent 从此处读取 task_id / script_id"""
        return self._channel_vars

    async def connect(self) -> dict:
        print(f"\n{'━'*62}")
        print(f"  📞  通话接通  UUID: {self._uuid[:16]}...")
        print(f"{'━'*62}\n")
        return {
            "Unique-ID":                    self._uuid,
            "Caller-Destination-Number":    "19042638084",
            "variable_task_id":             self._channel_vars["task_id"],
            "variable_script_id":           self._channel_vars["script_id"],
        }

    async def read_events(self):
        """持续运行直到通话结束"""
        while self._connected:
            await asyncio.sleep(0.5)

    async def play(self, audio_path: str, timeout: float = 60.0):
        """模拟播放 — 打印最近一条 AI 话术"""
        self._playback_done.clear()
        if self._played_texts:
            print(f"\n  \033[94m🤖 AI:\033[0m  {self._played_texts[-1]}")
        await asyncio.sleep(0.1)
        self._playback_done.set()

    async def stop_playback(self):
        self._playback_done.set()

    async def set_variable(self, name: str, value: str):
        """记录 channel 变量（CDR 等用途）"""
        self._variables[name] = value
        logger.debug(f"[Mock] set_variable {name}={value}")

    async def start_audio_capture(self) -> asyncio.Queue:
        """触发下一轮用户说话，返回音频队列"""
        # 清空上一轮残留
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        await asyncio.sleep(0.2)  # 模拟用户思考

        if self._turn < len(self.USER_SCRIPT):
            user_text = self.USER_SCRIPT[self._turn]
            self._turn += 1
            print(f"  \033[92m👤 用户:\033[0m {user_text}")
            await self._audio_queue.put(user_text.encode("utf-8"))
        else:
            print(f"  \033[90m[用户挂断]\033[0m")
            self._connected = False
            await self._audio_queue.put(b"__HANGUP__")

        await self._audio_queue.put(b"")  # ASR 结束信号
        return self._audio_queue

    async def transfer_to_human(self, extension: str = "8001"):
        print(f"\n  \033[93m🔀 转接人工坐席 (分机 {extension})\033[0m")
        self._connected = False

    async def hangup(self, cause: str = "NORMAL_CLEARING"):
        if self._connected:
            logger.debug(f"[Mock] hangup cause={cause}")
        self._connected = False

    def push_ai_text(self, text: str):
        """由 DemoTTSClient 调用，记录 AI 将说的文字"""
        self._played_texts.append(text)


# ══════════════════════════════════════════════════════════════
# Mock ASR — 从音频队列读取预设文本
# ══════════════════════════════════════════════════════════════
from backend.services.asr_service import BaseASR, ASRResult


class DemoASRClient(BaseASR):
    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None]
    ) -> AsyncGenerator[ASRResult, None]:
        async for chunk in audio_gen:
            if not chunk:
                break
            if chunk == b"__HANGUP__":
                return
            text = chunk.decode("utf-8", errors="ignore").strip()
            if text:
                yield ASRResult(text=text, is_final=True, confidence=1.0)


# ══════════════════════════════════════════════════════════════
# Demo TTS — 记录文本到 session，不实际合成
# ══════════════════════════════════════════════════════════════
from backend.services.tts_service import MockTTSClient


class DemoTTSClient(MockTTSClient):
    def __init__(self, session: MockESLSession):
        self._session = session

    async def synthesize(self, text: str) -> str:
        self._session.push_ai_text(text)
        return await super().synthesize(text)


# ══════════════════════════════════════════════════════════════
# 主 Demo 函数
# ══════════════════════════════════════════════════════════════
async def run_demo(
    script_id: str = "finance_product_a",
    user_script: list[str] = None,
    customer_info: dict = None,
    label: str = "",
) -> dict:
    from backend.core.call_agent import CallAgent
    from backend.core.state_machine import CallContext
    from backend.services.llm_service import LLMService

    call_uuid = uuid.uuid4().hex

    if label:
        print(f"\n\n{'─'*62}")
        print(f"  📌  {label}")
        print(f"{'─'*62}")

    # 创建 Session
    session = MockESLSession(
        call_uuid,
        task_id="demo_task_001",
        script_id=script_id,
    )
    if user_script is not None:
        session.USER_SCRIPT = user_script

    ctx = CallContext(
        uuid=call_uuid,
        task_id="demo_task_001",
        phone_number="19042638084",
        script_id=script_id,
        customer_info=customer_info or {
            "name": "张先生",
            "note": "曾咨询过定期理财，对收益率较敏感",
        },
    )

    agent = CallAgent(
        session=session,
        context=ctx,
        asr=DemoASRClient(),
        tts=DemoTTSClient(session),
        llm=LLMService(),
    )

    await agent.run()

    return {
        "uuid":        ctx.uuid[:12],
        "result":      ctx.result.value,
        "intent":      ctx.intent.value,
        "turns":       ctx.user_utterances,
        "duration":    ctx.duration_seconds or 0,
        "ai_turns":    ctx.ai_utterances,
    }


async def print_report(stats: dict):
    print(f"\n  \033[36m📋 通话报告\033[0m")
    print(f"     结果:   {stats['result']}")
    print(f"     意图:   {stats['intent']}")
    print(f"     轮次:   用户 {stats['turns']} 轮 / AI {stats['ai_turns']} 轮")
    print(f"     时长:   {stats['duration']}s")


async def run_single_demo():
    print("\n" + "═"*62)
    print("   🚀  智能外呼系统 Demo  (单场景)")
    print("═"*62)
    stats = await run_demo()
    await print_report(stats)


async def run_multi_scenario():
    """四场景对比演示"""
    print("\n" + "═"*62)
    print("   🚀  智能外呼系统 Demo  (四场景对比)")
    print("═"*62)

    scenarios = [
        {
            "label":   "场景 A：用户有购买意向",
            "script":  ["可以说","收益多少","好的发我资料","我考虑一下"],
        },
        {
            "label":   "场景 B：用户现在忙碌",
            "script":  ["我现在开会","不方便","改天吧"],
        },
        {
            "label":   "场景 C：用户要求转人工",
            "script":  ["你是机器人吗","给我转真人","我要和人工客服说话"],
        },
        {
            "label":   "场景 D：用户明确拒绝（两次）",
            "script":  ["不需要","真的不要","不要再打了"],
        },
    ]

    all_stats = []
    for s in scenarios:
        stats = await run_demo(
            user_script=s["script"],
            label=s["label"],
        )
        await print_report(stats)
        all_stats.append({"场景": s["label"], **stats})

    print(f"\n\n{'═'*62}")
    print("   📊  场景汇总")
    print(f"{'═'*62}")
    print(f"  {'场景':<22} {'结果':<14} {'意图':<14} {'轮次':>4}")
    print(f"  {'─'*58}")
    for s in all_stats:
        label = s["场景"].replace("场景 ","").replace("：",":")[:22]
        print(f"  {label:<22} {s['result']:<14} {s['intent']:<14} {s['turns']:>4}")
    print(f"{'═'*62}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="智能外呼系统 Demo")
    parser.add_argument("--multi",  action="store_true", help="运行四场景对比演示")
    parser.add_argument("--script", default="finance_product_a",
                        help="话术模板 ID (默认: finance_product_a)")
    args = parser.parse_args()

    if args.multi:
        asyncio.run(run_multi_scenario())
    else:
        asyncio.run(run_single_demo())
