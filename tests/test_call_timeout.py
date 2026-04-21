"""
通话时长限制功能测试
"""
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


import pytest


class TestCallAgentTimeout:
    """测试 CallAgent 超时处理辅助函数。"""

    def _make_agent(self, mock_session, mock_ctx):
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

        agent = self._make_agent(mock_session, mock_ctx)
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

        agent = self._make_agent(mock_session, mock_ctx)
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

        agent = self._make_agent(mock_session, mock_ctx)
        agent._barge_in_asr_task = asyncio.create_task(asyncio.sleep(100))

        await agent._stop_tts_if_playing()

        assert agent._barge_in_asr_task is None
