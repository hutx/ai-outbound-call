import asyncio
import os
import struct
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient


def _make_script():
    return SimpleNamespace(
        main_script="【推介产品】稳健理财\n【产品介绍】适合保守客户",
        opening_pause=2000,
        opening_script="您好，这里是智能客服。",
        opening_barge_in=False,
        closing_barge_in=False,
        conversation_barge_in=True,
        barge_in_protect_start=3,
        barge_in_protect_end=2,
    )


class TestStateMachine:
    def test_initial_state(self):
        from backend.core.state_machine import CallContext, CallState

        ctx = CallContext(uuid="u", task_id="t", phone_number="138", script_id="s")
        assert ctx.state == CallState.DIALING

    def test_intent_update_invalid_falls_back_to_unknown(self):
        from backend.core.state_machine import CallContext, StateMachine, CallIntent

        ctx = CallContext(uuid="u", task_id="t", phone_number="138", script_id="s")
        sm = StateMachine(ctx)
        sm.update_intent("invalid")
        assert ctx.intent == CallIntent.UNKNOWN

    @pytest.mark.asyncio
    async def test_rejection_count_triggers_end(self):
        from backend.core.state_machine import CallContext, StateMachine

        ctx = CallContext(uuid="u", task_id="t", phone_number="138", script_id="s")
        sm = StateMachine(ctx)
        calls = []

        async def handle_end(params):
            calls.append(params)

        sm.register_handler("end", handle_end)
        response = {
            "reply": "好的",
            "intent": "not_interested",
            "action": "continue",
            "action_params": {},
        }
        _, action_1 = await sm.process_llm_response(response)
        _, action_2 = await sm.process_llm_response(response)

        assert action_1 == "continue"
        assert action_2 == "end"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_action_aliases_are_normalized(self):
        from backend.core.state_machine import CallContext, StateMachine

        ctx = CallContext(uuid="u", task_id="t", phone_number="138", script_id="s")
        sm = StateMachine(ctx)
        seen = []

        async def handle_transfer(params):
            seen.append("transfer")

        async def handle_callback(params):
            seen.append("callback")

        sm.register_handler("transfer", handle_transfer)
        sm.register_handler("callback", handle_callback)

        _, action_1 = await sm.process_llm_response(
            {"reply": "ok", "intent": "request_human", "action": "human", "action_params": {}}
        )
        _, action_2 = await sm.process_llm_response(
            {
                "reply": "ok",
                "intent": "callback",
                "action": "schedule_callback",
                "action_params": {},
            }
        )

        assert action_1 == "transfer"
        assert action_2 == "callback"
        assert seen == ["transfer", "callback"]


class TestPromptBuilders:
    @pytest.mark.asyncio
    async def test_get_system_prompt_includes_customer_info_and_actions(self, monkeypatch):
        from backend.services import async_script_utils as utils

        async def fake_get_script(script_id):
            return _make_script()

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        prompt = await utils.get_system_prompt_for_call(
            "finance_product_a",
            {"name": "张三", "note": "高净值客户", "risk_level": "stable", "found": True},
        )

        assert "张三" in prompt
        assert "高净值客户" in prompt
        assert "callback" in prompt
        assert "blacklist" in prompt

    @pytest.mark.asyncio
    async def test_build_system_prompt_from_db_matches_current_contract(self, monkeypatch):
        from backend.services import script_service as svc

        async def fake_get_script(script_id):
            return _make_script()

        monkeypatch.setattr(svc.script_service, "get_script", fake_get_script)
        prompt = await svc.build_system_prompt_from_db(
            "finance_product_a",
            {"name": "李四", "note": "偏保守", "risk_level": "conservative", "found": True},
        )

        assert "李四" in prompt
        assert "callback_time" in prompt
        assert "blacklist" in prompt

    @pytest.mark.asyncio
    async def test_get_opening_uses_script_config(self, monkeypatch):
        from backend.services import async_script_utils as utils

        async def fake_get_script(script_id):
            return _make_script()

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        opening = await utils.get_opening_for_call("finance_product_a", {"name": "张三"})

        assert opening["reply"] == "您好，这里是智能客服。"
        assert opening["action"] == "continue"
        assert opening["barge_in_enabled"] is False


