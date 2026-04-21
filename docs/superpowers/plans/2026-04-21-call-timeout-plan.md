# 通话时长限制功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 CallAgent 中实现通话时长限制，默认 5 分钟，到达前 20 秒触发超时结束流程（打断 TTS + 播放超时结束语 + 挂断）。

**Architecture:** 电话接通后启动 `asyncio.Event` 看门狗任务，倒计时到 `(总时长 - 缓冲)` 时设置 `_call_timeout` 事件；`_conversation_loop` 每次循环入口处检查该事件，触发则打断 TTS 并播放超时结束语。

**Tech Stack:** Python asyncio, FreeSWITCH mod_forkzstream, Qwen ASR, 百炼 TTS

---

### Task 1: 配置层 — 新增通话时长相关环境变量

**Files:**
- Modify: `backend/core/config.py`
- Test: `tests/test_call_timeout.py`

- [ ] **Step 1: 编写测试**

```python
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
        import importlib
        from backend.core import config as config_module
        with patch.dict("os.environ", {
            "MAX_CALL_DURATION_SECONDS": "600",
            "CALL_END_BUFFER_SECONDS": "30",
        }, clear=True):
            # 重新实例化以测试 __post_init__
            cfg = config_module.AppConfig()
            assert cfg.max_call_duration_seconds == 600
            assert cfg.call_end_buffer_seconds == 30
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new && python -m pytest tests/test_call_timeout.py::TestCallTimeoutConfig::test_default_values -v
```
Expected: FAIL with `AttributeError`（`AppConfig` 没有 `max_call_duration_seconds` 属性）

- [ ] **Step 3: 在 `config.py` 中新增两个字段**

在 `AppConfig` 类（第 169-191 行）中修改：

```python
@dataclass
class AppConfig:
    max_concurrent_calls: int = 50
    api_port: int = 8000
    debug: bool = False
    # 单路通话最长时长（秒），超时强制挂断
    max_call_duration: int = 300  # 保留兼容
    # ★ 通话时长限制（新增）
    max_call_duration_seconds: int = 300
    call_end_buffer_seconds: int = 20
    # API 鉴权 Token（空 = 开放，仅限开发）
    api_token: str = ""

    def __post_init__(self):
        self.max_concurrent_calls = _env_int("MAX_CONCURRENT_CALLS", self.max_concurrent_calls)
        self.api_port = _env_int("API_PORT", self.api_port)
        self.debug = _env_bool("DEBUG", self.debug)
        self.max_call_duration = _env_int("MAX_CALL_DURATION", self.max_call_duration)
        # ★ 通话时长限制（新增）
        self.max_call_duration_seconds = _env_int("MAX_CALL_DURATION_SECONDS", self.max_call_duration_seconds)
        self.call_end_buffer_seconds = _env_int("CALL_END_BUFFER_SECONDS", self.call_end_buffer_seconds)
        self.api_token = _env("API_TOKEN", self.api_token)
        self.freeswitch = FreeSwitchConfig()
        self.asr = ASRConfig()
        self.tts = TTSConfig()
        self.llm = LLMConfig()
        self.db = DatabaseConfig()
        self.redis = RedisConfig()
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new && python -m pytest tests/test_call_timeout.py -v
```
Expected: PASS (2/2)

- [ ] **Step 5: 提交**

```bash
git add backend/core/config.py tests/test_call_timeout.py
git commit -m "feat(config): 新增通话时长限制环境变量配置 MAX_CALL_DURATION_SECONDS / CALL_END_BUFFER_SECONDS"
```

---

### Task 2: CallAgent — 超时看门狗 + 超时处理辅助函数

**Files:**
- Modify: `backend/core/call_agent.py`
- Test: `tests/test_call_timeout.py`

- [ ] **Step 1: 编写测试 — `_say_timeout_closing` 拼接正确结束语**

```python
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

        agent = CallAgent(
            session=mock_session,
            ctx=mock_ctx,
            system_prompt="",
        )
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

        agent = CallAgent(session=mock_session, ctx=mock_ctx, system_prompt="")
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

        agent = CallAgent(session=mock_session, ctx=mock_ctx, system_prompt="")
        agent._barge_in_asr_task = asyncio.create_task(asyncio.sleep(100))

        await agent._stop_tts_if_playing()

        assert agent._barge_in_asr_task is None
        assert agent._stop_tts_if_playing.called is True  # 验证被调用
```

- [ ] **Step 2: 运行测试验证失败**

```bash
python -m pytest tests/test_call_timeout.py::TestCallAgentTimeout -v
```
Expected: FAIL with `AttributeError`（`CallAgent` 没有 `_say_timeout_closing` 等方法）

- [ ] **Step 3: 实现 `_start_timeout_watchdog` + `_timeout_watchdog`**

在 `CallAgent.__init__` 中新增属性（紧跟 `_barge_in` 相关属性，约第 265 行之后）：

