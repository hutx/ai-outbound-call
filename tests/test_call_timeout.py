"""
通话时长限制功能测试
"""
import pytest
from unittest.mock import patch


class TestCallTimeoutConfig:
    """测试通话时长配置从环境变量正确读取。"""

    def test_default_values(self):
        """默认值：5 分钟总时长，20 秒缓冲。"""
        from backend.core.config import AppConfig
        with patch.dict("os.environ", {}, clear=True):
            cfg = AppConfig()
            assert cfg.max_call_duration_seconds == 300
            assert cfg.call_end_buffer_seconds == 20

    def test_custom_values_from_env(self):
        """环境变量覆盖配置。"""
        from backend.core import config as config_module
        with patch.dict("os.environ", {
            "MAX_CALL_DURATION_SECONDS": "600",
            "CALL_END_BUFFER_SECONDS": "30",
        }, clear=True):
            # 重新实例化以测试 __post_init__
            cfg = config_module.AppConfig()
            assert cfg.max_call_duration_seconds == 600
            assert cfg.call_end_buffer_seconds == 30


def _make_agent(mock_session, mock_ctx):
    """创建 CallAgent 实例，提供 mock 依赖以避免连接真实服务。"""
    from backend.core.call_agent import CallAgent

    # 最小 mock ASR
    mock_asr = type("MockASR", (), {
        "start_session": lambda self: None,
        "send_audio": lambda self, audio: None,
        "stop_session": lambda self: None,
    })()
    # 最小 mock TTS
    mock_tts = type("MockTTS", (), {
        "synthesize": lambda self, text: b"",
    })()
    # 最小 mock LLM
    mock_llm = type("MockLLM", (), {
        "chat": lambda self, messages: "mock",
    })()

    return CallAgent(
        session=mock_session,
        context=mock_ctx,
        asr=mock_asr,
        tts=mock_tts,
        llm=mock_llm,
    )


class TestCallAgentTimeout:
    """测试 CallAgent 超时处理辅助函数。"""

    @pytest.mark.asyncio
    async def test_say_timeout_closing_with_closing_script(self):
        """超时结束语 = 固定前缀 + closing_script。"""
        from backend.core.call_agent import CallAgent

        # 构造最小 Mock
        mock_session = type("MockSession", (), {
            "stop_playback": lambda self: None,
        })()
        mock_ctx = type("MockCtx", (), {
            "uuid": "test-uuid-001",
            "result": None,
        })()

        agent = _make_agent(mock_session, mock_ctx)
        # 手动注入 _script_config（正常在 run() 中设置）
        agent._script_config = type("MockScript", (), {
            "closing_script": "感谢您的接听，再见！",
        })()
        agent._say_captured = []

        async def fake_say(text, speech_type="closing", record=True):
            agent._say_captured.append(text)

        agent._say = fake_say

        await agent._say_timeout_closing()

        expected = "您本次通话时长已结束。感谢您的接听，再见！"
        assert agent._say_captured == [expected]

    @pytest.mark.asyncio
    async def test_say_timeout_closing_without_closing_script(self):
        """closing_script 为空时 fallback 到默认值。"""
        from backend.core.call_agent import CallAgent

        mock_session = type("MockSession", (), {"stop_playback": lambda self: None})()
        mock_ctx = type("MockCtx", (), {"uuid": "test-uuid-002", "result": None})()

        agent = _make_agent(mock_session, mock_ctx)
        agent._script_config = type("MockScript", (), {"closing_script": None})()
        agent._say_captured = []

        async def fake_say(text, speech_type="closing", record=True):
            agent._say_captured.append(text)

        agent._say = fake_say

        await agent._say_timeout_closing()

        assert agent._say_captured == ["您本次通话时长已结束。感谢您的接听，再见！"]

    @pytest.mark.asyncio
    async def test_stop_tts_if_playing_cancels_barge_in_task(self):
        """打断 TTS 时取消残留的 barge-in ASR 任务。"""
        import asyncio
        from backend.core.call_agent import CallAgent

        mock_session = type("MockSession", (), {"stop_playback": lambda self: None})()
        mock_ctx = type("MockCtx", (), {"uuid": "test-uuid-003", "result": None})()

        agent = _make_agent(mock_session, mock_ctx)
        agent._barge_in_asr_task = asyncio.create_task(asyncio.sleep(100))

        await agent._stop_tts_if_playing()

        assert agent._barge_in_asr_task is None