class TestLLMService:
    def _make_service(self):
        from backend.services.llm_service import LLMService

        return LLMService.__new__(LLMService)

    def test_parse_markdown_fenced_json(self):
        svc = self._make_service()
        data = svc._parse_response(
            '```json\n{"reply":"test","intent":"unknown","action":"continue","action_params":{}}\n```'
        )
        assert data["reply"] == "test"
        assert data["action"] == "continue"

    def test_parse_invalid_json_falls_back_to_text(self):
        svc = self._make_service()
        data = svc._parse_response("不是 JSON")
        assert data["action"] == "continue"
        assert data["reply"] == "不是 JSON"

    def test_trim_history_uses_current_global_config(self, monkeypatch):
        from backend.services.llm_service import config as llm_config

        svc = self._make_service()
        monkeypatch.setattr(llm_config.llm, "max_history_turns", 2)
        messages = [{"role": "user", "content": str(i)} for i in range(8)]
        trimmed = svc._trim_history(messages)

        assert trimmed[0] == messages[0]
        assert len(trimmed) == 4

    def test_resolve_provider_prefers_dashscope_compatible_for_qwen(self):
        from backend.core.config import LLMConfig
        from backend.services.llm_service import LLMService

        cfg = LLMConfig()
        cfg.provider = "auto"
        cfg.model = "qwen3.5-flash"
        cfg.anthropic_base_url = "https://dashscope.aliyuncs.com/apps/anthropic"

        assert LLMService._resolve_provider(cfg) == "dashscope_compatible"

    def test_resolve_provider_honors_explicit_provider(self):
        from backend.core.config import LLMConfig
        from backend.services.llm_service import LLMService

        cfg = LLMConfig()
        cfg.provider = "anthropic_compatible"
        assert LLMService._resolve_provider(cfg) == "anthropic_compatible"

    def test_trim_history_uses_instance_cfg(self):
        from backend.core.config import LLMConfig

        svc = self._make_service()
        svc._cfg = LLMConfig()
        svc._cfg.max_history_turns = 1
        messages = [{"role": "user", "content": str(i)} for i in range(6)]
        trimmed = svc._trim_history(messages)

        assert trimmed[0] == messages[0]
        assert len(trimmed) == 2


class TestConfigAndAuth:
    def test_validate_runtime_requires_token_in_non_debug(self):
        from backend.core.config import AppConfig

        cfg = AppConfig()
        cfg.debug = False
        cfg.api_token = ""
        with pytest.raises(ValueError):
            cfg.validate_runtime()

    def test_require_auth_allows_anonymous_in_debug_without_token(self, monkeypatch):
        from backend.core import auth

        monkeypatch.setattr(auth.config, "debug", True)
        monkeypatch.setattr(auth.config, "api_token", "")
        result = auth.require_auth(None)
        assert result["user"] == "anonymous"

    def test_require_auth_rejects_invalid_token(self, monkeypatch):
        from backend.core import auth

        monkeypatch.setattr(auth.config, "debug", False)
        monkeypatch.setattr(auth.config, "api_token", "secret")
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong")
        with pytest.raises(HTTPException) as exc:
            auth.require_auth(creds)
        assert exc.value.status_code == 401


class TestScheduler:
    def test_create_task_keeps_caller_id(self):
        from backend.core.scheduler import TaskScheduler

        scheduler = TaskScheduler(esl_pool=None)
        task = scheduler.create_task(
            "测试任务",
            ["13800000001", "13800000002"],
            "finance_product_a",
            caller_id="4008009000",
        )
        assert task.caller_id == "4008009000"

    def test_completed_excludes_dialing(self):
        from backend.core.scheduler import TaskScheduler

        scheduler = TaskScheduler(esl_pool=None)
        task = scheduler.create_task("t", ["138", "139", "137"], "s")
        task.phones[0].result = "dialing"
        task.phones[1].result = "completed"
        task.phones[2].result = "error"
        assert task.completed == 2

    @pytest.mark.asyncio
    async def test_start_task_refuses_duplicate_runner(self, monkeypatch):
        from backend.core.scheduler import TaskScheduler

        scheduler = TaskScheduler(esl_pool=None)
        task = scheduler.create_task("t", ["138"], "s")
        gate = asyncio.Event()

        async def fake_run(_task):
            await gate.wait()

        monkeypatch.setattr(scheduler, "_run", fake_run)

        assert await scheduler.start_task(task.task_id) is True
        assert await scheduler.start_task(task.task_id) is False
        assert scheduler.pause_task(task.task_id) is True
        assert scheduler.resume_task(task.task_id) is True
        assert await scheduler.start_task(task.task_id) is False

        gate.set()
        await asyncio.sleep(0)

    def test_on_call_finished_updates_counts(self):
        from backend.core.scheduler import TaskScheduler, TaskStatus
        from backend.core.state_machine import CallResult

        scheduler = TaskScheduler(esl_pool=None)
        task = scheduler.create_task("t", ["138", "139"], "s")
        task.status = TaskStatus.RUNNING
        task.phones[0].result = "dialing"
        task.phones[1].result = "dialing"

        scheduler.on_call_finished(task.task_id, "138", CallResult.COMPLETED)
        scheduler.on_call_finished(task.task_id, "139", CallResult.ERROR)

        assert task.connected_count == 1
        assert task.failed_count == 1