```python
self._call_timeout = asyncio.Event()
self._call_timeout_task: Optional[asyncio.Task] = None
```

在 `CallAgent` 类中添加两个方法（建议放在 `_cleanup` 之前，约第 1216 行之前）：

```python
def _start_timeout_watchdog(self):
    """启动通话超时看门狗。"""
    total = config.max_call_duration_seconds
    buffer = config.call_end_buffer_seconds
    trigger_at = max(total - buffer, 1)
    logger.info(
        f"[{self.ctx.uuid}] 通话超时看门狗启动: 总时长={total}s, "
        f"缓冲={buffer}s, 触发点={trigger_at}s"
    )
    self._call_timeout_task = asyncio.create_task(
        self._timeout_watchdog(trigger_at)
    )

async def _timeout_watchdog(self, trigger_at: float):
    """倒计时到触发点后设置超时信号。"""
    try:
        await asyncio.sleep(trigger_at)
        logger.warning(f"[{self.ctx.uuid}] 通话时长即将结束，触发超时信号")
        self._call_timeout.set()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[{self.ctx.uuid}] 超时看门狗异常: {e}")
        self._call_timeout.set()  # 异常降级：立即触发超时
```

- [ ] **Step 4: 实现 `_stop_tts_if_playing` + `_say_timeout_closing`**

在 `_start_timeout_watchdog` 之后添加：

```python
async def _stop_tts_if_playing(self):
    """超时触发时立即停止 TTS 播报。"""
    if self.session:
        try:
            await self.session.stop_playback()
        except Exception as e:
            logger.debug(f"[{self.ctx.uuid}] 停止 TTS 失败（可忽略）: {e}")
    # 取消残留的 barge-in ASR 任务
    if self._barge_in_asr_task and not self._barge_in_asr_task.done():
        self._barge_in_asr_task.cancel()
        await asyncio.gather(self._barge_in_asr_task, return_exceptions=True)
        self._barge_in_asr_task = None
        logger.info(f"[{self.ctx.uuid}] 超时打断：已取消残留的 barge-in ASR 任务")

async def _say_timeout_closing(self):
    """播放超时结束语：硬编码前缀 + 话术 closing_script。"""
    closing = ""
    if getattr(self, "_script_config", None):
        closing = getattr(self._script_config, "closing_script", "") or ""
    if not closing:
        closing = "感谢您的接听，再见！"
    hangup_msg = f"您本次通话时长已结束。{closing}"
    logger.info(f"[{self.ctx.uuid}] 超时结束语: {hangup_msg!r}")
    await self._say(hangup_msg, speech_type="closing")
    # 等待播放完成（动态等待，复用挂断语等待逻辑）
    wait_ms = max(len(hangup_msg) * 150, 2000)
    logger.debug(f"[{self.ctx.uuid}] 等待超时结束语播放完成: {wait_ms}ms")
    await asyncio.sleep(wait_ms / 1000.0)
```

- [ ] **Step 5: 运行测试验证通过**