class TestCallTimeoutIntegration:
    """集成测试：模拟完整超时触发流程。"""

    @pytest.mark.asyncio
    async def test_conversation_loop_breaks_on_timeout(self):
        """超时触发后 _conversation_loop 正确退出并播放结束语。"""
        import asyncio
        from backend.core.call_agent import CallAgent
        from backend.core.state_machine import CallResult

        # 构造 Mock Session
        mock_session = type("MockSession", (), {
            "_connected": True,
            "stop_playback": lambda self: asyncio.coroutine(lambda: None)(),
            "channel_vars": {},
        })()
        # 构造 Mock Context
        mock_ctx = type("MockCtx", (), {
            "uuid": "test-integration-001",
            "result": CallResult.NOT_ANSWERED,
            "script_id": "test",
            "messages": [],
            "is_active": True,
            "state": None,
        })()

        agent = _make_agent(mock_session, mock_ctx)
        agent._script_config = type("MockScript", (), {
            "closing_script": "感谢您的接听，再见！",
        })()

        # 手动设置超时信号
        agent._call_timeout.set()

        # 构造最小状态机
        class MockSM:
            def should_continue(self):
                return True
            def transition(self, state):
                pass
        agent.sm = MockSM()

        # 捕获 _say 调用
        agent._say_captured = []

        async def fake_say(text, speech_type="closing", record=True):
            agent._say_captured.append(text)

        agent._say = fake_say

        async def fake_say_opening():
            pass
        agent._say_opening = fake_say_opening

        await agent._conversation_loop()

        # 验证播放了超时结束语
        assert any("通话时长已结束" in msg for msg in agent._say_captured), \
            f"Expected timeout closing message, got: {agent._say_captured}"

    @pytest.mark.asyncio
    async def test_timeout_watchdog_sets_event_after_delay(self):
        """看门狗在指定时间后设置超时事件。"""
        import asyncio
        from unittest.mock import patch, MagicMock

        mock_session = type("MockSession", (), {
            "stop_playback": lambda self: None,
        })()
        mock_ctx = type("MockCtx", (), {
            "uuid": "test-watchdog",
            "result": None,
        })()

        with patch("backend.core.call_agent.config") as mock_config:
            # 总时长 3s，缓冲 1s → trigger_at = max(3-1, 1) = 2s
            mock_config.max_call_duration_seconds = 3
            mock_config.call_end_buffer_seconds = 1

            agent = _make_agent(mock_session, mock_ctx)
            agent._start_timeout_watchdog()

            # 1.5 秒时不应触发（1.5 < 2）
            await asyncio.sleep(1.5)
            assert not agent._call_timeout.is_set(), \
                "Watchdog should NOT have triggered at 1.5s (trigger_at=2s)"

            # 3 秒后应触发（3 > 2）
            await asyncio.sleep(1.5)
            assert agent._call_timeout.is_set(), \
                "Watchdog should have set the timeout event"

            # 清理
            if agent._call_timeout_task and not agent._call_timeout_task.done():
                agent._call_timeout_task.cancel()

    @pytest.mark.asyncio
    async def test_watchdog_respects_buffer_time(self):
        """看门狗触发时间 = 总时长 - 缓冲时间，且有 1s 最小触发下限。"""
        import asyncio
        from unittest.mock import patch

        mock_session = type("MockSession", (), {
            "stop_playback": lambda self: None,
        })()
        mock_ctx = type("MockCtx", (), {
            "uuid": "test-watchdog-buffer",
            "result": None,
        })()

        with patch("backend.core.call_agent.config") as mock_config:
            # 总时长 3s，缓冲 1s → trigger_at = max(3-1, 1) = 2s
            mock_config.max_call_duration_seconds = 3
            mock_config.call_end_buffer_seconds = 1

            agent = _make_agent(mock_session, mock_ctx)
            agent._start_timeout_watchdog()

            # 1.5 秒时不应触发（1.5 < 2）
            await asyncio.sleep(1.5)
            assert not agent._call_timeout.is_set(), \
                "Watchdog should NOT have triggered at 1.5s (trigger_at=2s)"

            # 3 秒时应触发（3 > 2）
            await asyncio.sleep(1.5)
            assert agent._call_timeout.is_set(), \
                "Watchdog should have triggered at 3s (trigger_at=2s)"

            if agent._call_timeout_task and not agent._call_timeout_task.done():
                agent._call_timeout_task.cancel()

    @pytest.mark.asyncio
    async def test_watchdog_minimum_trigger_floor(self):
        """看门狗有 1s 最小触发下限（max(trigger_at, 1)）。"""
        import asyncio
        from unittest.mock import patch

        mock_session = type("MockSession", (), {
            "stop_playback": lambda self: None,
        })()
        mock_ctx = type("MockCtx", (), {
            "uuid": "test-watchdog-floor",
            "result": None,
        })()

        with patch("backend.core.call_agent.config") as mock_config:
            # 0.5 - 0.1 = 0.4 → max(0.4, 1) = 1s 最小触发下限
            mock_config.max_call_duration_seconds = 0.5
            mock_config.call_end_buffer_seconds = 0.1

            agent = _make_agent(mock_session, mock_ctx)
            agent._start_timeout_watchdog()

            # 0.6 秒时不应触发（0.6 < 1）
            await asyncio.sleep(0.6)
            assert not agent._call_timeout.is_set(), \
                "Watchdog should NOT have triggered at 0.6s (min trigger=1s)"

            # 1.2 秒时应触发（1.2 > 1）
            await asyncio.sleep(0.6)
            assert agent._call_timeout.is_set(), \
                "Watchdog should have triggered at 1.2s (min trigger=1s)"

            if agent._call_timeout_task and not agent._call_timeout_task.done():
                agent._call_timeout_task.cancel()

    @pytest.mark.asyncio
    async def test_conversation_loop_continues_when_not_timed_out(self):
        """未超时时 _conversation_loop 正常继续运行。"""
        import asyncio
        from backend.core.call_agent import CallAgent
        from backend.core.state_machine import CallResult, CallState

        call_count = 0

        async def fake_listen_user(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return ("用户说话", False, False)
            return ("", False, False)  # 第一次返回空（模拟无回应）

        mock_session = type("MockSession", (), {
            "_connected": True,
            "stop_playback": lambda self: None,
            "channel_vars": {},
        })()
        mock_ctx = type("MockCtx", (), {
            "uuid": "test-no-timeout",
            "result": CallResult.NOT_ANSWERED,
            "script_id": "test",
            "messages": [],
            "is_active": True,
            "state": CallState.CONNECTED,
        })()

        agent = _make_agent(mock_session, mock_ctx)
        agent._script_config = type("MockScript", (), {
            "closing_script": "再见！",
        })()

        # 不设置超时
        assert not agent._call_timeout.is_set()

        # 构造状态机：循环最多 3 次后退出
        loop_iterations = [0]

        class MockSM:
            def should_continue(self):
                loop_iterations[0] += 1
                return loop_iterations[0] <= 3
            def transition(self, state):
                pass

        agent.sm = MockSM()
        agent._say_captured = []

        async def fake_say(text, speech_type="closing", record=True):
            agent._say_captured.append(text)

        agent._say = fake_say
        agent._listen_user = fake_listen_user

        async def fake_think_reply(text):
            return ("", "continue")
        agent._think_and_reply_stream = fake_think_reply

        async def fake_say_opening():
            pass
        agent._say_opening = fake_say_opening

        await agent._conversation_loop()

        # 验证循环正常执行了多次（没有被超时提前打断）
        assert call_count >= 2, f"Expected at least 2 _listen_user calls, got {call_count}"
        # 验证没有播放超时结束语
        assert not any("通话时长已结束" in msg for msg in agent._say_captured), \
            f"Should not have timeout message, got: {agent._say_captured}"