class TestASRAndTTS:
    @pytest.mark.asyncio
    async def test_mock_asr_returns_final_result(self):
        from backend.services.asr_service import MockASRClient

        async def fake_audio():
            yield b"\x00" * 1600
            yield b""

        client = MockASRClient()
        results = [item async for item in client.recognize_stream(fake_audio())]
        assert results[-1].is_final is True
        assert results[-1].text

    def test_create_asr_client_returns_mock(self):
        from backend.core.config import ASRConfig
        from backend.services.asr_service import MockASRClient, create_asr_client

        cfg = ASRConfig()
        cfg.provider = "mock"
        assert isinstance(create_asr_client(cfg), MockASRClient)

    @pytest.mark.asyncio
    async def test_mock_tts_stream_yields_pcm_chunks(self):
        from backend.services.tts_service import MockTTSClient

        client = MockTTSClient()
        chunks = [chunk async for chunk in client.synthesize_stream("您好")]
        assert len(chunks) == 3
        assert all(isinstance(chunk, bytes) and chunk for chunk in chunks)

    def test_create_tts_unknown_provider_falls_back_to_edge(self):
        from backend.core.config import TTSConfig
        from backend.services.tts_service import EdgeTTSClient, create_tts_client

        cfg = TTSConfig()
        cfg.provider = "unknown-provider"
        assert isinstance(create_tts_client(cfg), EdgeTTSClient)


class TestCRMService:
    def setup_method(self):
        import backend.services.crm_service as crm_module

        crm_module._BLACKLIST.clear()
        crm_module._INTENTS.clear()

    @pytest.mark.asyncio
    async def test_query_known_customer(self):
        from backend.services.crm_service import crm

        info = await crm.query_customer_info("19042638084")
        assert info["found"] is True
        assert info["name"] == "张三"

    @pytest.mark.asyncio
    async def test_blacklist_lifecycle(self):
        from backend.services.crm_service import crm

        await crm.add_to_blacklist("18888888888", "test")
        assert crm.is_blacklisted("18888888888") is True

        info = await crm.query_customer_info("18888888888")
        assert info["blacklisted"] is True

        await crm.remove_from_blacklist("18888888888")
        assert crm.is_blacklisted("18888888888") is False

    @pytest.mark.asyncio
    async def test_schedule_callback_returns_success(self):
        from backend.services.crm_service import crm

        result = await crm.schedule_callback(
            "19042638084", "task_001", "2026-04-09 10:00", "测试"
        )
        assert result["success"] is True
        assert result["callback_time"] == "2026-04-09 10:00"


class TestDBHelpers:
    def test_build_call_record_values_keeps_recent_messages(self):
        from backend.core.state_machine import CallContext
        from backend.utils.db import _build_call_record_values

        ctx = CallContext(
            uuid="u1",
            task_id="t1",
            phone_number="13800000000",
            script_id="finance_product_a",
        )
        ctx.messages = [{"role": "user", "content": str(i)} for i in range(40)]
        values = _build_call_record_values(ctx)

        assert values["uuid"] == "u1"
        assert len(values["messages"]) == 30
        assert values["messages"][0]["content"] == "10"

    def test_build_call_record_upsert_stmt_for_sqlite_contains_on_conflict(self):
        from backend.utils.db import _build_call_record_upsert_stmt

        stmt = _build_call_record_upsert_stmt({"uuid": "u1", "task_id": "t1"}, "sqlite")
        sql = str(stmt)

        assert "ON CONFLICT" in sql
        assert "uuid" in sql