```bash
python -m pytest tests/test_call_timeout.py::TestCallAgentTimeout -v
```
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/core/call_agent.py tests/test_call_timeout.py
git commit -m "feat(call_agent): 新增超时看门狗 + _stop_tts_if_playing + _say_timeout_closing"
```

---

### Task 3: 改造 `run()` + `_conversation_loop()` — 集成超时信号

**Files:**
- Modify: `backend/core/call_agent.py`（第 285-470 行区域）

- [ ] **Step 1: 改造 `run()` — 替换旧超时逻辑**

将 `run()` 中第 323-331 行：

```python
            # 7. 主对话循环，整体超时保护
            try:
                await asyncio.wait_for(
                    self._conversation_loop(),
                    timeout=MAX_CALL_DURATION,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[{self.ctx.uuid}] 通话超过最大时长 {MAX_CALL_DURATION}s，强制结束")
                await self._say("感谢您的耐心，通话时间已到，再见！", record=False)
```

替换为：

```python
            # 7. 启动通话超时看门狗
            self._start_timeout_watchdog()

            # 8. 主对话循环（超时信号在循环内处理）
            await self._conversation_loop()
```

在 `finally` 块中（约第 345-346 行），加入看门狗清理：

```python
        finally:
            # 清理超时看门狗任务
            if self._call_timeout_task and not self._call_timeout_task.done():
                self._call_timeout_task.cancel()
                await asyncio.gather(self._call_timeout_task, return_exceptions=True)
            await self._cleanup()
```

- [ ] **Step 2: 改造 `_conversation_loop()` — 循环入口检查**

在 `_conversation_loop()` 第 362 行 `while` 循环入口处，紧跟 `while self.sm.should_continue() and self.session._connected:` 之后添加：

```python
            # ★ 检查通话超时
            if self._call_timeout.is_set():
                logger.info(f"[{self.ctx.uuid}] 通话超时，打断当前播报（如有），发送超时结束语")
                await self._stop_tts_if_playing()
                await self._say_timeout_closing()
                break
```

- [ ] **Step 3: `_say()` 返回后、`_think_and_reply_stream()` 开始前也检查一次**

在 `_conversation_loop()` 中，找到第 452 行附近 `if reply_text and action not in ("transfer",):` 之前，添加检查：

```python
            # ★ 检查通话超时（防止 LLM 流式输出期间超时）
            if self._call_timeout.is_set():
                logger.info(f"[{self.ctx.uuid}] 通话超时（LLM 返回后），打断当前播报（如有），发送超时结束语")
                await self._stop_tts_if_playing()
                await self._say_timeout_closing()
                break
```

具体位置：在第 451 行 `# 播报（transfer 时先播再转，end 时先说再挂）` 之前插入。

- [ ] **Step 4: 删除旧 `MAX_CALL_DURATION` 常量**

删除第 36 行：

```python
MAX_CALL_DURATION = int(config.__dict__.get("max_call_duration", 300))
```

确认无其他地方引用该常量（仅有 `run()` 中使用，已被替换）。

- [ ] **Step 5: 运行全部测试**

```bash
python -m pytest tests/test_call_timeout.py -v
```
Expected: PASS (全部 5 个测试)

同时运行现有测试确保无回归：

```bash
python -m pytest tests/test_no_response_config.py -v
```
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/core/call_agent.py
git commit -m "feat(call_agent): 替换旧超时逻辑为看门狗信号模式，_conversation_loop 内检查超时"
```

---

### Task 4: 集成测试 — 模拟超时触发完整流程

**Files:**
- Test: `tests/test_call_timeout.py`（新增测试类）

- [ ] **Step 1: 编写集成测试**

在 `tests/test_call_timeout.py` 末尾新增：

```python
class TestCallTimeoutIntegration:
    """集成测试：模拟完整超时触发流程。"""

    @pytest.mark.asyncio
    async def test_conversation_loop_breaks_on_timeout(self):
        """超时触发后 _conversation_loop 正确退出。"""
        import asyncio
        from backend.core.call_agent import CallAgent
        from backend.core.state_machine import CallState, CallResult

        # 构造 Mock
        mock_session = type("MockSession", (), {
            "_connected": True,
            "stop_playback": lambda self: asyncio.coroutine(lambda: None)(),
            "start_audio_capture": lambda self: asyncio.Queue(),
        })()
        mock_ctx = type("MockCtx", (), {
            "uuid": "test-integration-001",
            "result": CallResult.NOT_STARTED,
            "script_id": "test",
            "messages": [],
        })()

        # 构造 agent
        agent = CallAgent(session=mock_session, ctx=mock_ctx, system_prompt="")
        agent._script_config = type("MockScript", (), {"closing_script": "再见！"})()

        # 手动设置超时信号
        agent._call_timeout.set()

        # 构造最小状态
        class MockSM:
            def should_continue(self): return True
            def transition(self, state): pass
        agent.sm = MockSM()

        # _say 捕获
        agent._say_captured = []
        async def fake_say(text, speech_type="closing", record=True):
            agent._say_captured.append(text)
            return False, ""
        agent._say = fake_say

        await agent._conversation_loop()

        # 验证结束语
        assert any("通话时长已结束" in msg for msg in agent._say_captured), \
            f"Expected timeout closing message, got: {agent._say_captured}"

    @pytest.mark.asyncio
    async def test_timeout_watchdog_sets_event_after_delay(self):
        """看门狗在指定时间后设置超时事件。"""
        import asyncio
        from backend.core.call_agent import CallAgent
        from unittest.mock import patch

        with patch("backend.core.call_agent.config") as mock_config:
            mock_config.max_call_duration_seconds = 3
            mock_config.call_end_buffer_seconds = 1

            mock_session = type("MockSession", (), {"_connected": True})()
            mock_ctx = type("MockCtx", (), {"uuid": "test-watchdog"})()

            agent = CallAgent(session=mock_session, ctx=mock_ctx, system_prompt="")
            agent._start_timeout_watchdog()

            # 2 秒后应该已触发（3-1=2 秒）
            await asyncio.sleep(2.5)
            assert agent._call_timeout.is_set()

            # 清理
            if agent._call_timeout_task and not agent._call_timeout_task.done():
                agent._call_timeout_task.cancel()
```

- [ ] **Step 2: 运行集成测试**

```bash
python -m pytest tests/test_call_timeout.py::TestCallTimeoutIntegration -v
```
Expected: PASS

- [ ] **Step 3: 运行全部测试套件**

```bash
python -m pytest tests/ -v
```
Expected: 全部 PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_call_timeout.py
git commit -m "test(call_timeout): 新增超时触发集成测试"
```
