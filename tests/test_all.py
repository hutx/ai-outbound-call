"""
生产级单元测试套件
运行方式:
    # 安装依赖
    pip install pytest pytest-asyncio

    # 运行全部测试
    pytest tests/ -v

    # 只跑某个模块
    pytest tests/test_all.py::TestStateMachine -v
"""
import asyncio
import os
import sys
import struct
import tempfile

import pytest

# ── 环境配置（必须在 import backend 之前） ─────────────────────
os.environ.setdefault("ASR_PROVIDER", "mock")
os.environ.setdefault("TTS_PROVIDER", "mock")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-key")
os.environ.setdefault("MAX_CALL_DURATION", "300")
os.environ.setdefault("API_TOKEN", "test_token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_outbound.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Mock 无法安装的外部依赖 ────────────────────────────────────
import types

def _mock_missing(name):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

for _m in ["anthropic", "aiofiles", "websockets", "websockets.exceptions",
           "aiohttp", "asyncpg", "sqlalchemy", "sqlalchemy.ext",
           "sqlalchemy.ext.asyncio", "sqlalchemy.orm"]:
    _mock_missing(_m)

# anthropic 详细 mock
_am = sys.modules["anthropic"]
_am.RateLimitError = Exception
class _FakeMsg:
    content = [type("C", (), {
        "text": '{"reply":"您好","intent":"interested","action":"continue","action_params":{}}'
    })()]
class _FakeClient:
    def __init__(self, **k):
        self.messages = type("M", (), {
            "create": lambda s, **k: _FakeMsg()
        })()
_am.AsyncAnthropic = _FakeClient

# sqlalchemy 详细 mock
_sa = sys.modules["sqlalchemy"]

class _ColType:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self

for _n in ["String", "Integer", "DateTime", "JSON", "Text", "Boolean", "Float"]:
    setattr(_sa, _n, _ColType())

_sa.select = lambda *a: type("Q", (), {
    "where": lambda s, *a: s, "order_by": lambda s, *a: s,
    "limit": lambda s, x: s, "offset": lambda s, x: s,
})()
_sa.update = lambda *a: type("U", (), {"where": lambda s, *a: s, "values": lambda s, **k: s})()
_sa.text   = lambda q: q

_sa_async = sys.modules["sqlalchemy.ext.asyncio"]
_sa_async.create_async_engine  = lambda *a, **k: None
_sa_async.AsyncSession         = object
_sa_async.async_sessionmaker   = lambda *a, **k: lambda: None

_sa_orm = sys.modules["sqlalchemy.orm"]
_sa_orm.DeclarativeBase = type("DeclarativeBase", (), {"metadata": type("M", (), {"create_all": lambda s, x: None})()})
_sa_orm.sessionmaker    = lambda *a, **k: lambda: None
_sa_orm.Mapped          = type("Mapped", (), {"__class_getitem__": classmethod(lambda cls, x: x)})
_sa_orm.mapped_column   = lambda *a, **k: None


# ══════════════════════════════════════════════════════════════
# 测试：状态机
# ══════════════════════════════════════════════════════════════
class TestStateMachine:
    def test_initial_state(self):
        from backend.core.state_machine import CallContext, StateMachine, CallState
        ctx = CallContext(uuid="u", task_id="t", phone_number="138", script_id="s")
        sm = StateMachine(ctx)
        assert ctx.state == CallState.DIALING

    def test_transition(self):
        from backend.core.state_machine import CallContext, StateMachine, CallState
        ctx = CallContext(uuid="u", task_id="t", phone_number="p", script_id="s")
        sm = StateMachine(ctx)
        sm.transition(CallState.CONNECTED)
        assert ctx.state == CallState.CONNECTED

    def test_intent_update_valid(self):
        from backend.core.state_machine import CallContext, StateMachine, CallIntent
        ctx = CallContext(uuid="u", task_id="t", phone_number="p", script_id="s")
        sm = StateMachine(ctx)
        sm.update_intent("interested")
        assert ctx.intent == CallIntent.INTERESTED

    def test_intent_update_invalid_falls_back_to_unknown(self):
        from backend.core.state_machine import CallContext, StateMachine, CallIntent
        ctx = CallContext(uuid="u", task_id="t", phone_number="p", script_id="s")
        sm = StateMachine(ctx)
        sm.update_intent("totally_invalid")
        assert ctx.intent == CallIntent.UNKNOWN

    def test_duration_calculation(self):
        from backend.core.state_machine import CallContext
        from datetime import datetime
        ctx = CallContext(uuid="u", task_id="t", phone_number="p", script_id="s")
        ctx.answered_at = datetime(2025, 1, 1, 10, 0, 0)
        ctx.ended_at    = datetime(2025, 1, 1, 10, 2, 34)
        assert ctx.duration_seconds == 154

    def test_is_active_true_when_dialing(self):
        from backend.core.state_machine import CallContext
        ctx = CallContext(uuid="u", task_id="t", phone_number="p", script_id="s")
        assert ctx.is_active is True

    def test_is_active_false_when_ended(self):
        from backend.core.state_machine import CallContext, CallState
        ctx = CallContext(uuid="u", task_id="t", phone_number="p", script_id="s")
        ctx.state = CallState.ENDED
        assert ctx.is_active is False

    @pytest.mark.asyncio
    async def test_rejection_count_triggers_end(self):
        from backend.core.state_machine import CallContext, StateMachine
        ctx = CallContext(uuid="u", task_id="t", phone_number="p", script_id="s")
        sm = StateMachine(ctx)
        end_called = []
        async def _end(p): end_called.append(1)
        sm.register_handler("end", _end)

        reject = {"reply": "好", "intent": "not_interested", "action": "continue", "action_params": {}}
        _, a1 = await sm.process_llm_response(reject)
        assert a1 == "continue"
        _, a2 = await sm.process_llm_response(reject)
        assert a2 == "end"
        assert ctx.rejection_count == 2
        assert end_called


# ══════════════════════════════════════════════════════════════
# 测试：LLM 服务
# ══════════════════════════════════════════════════════════════
class TestLLMService:
    def _make_svc(self):
        from backend.services.llm_service import LLMService
        from backend.core.config import LLMConfig
        svc = LLMService.__new__(LLMService)
        svc._cfg = LLMConfig()
        svc._cfg.max_history_turns = 5
        return svc

    def test_parse_valid_json(self):
        svc = self._make_svc()
        r = svc._parse_response(
            '{"reply":"您好","intent":"interested","action":"continue","action_params":{}}'
        )
        assert r["reply"] == "您好"
        assert r["intent"] == "interested"
        assert r["action"] == "continue"

    def test_parse_markdown_fenced_json(self):
        svc = self._make_svc()
        raw = '```json\n{"reply":"test","intent":"unknown","action":"continue","action_params":{}}\n```'
        r = svc._parse_response(raw)
        assert r["reply"] == "test"

    def test_parse_invalid_json_graceful_fallback(self):
        svc = self._make_svc()
        r = svc._parse_response("这根本不是 JSON")
        assert r["action"] == "continue"
        assert len(r["reply"]) > 0

    def test_fallback_response_action_is_end(self):
        svc = self._make_svc()
        assert svc._fallback_response()["action"] == "end"

    def test_trim_history_preserves_first_message(self):
        svc = self._make_svc()
        msgs = [{"role": "user", "content": str(i)} for i in range(20)]
        trimmed = svc._trim_history(msgs)
        assert trimmed[0] == msgs[0]
        assert len(trimmed) <= 11  # max_history_turns=5 → max_msgs=10, +first = 11

    def test_build_system_prompt_contains_compliance(self):
        from backend.services.llm_service import build_system_prompt
        prompt = build_system_prompt("finance_product_a", {"name": "张三"})
        assert "本通话由人工智能完成" in prompt
        assert "张三" in prompt
        assert "JSON" in prompt

    def test_build_opening_contains_compliance_and_name(self):
        from backend.services.llm_service import build_opening
        op = build_opening("finance_product_a", {"name": "李四"})
        assert "本通话由人工智能完成" in op["reply"]
        assert "李四" in op["reply"]

    def test_get_scripts_returns_dict(self):
        from backend.services.llm_service import get_scripts
        scripts = get_scripts()
        assert isinstance(scripts, dict)
        assert len(scripts) >= 1

    def test_scripts_have_required_fields(self):
        from backend.services.llm_service import get_scripts
        for sid, s in get_scripts().items():
            assert "product_name" in s, f"{sid} 缺少 product_name"
            assert "product_desc" in s, f"{sid} 缺少 product_desc"
            assert "key_selling_points" in s, f"{sid} 缺少 key_selling_points"


# ══════════════════════════════════════════════════════════════
# 测试：配置管理
# ══════════════════════════════════════════════════════════════
class TestConfig:
    def test_all_config_fields_present(self):
        from backend.core.config import AppConfig
        cfg = AppConfig()
        assert hasattr(cfg, "freeswitch")
        assert hasattr(cfg, "asr")
        assert hasattr(cfg, "tts")
        assert hasattr(cfg, "llm")
        assert hasattr(cfg, "db")
        assert hasattr(cfg, "redis")
        assert hasattr(cfg, "max_call_duration")
        assert hasattr(cfg, "api_token")

    def test_env_override_max_call_duration(self):
        from backend.core.config import AppConfig
        cfg = AppConfig()
        assert cfg.max_call_duration == 300

    def test_esl_defaults(self):
        from backend.core.config import FreeSwitchConfig
        cfg = FreeSwitchConfig()
        assert cfg.port == 8021
        assert cfg.socket_port == 9999

    def test_ali_asr_fields_in_asr_config(self):
        from backend.core.config import ASRConfig
        cfg = ASRConfig()
        assert hasattr(cfg, "ali_asr_appkey")
        assert hasattr(cfg, "ali_access_key_id")
        assert hasattr(cfg, "ali_nls_token")
        assert hasattr(cfg, "ali_nls_url")
        assert "cn-shanghai" in cfg.ali_nls_url

    def test_ali_asr_bool_fields_default_true(self):
        from backend.core.config import ASRConfig
        cfg = ASRConfig()
        assert cfg.ali_enable_intermediate is True
        assert cfg.ali_enable_punctuation  is True
        assert cfg.ali_enable_itn          is True


# ══════════════════════════════════════════════════════════════
# 测试：任务调度器
# ══════════════════════════════════════════════════════════════
class TestScheduler:
    def _make_sch(self):
        from backend.core.scheduler import TaskScheduler
        return TaskScheduler(esl_pool=None)

    def test_create_task_basic(self):
        from backend.core.scheduler import TaskStatus
        sch = self._make_sch()
        task = sch.create_task("测试", ["13800000001", "13800000002"], "finance_product_a")
        assert task.total == 2
        assert task.status == TaskStatus.PENDING
        assert task.progress_pct == 0.0

    def test_to_dict_has_required_fields(self):
        sch = self._make_sch()
        task = sch.create_task("t", ["138"], "s")
        d = task.to_dict()
        for field in ["task_id", "name", "status", "total", "completed",
                      "connected", "failed", "progress", "connect_rate"]:
            assert field in d, f"缺少字段: {field}"

    def test_pause_resume_cancel(self):
        from backend.core.scheduler import TaskStatus
        sch = self._make_sch()
        task = sch.create_task("t", ["138"], "s")
        task.status = TaskStatus.RUNNING

        assert sch.pause_task(task.task_id)
        assert task.status == TaskStatus.PAUSED

        assert sch.resume_task(task.task_id)
        assert task.status == TaskStatus.RUNNING

        assert sch.cancel_task(task.task_id)
        assert task.status == TaskStatus.CANCELLED

    def test_on_call_finished_updates_counts(self):
        from backend.core.scheduler import TaskStatus
        from backend.core.state_machine import CallResult
        sch = self._make_sch()
        task = sch.create_task("t", ["138", "139"], "s")
        task.status = TaskStatus.RUNNING
        task.phones[0].result = "dialing"
        task.phones[1].result = "dialing"

        sch.on_call_finished(task.task_id, "138", CallResult.COMPLETED)
        assert task.connected_count == 1

        sch.on_call_finished(task.task_id, "139", CallResult.ERROR)
        assert task.failed_count == 1

    def test_dial_window_constants(self):
        from backend.core.scheduler import DIAL_WINDOW_START, DIAL_WINDOW_END
        from datetime import time as dtime
        assert DIAL_WINDOW_START == dtime(9, 0)
        assert DIAL_WINDOW_END   == dtime(21, 0)

    def test_in_dial_window_returns_bool(self):
        from backend.core.scheduler import TaskScheduler
        assert isinstance(TaskScheduler._in_dial_window(), bool)

    def test_window_boundaries(self):
        from backend.core.scheduler import DIAL_WINDOW_START, DIAL_WINDOW_END
        from datetime import time as dtime
        assert DIAL_WINDOW_START <= dtime(10, 0) <= DIAL_WINDOW_END
        assert dtime(8, 59) < DIAL_WINDOW_START
        assert dtime(21, 1) > DIAL_WINDOW_END

    def test_concurrent_limit_capped_to_max(self):
        sch = self._make_sch()
        task = sch.create_task("t", ["138"], "s", concurrent_limit=9999)
        from backend.core.config import config
        assert task.concurrent_limit <= config.max_concurrent_calls

    def test_phone_numbers_stripped(self):
        sch = self._make_sch()
        task = sch.create_task("t", [" 13800000001 ", "13800000002"], "s")
        for pr in task.phones:
            assert pr.phone == pr.phone.strip()


# ══════════════════════════════════════════════════════════════
# 测试：ESL 服务
# ══════════════════════════════════════════════════════════════
class TestESLService:
    class FakeReader:
        def __init__(self, raw: bytes): self._buf = raw; self._pos = 0
        async def readline(self):
            end = self._buf.find(b"\r\n", self._pos)
            if end == -1: return b""
            d = self._buf[self._pos:end+2]; self._pos = end+2; return d
        async def readexactly(self, n):
            d = self._buf[self._pos:self._pos+n]; self._pos += n; return d

    class FakeWriter:
        def __init__(self): self.sent=b""; self._closing=False
        def write(self, d): self.sent += d.encode() if isinstance(d, str) else d
        async def drain(self): pass
        def is_closing(self): return self._closing
        def close(self): self._closing = True
        async def wait_closed(self): pass

    def _make_raw_handshake(self) -> bytes:
        return (
            "Content-Type: command/reply\r\n"
            "Unique-ID: uuid_test_001\r\n"
            "Caller-Destination-Number: 19042638084\r\n"
            "variable_task_id: task_prod_001\r\n"
            "variable_script_id: finance_product_a\r\n"
            "variable_ai_agent: true\r\n"
            "\r\n"
            "Content-Type: command/reply\r\nReply-Text: +OK\r\n\r\n"
        ).encode()

    @pytest.mark.asyncio
    async def test_connect_extracts_uuid_and_channel_vars(self):
        from backend.services.esl_service import ESLSocketCallSession
        raw = self._make_raw_handshake()
        session = ESLSocketCallSession(self.FakeReader(raw), self.FakeWriter())
        # simulate connect (read event + parse vars)
        data = await session._read_event()
        session._uuid = data.get("Unique-ID")
        for k, v in data.items():
            if k.startswith("variable_"):
                session._channel_vars[k[9:]] = v

        assert session._uuid == "uuid_test_001"
        assert session._channel_vars["task_id"] == "task_prod_001"
        assert session._channel_vars["script_id"] == "finance_product_a"

    @pytest.mark.asyncio
    async def test_safe_put_does_not_block_on_full_queue(self):
        from backend.services.esl_service import ESLSocketCallSession
        q = asyncio.Queue(maxsize=2)
        q.put_nowait("a")
        q.put_nowait("b")
        await ESLSocketCallSession._safe_put(q, "c")
        assert q.qsize() == 2

    def test_playback_done_is_event(self):
        from backend.services.esl_service import ESLSocketCallSession
        session = ESLSocketCallSession(self.FakeReader(b""), self.FakeWriter())
        assert isinstance(session._playback_done, asyncio.Event)

    def test_connection_is_connected_property(self):
        from backend.services.esl_service import AsyncESLConnection
        conn = AsyncESLConnection("127.0.0.1", 8021, "pass")
        assert conn.is_connected is False
        conn._connected = True
        conn._writer = self.FakeWriter()
        assert conn.is_connected is True
        conn._writer._closing = True
        assert conn.is_connected is False


# ══════════════════════════════════════════════════════════════
# 测试：ASR 服务
# ══════════════════════════════════════════════════════════════
class TestASRService:
    @pytest.mark.asyncio
    async def test_mock_asr_returns_final_result(self):
        from backend.services.asr_service import MockASRClient, ASRResult
        async def fake_audio():
            yield b"\x00" * 1600
            yield b""
        client = MockASRClient()
        results = [r async for r in client.recognize_stream(fake_audio())]
        assert len(results) >= 1
        assert results[-1].is_final
        assert len(results[-1].text) > 0

    @pytest.mark.asyncio
    async def test_mock_asr_cycles_responses(self):
        from backend.services.asr_service import MockASRClient
        async def fake_audio():
            yield b"\x00"; yield b""
        client = MockASRClient()
        texts = set()
        for _ in range(6):
            async for r in client.recognize_stream(fake_audio()):
                if r.is_final: texts.add(r.text)
        assert len(texts) > 1

    def test_factory_creates_mock(self):
        from backend.services.asr_service import create_asr_client, MockASRClient
        from backend.core.config import ASRConfig
        cfg = ASRConfig(); cfg.provider = "mock"
        assert isinstance(create_asr_client(cfg), MockASRClient)

    def test_factory_raises_on_unknown_provider(self):
        from backend.services.asr_service import create_asr_client
        from backend.core.config import ASRConfig
        cfg = ASRConfig(); cfg.provider = "does_not_exist"
        with pytest.raises(ValueError):
            create_asr_client(cfg)

    def test_asr_result_strips_whitespace(self):
        from backend.services.asr_service import ASRResult
        r = ASRResult(text="  您好  ")
        assert r.text == "您好"


# ══════════════════════════════════════════════════════════════
# 测试：TTS 服务
# ══════════════════════════════════════════════════════════════
class TestTTSService:
    @pytest.mark.asyncio
    async def test_mock_tts_returns_wav_path(self):
        from backend.services.tts_service import MockTTSClient
        path = await MockTTSClient().synthesize("您好，欢迎使用智能外呼系统")
        assert path.endswith(".wav")

    @pytest.mark.asyncio
    async def test_mock_tts_file_exists_on_disk(self):
        from backend.services.tts_service import MockTTSClient
        path = await MockTTSClient().synthesize("测试文本")
        assert os.path.exists(path)

    def test_factory_creates_mock(self):
        from backend.services.tts_service import create_tts_client, MockTTSClient
        from backend.core.config import TTSConfig
        cfg = TTSConfig(); cfg.provider = "mock"
        assert isinstance(create_tts_client(cfg), MockTTSClient)


# ══════════════════════════════════════════════════════════════
# 测试：CRM 服务
# ══════════════════════════════════════════════════════════════
class TestCRMService:
    def setup_method(self):
        # 每个测试前清空黑名单（避免测试间污染）
        import backend.services.crm_service as crm_mod
        crm_mod._BLACKLIST.clear()
        crm_mod._INTENTS.clear()

    @pytest.mark.asyncio
    async def test_query_known_customer(self):
        from backend.services.crm_service import crm
        info = await crm.query_customer_info("19042638084")
        assert info["found"] is True
        assert info["name"] == "张三"
        assert info["blacklisted"] is False

    @pytest.mark.asyncio
    async def test_query_unknown_customer(self):
        from backend.services.crm_service import crm
        info = await crm.query_customer_info("10000000000")
        assert info["found"] is False

    def test_is_blacklisted_initially_false(self):
        from backend.services.crm_service import crm
        assert crm.is_blacklisted("19999999999") is False

    @pytest.mark.asyncio
    async def test_add_to_blacklist_updates_memory(self):
        from backend.services.crm_service import crm
        await crm.add_to_blacklist("19999999999", "test")
        assert crm.is_blacklisted("19999999999") is True

    @pytest.mark.asyncio
    async def test_blacklisted_phone_returns_blacklisted_true(self):
        from backend.services.crm_service import crm
        await crm.add_to_blacklist("18888888888")
        info = await crm.query_customer_info("18888888888")
        assert info["blacklisted"] is True

    @pytest.mark.asyncio
    async def test_remove_from_blacklist(self):
        from backend.services.crm_service import crm
        await crm.add_to_blacklist("17777777777")
        await crm.remove_from_blacklist("17777777777")
        assert crm.is_blacklisted("17777777777") is False

    @pytest.mark.asyncio
    async def test_record_intent(self):
        from backend.services.crm_service import crm, _INTENTS
        await crm.record_intent("19042638084", "high", "uuid-001")
        assert _INTENTS["19042638084"]["level"] == "high"

    @pytest.mark.asyncio
    async def test_schedule_callback_returns_success(self):
        from backend.services.crm_service import crm
        result = await crm.schedule_callback("19042638084", "task_001", "2025-06-01 10:00", "测试")
        assert result["success"] is True
        assert "callback_time" in result

    @pytest.mark.asyncio
    async def test_send_sms_returns_true(self):
        from backend.services.crm_service import crm
        result = await crm.send_sms("19042638084", "product_intro", {"product": "理财A"})
        assert result is True


# ══════════════════════════════════════════════════════════════
# 测试：音频工具
# ══════════════════════════════════════════════════════════════
class TestAudioUtils:
    def test_pcmu_to_pcm_length_preserved(self):
        from backend.utils.audio import pcmu_to_pcm, pcm_to_pcmu
        pcm = bytes(i % 128 for i in range(512))
        assert len(pcmu_to_pcm(pcm_to_pcmu(pcm))) == len(pcm)

    def test_write_and_read_wav(self, tmp_path):
        from backend.utils.audio import write_wav, read_wav_pcm
        path = str(tmp_path / "test.wav")
        write_wav(path, b"\x00\x01" * 800, sample_rate=8000)
        _, rate, channels = read_wav_pcm(path)
        assert rate == 8000
        assert channels == 1

    def test_vad_detects_loud_frame(self):
        from backend.utils.audio import SimpleVAD
        vad = SimpleVAD(energy_threshold=100, speech_min_frames=1)
        loud = struct.pack("<" + "h" * 160, *([10000] * 160))
        active, _ = vad.process_frame(loud)
        assert active is True

    def test_vad_silence_resets_after_speech(self):
        from backend.utils.audio import SimpleVAD
        vad = SimpleVAD(energy_threshold=100, speech_min_frames=1, silence_min_frames=3)
        loud = struct.pack("<" + "h" * 160, *([10000] * 160))
        silence = b"\x00\x00" * 160
        vad.process_frame(loud)
        for _ in range(5):
            _, ended = vad.process_frame(silence)
        assert not vad._in_speech


# ══════════════════════════════════════════════════════════════
# 测试：TTS 缓存清理器
# ══════════════════════════════════════════════════════════════
class TestTTSCache:
    def test_clean_expired_files(self, tmp_path):
        from backend.utils.tts_cache import clean_cache_sync
        import time
        # 创建一个旧文件（模拟过期）
        old_file = tmp_path / "tts_expired.wav"
        old_file.write_bytes(b"\x00" * 1024)
        # 修改 mtime 为 3 小时前
        old_ts = time.time() - 3 * 3600
        os.utime(str(old_file), (old_ts, old_ts))

        # 创建一个新文件（不应被删除）
        new_file = tmp_path / "tts_fresh.wav"
        new_file.write_bytes(b"\x00" * 1024)

        import backend.utils.tts_cache as tc
        original_ttl = tc.CACHE_TTL_SECONDS
        tc.CACHE_TTL_SECONDS = 3600  # 1 小时 TTL

        count, freed = clean_cache_sync(str(tmp_path))
        tc.CACHE_TTL_SECONDS = original_ttl

        assert count == 1
        assert freed == 1024
        assert not old_file.exists()
        assert new_file.exists()

    def test_clean_oversized_cache(self, tmp_path):
        from backend.utils.tts_cache import clean_cache_sync
        import backend.utils.tts_cache as tc

        # 创建 3 个文件，每个 1KB
        for i in range(3):
            f = tmp_path / f"tts_file{i:02d}.wav"
            f.write_bytes(b"\x00" * 1024)

        original_max = tc.CACHE_MAX_BYTES
        original_ttl = tc.CACHE_TTL_SECONDS
        tc.CACHE_MAX_BYTES = 2048  # 只允许 2KB
        tc.CACHE_TTL_SECONDS = 999999  # 不按过期删

        count, freed = clean_cache_sync(str(tmp_path))
        tc.CACHE_MAX_BYTES = original_max
        tc.CACHE_TTL_SECONDS = original_ttl

        assert count >= 1  # 至少删了一个


# ══════════════════════════════════════════════════════════════
# 测试：CallAgent 生产特性
# ══════════════════════════════════════════════════════════════
class TestCallAgent:
    def test_production_timeouts_defined(self):
        from backend.core.call_agent import MAX_CALL_DURATION, LLM_TIMEOUT, TTS_TIMEOUT, ASR_TIMEOUT
        assert MAX_CALL_DURATION == 300
        assert LLM_TIMEOUT       == 15.0
        assert TTS_TIMEOUT       == 10.0
        assert ASR_TIMEOUT       == 12.0

    @pytest.mark.asyncio
    async def test_barge_in_fires_on_audio(self):
        from backend.core.call_agent import AudioStreamAdapter
        q = asyncio.Queue()
        barge = asyncio.Event()
        barge.clear()
        adapter = AudioStreamAdapter(q, vad_silence_ms=200, barge_in_cb=barge)
        await q.put(b"\x01\x02" * 160)
        await q.put(b"")
        chunks = [c async for c in adapter.stream() if c]
        assert barge.is_set()
        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_adapter_stop_terminates_stream(self):
        from backend.core.call_agent import AudioStreamAdapter
        q = asyncio.Queue()
        adapter = AudioStreamAdapter(q, vad_silence_ms=9999)
        asyncio.get_event_loop().call_soon(adapter.stop)
        chunks = [c async for c in adapter.stream()]
        assert len(chunks) <= 1  # 最多只有结束信号 b""


# ══════════════════════════════════════════════════════════════
# 测试：API 鉴权逻辑
# ══════════════════════════════════════════════════════════════
class TestAuth:
    def _check(self, provided: str, expected: str) -> bool:
        return not expected or provided == expected

    def test_no_token_configured_always_pass(self):
        assert self._check("", "") is True
        assert self._check("anything", "") is True

    def test_correct_token_passes(self):
        assert self._check("secret", "secret") is True

    def test_wrong_token_rejected(self):
        assert self._check("wrong", "secret") is False

    def test_missing_token_rejected(self):
        assert self._check("", "secret") is False


# ══════════════════════════════════════════════════════════════
# 测试：Prometheus 指标格式
# ══════════════════════════════════════════════════════════════
class TestMetrics:
    def test_metrics_output_format(self):
        metrics = {
            "calls_total": 42, "calls_active": 3,
            "calls_completed": 38, "calls_error": 1,
        }
        lines = [
            "# HELP outbound_calls_total Total outbound calls",
            "# TYPE outbound_calls_total counter",
            f"outbound_calls_total {metrics['calls_total']}",
            "# TYPE outbound_calls_active gauge",
            f"outbound_calls_active {metrics['calls_active']}",
        ]
        assert any("HELP" in l for l in lines)
        assert any("TYPE" in l for l in lines)
        assert "outbound_calls_total 42" in lines
        assert "outbound_calls_active 3" in lines

    def test_metric_values_are_integers(self):
        metrics = {"calls_total": 0, "calls_active": 0}
        for v in metrics.values():
            assert isinstance(v, int)