class TestESLService:
    class FakeReader:
        def __init__(self, raw: bytes):
            self._buf = raw
            self._pos = 0

        async def readline(self):
            end = self._buf.find(b"\r\n", self._pos)
            if end == -1:
                return b""
            data = self._buf[self._pos : end + 2]
            self._pos = end + 2
            return data

        async def readexactly(self, n):
            data = self._buf[self._pos : self._pos + n]
            self._pos += n
            return data

    class FakeWriter:
        def __init__(self):
            self.sent = b""
            self._closing = False

        def write(self, data):
            self.sent += data.encode() if isinstance(data, str) else data

        async def drain(self):
            return None

        def is_closing(self):
            return self._closing

        def close(self):
            self._closing = True

        async def wait_closed(self):
            return None

    @pytest.mark.asyncio
    async def test_connect_extracts_uuid_and_channel_vars(self):
        from backend.services.esl_service import ESLSocketCallSession

        raw = (
            "Unique-ID: uuid_test_001\r\n"
            "Caller-Destination-Number: 19042638084\r\n"
            "variable_task_id: task_prod_001\r\n"
            "variable_script_id: finance_product_a\r\n"
            "\r\n"
            "Content-Type: command/reply\r\n"
            "Reply-Text: +OK\r\n"
            "\r\n"
            "Content-Type: command/reply\r\n"
            "Reply-Text: +OK\r\n"
            "\r\n"
        ).encode()
        session = ESLSocketCallSession(self.FakeReader(raw), self.FakeWriter())
        data = await session.connect()

        assert session.uuid == "uuid_test_001"
        assert data["Caller-Destination-Number"] == "19042638084"
        assert session.channel_vars["task_id"] == "task_prod_001"
        assert session.channel_vars["script_id"] == "finance_product_a"

    @pytest.mark.asyncio
    async def test_safe_put_does_not_grow_full_queue(self):
        from backend.services.esl_service import ESLSocketCallSession

        queue = asyncio.Queue(maxsize=2)
        queue.put_nowait("a")
        queue.put_nowait("b")
        await ESLSocketCallSession._safe_put(queue, "c")
        assert queue.qsize() == 2


class TestAudioUtils:
    def test_write_and_read_wav(self, tmp_path):
        from backend.utils.audio import read_wav_pcm, write_wav

        path = str(tmp_path / "test.wav")
        write_wav(path, b"\x00\x01" * 800, sample_rate=8000)
        _, rate, channels = read_wav_pcm(path)
        assert rate == 8000
        assert channels == 1

    def test_vad_detects_speech_end(self):
        from backend.utils.audio import SimpleVAD

        vad = SimpleVAD(energy_threshold=100, speech_min_frames=1, silence_min_frames=3)
        loud = struct.pack("<" + "h" * 160, *([10000] * 160))
        silence = b"\x00\x00" * 160
        vad.process_frame(loud)
        ended = False
        for _ in range(3):
            _, ended = vad.process_frame(silence)
        assert ended is True

    def test_vad_rms_threshold_is_pure_python(self):
        from backend.utils.audio import SimpleVAD

        vad = SimpleVAD(energy_threshold=50, speech_min_frames=1)
        quiet = struct.pack("<" + "h" * 160, *([10] * 160))
        loud = struct.pack("<" + "h" * 160, *([5000] * 160))
        assert vad.is_speech_frame(quiet) is False
        assert vad.is_speech_frame(loud) is True


class TestTTSCache:
    def test_clean_expired_files(self, tmp_path):
        import time
        import backend.utils.tts_cache as cache_module
        from backend.utils.tts_cache import clean_cache_sync

        expired = tmp_path / "tts_expired.wav"
        expired.write_bytes(b"\x00" * 1024)
        old_ts = time.time() - 3 * 3600
        os.utime(str(expired), (old_ts, old_ts))

        fresh = tmp_path / "tts_fresh.wav"
        fresh.write_bytes(b"\x00" * 512)

        original_ttl = cache_module.CACHE_TTL_SECONDS
        cache_module.CACHE_TTL_SECONDS = 3600
        try:
            count, freed = clean_cache_sync(str(tmp_path))
        finally:
            cache_module.CACHE_TTL_SECONDS = original_ttl

        assert count == 1
        assert freed == 1024
        assert not expired.exists()
        assert fresh.exists()


class TestCallAgent:
    class DummySession:
        def __init__(self):
            self._connected = True
            self.channel_vars = {}

        async def connect(self):
            return {}

        async def set_variable(self, *args, **kwargs):
            return None

        async def read_events(self):
            await asyncio.sleep(0)

        async def start_audio_capture(self):
            return asyncio.Queue()

        async def play_stream(self, audio_chunks, text="", timeout=60.0):
            async for _ in audio_chunks:
                pass

        async def transfer_to_human(self, ext="8001"):
            return None

        async def hangup(self):
            self._connected = False

    class DummyASR:
        async def recognize_stream(self, gen):
            if False:
                yield None

    class DummyTTS:
        async def synthesize_stream(self, text):
            yield b"pcm"

    class DummyLLM:
        pass

    def _make_agent(self):
        from backend.core.call_agent import CallAgent
        from backend.core.state_machine import CallContext

        ctx = CallContext(
            uuid="u1",
            task_id="t1",
            phone_number="13800000000",
            script_id="finance_product_a",
        )
        return CallAgent(
            self.DummySession(),
            ctx,
            asr=self.DummyASR(),
            tts=self.DummyTTS(),
            llm=self.DummyLLM(),
        )

    def test_production_timeouts_defined(self):
        from backend.core.call_agent import ASR_TIMEOUT, LLM_TIMEOUT, MAX_CALL_DURATION, TTS_TIMEOUT

        assert MAX_CALL_DURATION == 300
        assert LLM_TIMEOUT == 30.0
        assert TTS_TIMEOUT == 10.0
        assert ASR_TIMEOUT == 12.0

    @pytest.mark.asyncio
    async def test_barge_in_fires_on_audio(self):
        from backend.core.call_agent import AudioStreamAdapter

        queue = asyncio.Queue()
        barge_in = asyncio.Event()
        adapter = AudioStreamAdapter(
            queue,
            vad_silence_ms=200,
            barge_in_cb=barge_in,
            protect_start_ms=0,
            protect_end_ms=0,
        )
        await queue.put(b"\x01\x02" * 160)
        await queue.put(b"")
        chunks = [chunk async for chunk in adapter.stream() if chunk]
        stats = adapter.audio_stats()

        assert barge_in.is_set()
        assert chunks
        assert stats["speech_detected"] is True
        assert stats["max_rms"] > 0

    @pytest.mark.asyncio
    async def test_schedule_callback_handler_updates_state(self, monkeypatch):
        from backend.core.state_machine import CallResult, CallState
        from backend.services import async_script_utils as script_utils
        from backend.core.call_agent import crm

        async def fake_get_barge(script_id, speech_type="conversation"):
            return {
                "barge_in_enabled": True,
                "protect_start_sec": 0,
                "protect_end_sec": 0,
            }

        callback_calls = []

        async def fake_schedule(phone, task_id="", callback_time=None, note=""):
            callback_calls.append((phone, task_id, callback_time, note))
            return {"success": True, "callback_time": callback_time or "finance_product_a"}

        monkeypatch.setattr(script_utils, "get_barge_in_config", fake_get_barge)
        monkeypatch.setattr(crm, "schedule_callback", fake_schedule)

        agent = self._make_agent()
        await agent._do_schedule_callback(
            {"callback_time": "2026-04-09 10:00", "note": "稍后联系"}
        )

        assert callback_calls == [("13800000000", "t1", "2026-04-09 10:00", "稍后联系")]
        assert agent.ctx.state == CallState.ENDING
        assert agent.ctx.result == CallResult.COMPLETED

    @pytest.mark.asyncio
    async def test_blacklist_handler_updates_result(self, monkeypatch):
        from backend.core.state_machine import CallResult, CallState
        from backend.core.call_agent import crm

        blacklist_calls = []

        async def fake_blacklist(phone, reason=""):
            blacklist_calls.append((phone, reason))
            return True

        monkeypatch.setattr(crm, "add_to_blacklist", fake_blacklist)
        agent = self._make_agent()
        await agent._do_blacklist({"reason": "勿扰"})

        assert blacklist_calls == [("13800000000", "勿扰")]
        assert agent.ctx.state == CallState.ENDING
        assert agent.ctx.result == CallResult.BLACKLISTED


class TestMockTestCallSession:
    @pytest.mark.asyncio
    async def test_play_stream_waits_for_frontend_playback_ack(self):
        from backend.api.test_call_support import MockESLCallSession

        class FakeWebSocket:
            def __init__(self):
                self.json_messages = []
                self.binary_messages = []

            async def send_json(self, data):
                self.json_messages.append(data)

            async def send_bytes(self, data):
                self.binary_messages.append(data)

        async def audio_chunks():
            yield b"\x01\x00" * 800

        session = MockESLCallSession(
            uuid="call-1",
            channel_vars={},
            asr_client=object(),
            tts_client=object(),
        )
        session.ws_connection = FakeWebSocket()
        session._ws_ready.set()

        task = asyncio.create_task(session.play_stream(audio_chunks(), text="您好", timeout=5.0))
        await asyncio.sleep(0.05)

        assert not task.done()
        assert session.ws_connection.json_messages[0]["type"] == "ai_response"

        session.notify_playback_finished()
        await asyncio.wait_for(task, timeout=1.0)

        assert task.done()
        assert any(
            message.get("type") == "streaming_audio_done"
            for message in session.ws_connection.json_messages
        )


class TestESLOriginateRouting:
    def test_build_originate_target_for_internal_extension(self):
        from backend.services.esl_service import AsyncESLConnection

        endpoint, target_type, dest = AsyncESLConnection._build_originate_target(
            phone="1001",
            gateway="carrier_trunk",
            internal_domain="$${local_ip_v4}",
        )

        assert endpoint == "user/1001@$${local_ip_v4}"
        assert target_type == "internal_extension"
        assert dest == "1001"

    def test_build_originate_target_for_mobile_phone(self):
        from backend.services.esl_service import AsyncESLConnection

        endpoint, target_type, dest = AsyncESLConnection._build_originate_target(
            phone="13800000000",
            gateway="carrier_trunk",
            internal_domain="$${local_ip_v4}",
        )

        assert endpoint == "sofia/gateway/carrier_trunk/9777613800000000"
        assert target_type == "pstn"
        assert dest == "9777613800000000"


def _make_test_client(router):
    from backend.core.auth import require_auth

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: {"user": "test"}
    return TestClient(app)


class TestOperationsAPI:
    def test_create_task_route_passes_caller_id_and_increments_metrics(self):
        from backend.api.operations_api import OperationsAPI

        metrics = {"tasks_created": 0}

        class FakeScheduler:
            def __init__(self):
                self.created = None
                self.started = None

            def create_task(self, **kwargs):
                self.created = kwargs
                return SimpleNamespace(
                    task_id="task_001",
                    name=kwargs["name"],
                    status=SimpleNamespace(name="PENDING"),
                    total=len(kwargs["phone_numbers"]),
                )

            async def start_task(self, task_id):
                self.started = task_id
                return True

        scheduler = FakeScheduler()
        api = OperationsAPI(scheduler_getter=lambda: scheduler, metrics=metrics)
        client = _make_test_client(api.router)

        response = client.post(
            "/api/tasks",
            json={
                "name": "批量任务",
                "phone_numbers": ["13800000000", "13900000000"],
                "script_id": "finance_product_a",
                "caller_id": "4008009000",
            },
        )

        assert response.status_code == 200
        assert response.json()["task_id"] == "task_001"
        assert scheduler.created["caller_id"] == "4008009000"
        assert scheduler.started == "task_001"
        assert metrics["tasks_created"] == 1


class TestMonitorAPI:
    def test_metrics_route_returns_prometheus_text(self):
        from backend.api.monitor_api import MonitorAPI

        metrics = {
            "calls_total": 12,
            "calls_active": 2,
            "calls_completed": 9,
            "calls_transferred": 1,
            "calls_error": 0,
            "tasks_created": 3,
        }

        scheduler = SimpleNamespace(list_tasks=lambda: [{"status": "RUNNING"}, {"status": "PAUSED"}])
        pool = SimpleNamespace(_conns=[SimpleNamespace(is_connected=True)])
        api = MonitorAPI(
            config=SimpleNamespace(max_concurrent_calls=50, api_token=""),
            metrics=metrics,
            scheduler_getter=lambda: scheduler,
            esl_pool_getter=lambda: pool,
            start_time=0.0,
        )
        client = _make_test_client(api.router)

        response = client.get("/metrics")

        assert response.status_code == 200
        assert "outbound_calls_total 12" in response.text
        assert "outbound_calls_active 2" in response.text

    def test_health_route_reports_ok_when_esl_connected(self):
        from backend.api.monitor_api import MonitorAPI

        api = MonitorAPI(
            config=SimpleNamespace(max_concurrent_calls=50, api_token=""),
            metrics={"calls_active": 1, "calls_total": 0, "calls_completed": 0, "calls_transferred": 0, "calls_error": 0, "tasks_created": 0},
            scheduler_getter=lambda: None,
            esl_pool_getter=lambda: SimpleNamespace(_conns=[SimpleNamespace(is_connected=True)]),
            start_time=0.0,
        )
        client = _make_test_client(api.router)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestTestCallAPI:
    def test_get_test_call_messages_route_reads_active_call(self):
        from backend.api.test_call_api import TestCallAPI

        active_calls = {
            "call_1": SimpleNamespace(
                ctx=SimpleNamespace(
                    messages=[{"role": "assistant", "content": "您好"}],
                    state=SimpleNamespace(name="CONNECTED"),
                    intent=SimpleNamespace(value="unknown"),
                    result=SimpleNamespace(value="completed"),
                )
            )
        }
        api = TestCallAPI(
            config=SimpleNamespace(api_token=""),
            active_calls=active_calls,
            metrics={},
            broadcast_stats=lambda: None,
            asr_getter=lambda: object(),
            tts_getter=lambda: object(),
            llm_getter=lambda: object(),
        )
        client = _make_test_client(api.router)

        response = client.get("/api/test-call/call_1/messages")

        assert response.status_code == 200
        data = response.json()
        assert data["call_id"] == "call_1"
        assert data["messages"][0]["content"] == "您好"

    def test_synthesize_tts_route_returns_wav_file(self, tmp_path):
        from backend.api.test_call_api import TestCallAPI

        wav_path = tmp_path / "tts.wav"
        wav_path.write_bytes(b"RIFFmockwav")

        class FakeTTS:
            async def synthesize(self, text: str) -> str:
                return str(wav_path)

        api = TestCallAPI(
            config=SimpleNamespace(api_token=""),
            active_calls={},
            metrics={},
            broadcast_stats=lambda: None,
            asr_getter=lambda: object(),
            tts_getter=lambda: FakeTTS(),
            llm_getter=lambda: object(),
        )
        client = _make_test_client(api.router)

        response = client.post("/api/tts/synthesize", json={"text": "测试语音"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")

    def test_start_test_call_route_registers_active_call(self, monkeypatch):
        import backend.api.test_call_api as test_call_module
        from backend.api.test_call_api import TestCallAPI

        active_calls = {}
        metrics = {"calls_total": 0, "calls_active": 0}
        broadcast_calls = []
        created_agents = []
        scheduled = []

        async def fake_broadcast():
            broadcast_calls.append("broadcast")

        class FakeCallAgent:
            def __init__(self, session, context, asr, tts, llm):
                self.session = session
                self.ctx = context
                created_agents.append(self)

        async def fake_run_test_call_with_agent(*args, **kwargs):
            return None

        def fake_create_task(coro):
            scheduled.append(True)
            coro.close()
            return SimpleNamespace()

        monkeypatch.setattr(test_call_module, "CallAgent", FakeCallAgent)
        monkeypatch.setattr(
            test_call_module, "run_test_call_with_agent", fake_run_test_call_with_agent
        )
        monkeypatch.setattr(test_call_module.asyncio, "create_task", fake_create_task)

        api = TestCallAPI(
            config=SimpleNamespace(api_token=""),
            active_calls=active_calls,
            metrics=metrics,
            broadcast_stats=fake_broadcast,
            asr_getter=lambda: object(),
            tts_getter=lambda: object(),
            llm_getter=lambda: object(),
        )
        client = _make_test_client(api.router)

        response = client.post(
            "/api/test-call/start",
            json={"phone_number": "13800000000", "script_id": "finance_product_a", "customer_info": {}},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["call_id"] in active_calls
        assert metrics["calls_total"] == 1
        assert metrics["calls_active"] == 1
        assert len(created_agents) == 1
        assert scheduled == [True]
        assert broadcast_calls == ["broadcast"]
