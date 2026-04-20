# 无回应挂断功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在话术表中新增无回应配置字段，替换 CallAgent 中硬编码的无回应追问/挂断逻辑，并在前端提供可配置的表单。

**Architecture:** 数据库模型新增 5 个字段 → ScriptConfig/ScriptService 读取 → async_script_utils 提供配置函数 → CallAgent 对话循环读取配置替换硬编码 → 前端表单新增配置区。

**Tech Stack:** Python (SQLAlchemy, FastAPI), SQLite/PostgreSQL, 纯 HTML/JS 前端

---

### Task 1: 数据库模型新增无回应字段

**Files:**
- Modify: `backend/models/call_script.py:1-37`

- [ ] **Step 1: 在 CallScript 模型中新增 5 个字段**

在 `tolerance_ms` 字段之后（文件末尾，第 37 行后）新增：

```python
    # 无回应配置
    no_response_timeout = Column(Integer, nullable=False, default=3)  # 无回应判定超时（秒）
    no_response_mode = Column(String(20), nullable=False, default='consecutive')  # consecutive / cumulative
    no_response_max_count = Column(Integer, nullable=False, default=3)  # 触发挂断次数
    no_response_hangup_msg = Column(Text)  # 自定义挂断语
    no_response_hangup_enabled = Column(Boolean, nullable=False, default=True)  # 是否启用自定义挂断语
```

- [ ] **Step 2: 验证语法**

Run: `python -c "from backend.models.call_script import CallScript; print([c.name for c in CallScript.__table__.columns])"`
Expected: 输出包含所有列名，包括新增的 5 个字段

---

### Task 2: ScriptConfig 数据类新增字段

**Files:**
- Modify: `backend/services/script_service.py:34-53` (ScriptConfig dataclass)

- [ ] **Step 1: 在 ScriptConfig dataclass 中新增 5 个字段**

在 `tolerance_ms` 字段之后（第 52 行后）新增：

```python
    no_response_timeout: int = 3
    no_response_mode: str = "consecutive"
    no_response_max_count: int = 3
    no_response_hangup_msg: Optional[str] = None
    no_response_hangup_enabled: bool = True
```

- [ ] **Step 2: 更新 get_script() 中的 ScriptConfig 构造**

在 `script_service.py` 的 `get_script()` 方法中（约第 79-96 行），ScriptConfig 构造增加新字段映射：

```python
                script_config = ScriptConfig(
                    # ... 现有字段 ...
                    tolerance_enabled=db_script.tolerance_enabled,
                    tolerance_ms=db_script.tolerance_ms,
                    no_response_timeout=db_script.no_response_timeout,
                    no_response_mode=db_script.no_response_mode,
                    no_response_max_count=db_script.no_response_max_count,
                    no_response_hangup_msg=db_script.no_response_hangup_msg,
                    no_response_hangup_enabled=db_script.no_response_hangup_enabled,
                )
```

- [ ] **Step 3: 更新 get_all_scripts() 中的 ScriptConfig 构造**

在 `get_all_scripts()` 方法中（约第 115-132 行），同样增加新字段映射：

```python
                    script_config = ScriptConfig(
                        # ... 现有字段 ...
                        tolerance_enabled=db_script.tolerance_enabled,
                        tolerance_ms=db_script.tolerance_ms,
                        no_response_timeout=db_script.no_response_timeout,
                        no_response_mode=db_script.no_response_mode,
                        no_response_max_count=db_script.no_response_max_count,
                        no_response_hangup_msg=db_script.no_response_hangup_msg,
                        no_response_hangup_enabled=db_script.no_response_hangup_enabled,
                    )
```

- [ ] **Step 4: 更新 create_script() 中的 CallScript 构造**

在 `create_script()` 方法中（约第 180-197 行），CallScript 构造增加新字段：

```python
                new_script = CallScript(
                    # ... 现有字段 ...
                    tolerance_enabled=script_config.tolerance_enabled,
                    tolerance_ms=script_config.tolerance_ms,
                    no_response_timeout=script_config.no_response_timeout,
                    no_response_mode=script_config.no_response_mode,
                    no_response_max_count=script_config.no_response_max_count,
                    no_response_hangup_msg=script_config.no_response_hangup_msg,
                    no_response_hangup_enabled=script_config.no_response_hangup_enabled,
                )
```

- [ ] **Step 5: 验证语法**

Run: `python -c "from backend.services.script_service import ScriptConfig, script_service; print('OK')"`
Expected: 输出 OK，无报错

---

### Task 3: API 接收/返回新字段

**Files:**
- Modify: `backend/api/scripts_api.py`

- [ ] **Step 1: list_scripts() 返回新字段**

在 `list_scripts()` 的返回 dict 中（第 21-38 行），增加：

```python
            {
                # ... 现有字段 ...
                "tolerance_enabled": s.tolerance_enabled,
                "tolerance_ms": s.tolerance_ms,
                "no_response_timeout": s.no_response_timeout,
                "no_response_mode": s.no_response_mode,
                "no_response_max_count": s.no_response_max_count,
                "no_response_hangup_msg": s.no_response_hangup_msg,
                "no_response_hangup_enabled": s.no_response_hangup_enabled,
                "is_active": s.is_active
            }
```

- [ ] **Step 2: get_script() 返回新字段**

在 `get_script()` 的返回 dict 中（第 55-72 行），增加：

```python
            "tolerance_enabled": script.tolerance_enabled,
            "tolerance_ms": script.tolerance_ms,
            "no_response_timeout": script.no_response_timeout,
            "no_response_mode": script.no_response_mode,
            "no_response_max_count": script.no_response_max_count,
            "no_response_hangup_msg": script.no_response_hangup_msg,
            "no_response_hangup_enabled": script.no_response_hangup_enabled,
            "is_active": script.is_active
```

- [ ] **Step 3: create_script() 接收新字段**

在 `create_script()` 中构造 ScriptConfig 的地方（第 110-127 行），增加新字段：

```python
        script_config = ScriptConfig(
            # ... 现有字段 ...
            tolerance_enabled=script_data.get("tolerance_enabled", True),
            tolerance_ms=script_data.get("tolerance_ms", 1000),
            no_response_timeout=script_data.get("no_response_timeout", 3),
            no_response_mode=script_data.get("no_response_mode", "consecutive"),
            no_response_max_count=script_data.get("no_response_max_count", 3),
            no_response_hangup_msg=script_data.get("no_response_hangup_msg"),
            no_response_hangup_enabled=script_data.get("no_response_hangup_enabled", True),
        )
```

- [ ] **Step 4: update_script() 接收新字段**

在 `update_script()` 中（第 183-186 行，tolerance 字段之后），增加：

```python
        if "no_response_timeout" in script_data:
            update_data["no_response_timeout"] = script_data["no_response_timeout"]
        if "no_response_mode" in script_data:
            update_data["no_response_mode"] = script_data["no_response_mode"]
        if "no_response_max_count" in script_data:
            update_data["no_response_max_count"] = script_data["no_response_max_count"]
        if "no_response_hangup_msg" in script_data:
            update_data["no_response_hangup_msg"] = script_data["no_response_hangup_msg"]
        if "no_response_hangup_enabled" in script_data:
            update_data["no_response_hangup_enabled"] = script_data["no_response_hangup_enabled"]
```

- [ ] **Step 5: 运行 API 相关测试**

Run: `cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new && python -m pytest tests/test_all.py -v -k "API" --tb=short 2>&1 | tail -20`
Expected: 现有 API 测试全部通过

---

### Task 4: 新增 get_no_response_config() 辅助函数

**Files:**
- Modify: `backend/services/async_script_utils.py`

- [ ] **Step 1: 新增 get_no_response_config() 函数**

在文件末尾（第 177 行之后）新增：

```python
async def get_no_response_config(script_id: str) -> dict:
    """
    获取话术的无回应配置。

    Returns:
        dict with keys:
            timeout: int (秒), 默认 3
            mode: str ("consecutive" | "cumulative"), 默认 "consecutive"
            max_count: int, 默认 3
            hangup_msg: str | None, 自定义挂断语
            hangup_enabled: bool, 是否启用自定义挂断语
            closing_script: str | None, 结束语（用于 hangup_enabled=False 时 fallback）
    """
    script_config = await script_service.get_script(script_id)
    if not script_config:
        return {
            "timeout": 3,
            "mode": "consecutive",
            "max_count": 3,
            "hangup_msg": None,
            "hangup_enabled": True,
            "closing_script": None,
        }

    return {
        "timeout": script_config.no_response_timeout or 3,
        "mode": script_config.no_response_mode if script_config.no_response_mode in ("consecutive", "cumulative") else "consecutive",
        "max_count": script_config.no_response_max_count or 3,
        "hangup_msg": script_config.no_response_hangup_msg,
        "hangup_enabled": script_config.no_response_hangup_enabled if script_config.no_response_hangup_enabled is not None else True,
        "closing_script": script_config.closing_script,
    }
```

- [ ] **Step 2: 验证语法**

Run: `python -c "from backend.services.async_script_utils import get_no_response_config; print('OK')"`
Expected: 输出 OK

---

### Task 5: CallAgent 对话循环改造

**Files:**
- Modify: `backend/core/call_agent.py`

- [ ] **Step 1: 更新 import**

在 import 行（第 30 行）中，增加 `get_no_response_config`：

```python
from backend.services.async_script_utils import get_system_prompt_for_call, get_opening_for_call, get_barge_in_config, get_no_response_config
```

- [ ] **Step 2: 在 _conversation_loop() 中读取配置并替换 silence_retries 逻辑**

替换 `_conversation_loop()` 方法（第 346-440 行）中第 352-385 行的 `silence_retries` 逻辑。

将：
```python
        silence_retries = 0
```

替换为：
```python
        no_response_config = await get_no_response_config(self.ctx.script_id)
        no_response_mode = no_response_config["mode"]
        no_response_max_count = no_response_config["max_count"]
        no_response_hangup_msg = no_response_config["hangup_msg"]
        no_response_hangup_enabled = no_response_config["hangup_enabled"]
        closing_script = no_response_config["closing_script"]
        no_response_count = 0
```

将第 366-385 行的无回应处理逻辑：
```python
            if not user_text:
                if is_all_silent:
                    logger.info(f"[{self.ctx.uuid}] 本轮全静音，不计入无响应，继续监听")
                    continue
                if is_asr_timeout:
                    silence_retries += 1
                    if silence_retries > MAX_SILENCE_RETRIES:
                        logger.info(f"[{self.ctx.uuid}] 连续 {silence_retries} 次无响应，结束通话")
                        break
                    logger.info(f"[{self.ctx.uuid}] 第 {silence_retries} 次无响应（ASR 超时），追问")
                    await self._say("您好，请问您还在吗？")
                    continue
                silence_retries += 1
                if silence_retries > MAX_SILENCE_RETRIES:
                    logger.info(f"[{self.ctx.uuid}] 连续 {silence_retries} 次无响应，结束通话")
                    break
                logger.info(f"[{self.ctx.uuid}] 第 {silence_retries} 次无响应，追问")
                await self._say("您好，请问您还在吗？")
                continue
            silence_retries = 0
```

替换为：
```python
            if not user_text:
                # 无回应判定：VAD 全程静音 或 ASR 超时
                if is_all_silent:
                    logger.info(f"[{self.ctx.uuid}] 本轮全静音，不计入无回应，继续监听")
                    continue

                no_response_count += 1
                if no_response_count >= no_response_max_count:
                    # 达到最大次数，播放挂断语并结束
                    if no_response_hangup_enabled and no_response_hangup_msg:
                        hangup_msg = no_response_hangup_msg
                    elif closing_script:
                        hangup_msg = closing_script
                    else:
                        hangup_msg = "感谢接听，再见！"
                    logger.info(f"[{self.ctx.uuid}] 连续/累计 {no_response_count} 次无回应，播放挂断语并结束通话")
                    await self._say(hangup_msg)
                    break

                logger.info(f"[{self.ctx.uuid}] 第 {no_response_count} 次无回应（{no_response_mode}），追问")
                await self._say("您好，请问您还在吗？")
                continue

            # 用户有效回应
            if no_response_mode == "consecutive":
                no_response_count = 0
```

- [ ] **Step 3: 运行 CallAgent 相关测试**

Run: `cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new && python -m pytest tests/test_all.py -v -k "CallAgent" --tb=short 2>&1 | tail -20`
Expected: 现有 CallAgent 测试全部通过

---

### Task 6: 数据库迁移脚本

**Files:**
- Create: `backend/scripts/migrate_add_no_response_fields.py`

- [ ] **Step 1: 创建迁移脚本**

参照 `migrate_add_tolerance_fields.py` 的模式，创建：

```python
"""
数据库迁移脚本：为 call_scripts 表新增无回应配置字段
运行方式: python -m backend.scripts.migrate_add_no_response_fields
"""
import asyncio
import logging

from sqlalchemy import text
from backend.utils.session_manager import session_manager

logger = logging.getLogger(__name__)

COLUMNS = [
    ("no_response_timeout", "ALTER TABLE call_scripts ADD COLUMN no_response_timeout INTEGER NOT NULL DEFAULT 3"),
    ("no_response_mode", "ALTER TABLE call_scripts ADD COLUMN no_response_mode VARCHAR(20) NOT NULL DEFAULT 'consecutive'"),
    ("no_response_max_count", "ALTER TABLE call_scripts ADD COLUMN no_response_max_count INTEGER NOT NULL DEFAULT 3"),
    ("no_response_hangup_msg", "ALTER TABLE call_scripts ADD COLUMN no_response_hangup_msg TEXT"),
    ("no_response_hangup_enabled", "ALTER TABLE call_scripts ADD COLUMN no_response_hangup_enabled BOOLEAN NOT NULL DEFAULT TRUE"),
]


async def migrate():
    async with session_manager.get_session() as session:
        try:
            for col_name, alter_sql in COLUMNS:
                result = await session.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'call_scripts' AND column_name = :col_name"
                ), {"col_name": col_name})
                if result.fetchone():
                    logger.info(f"{col_name} 字段已存在，跳过")
                else:
                    await session.execute(text(alter_sql))
                    logger.info(f"{col_name} 字段已添加")

            await session.commit()
            logger.info("无回应配置字段迁移完成")
        except Exception as e:
            logger.error(f"数据库迁移失败: {e}")
            await session.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(migrate())
```

- [ ] **Step 2: 验证语法**

Run: `python -c "import backend.scripts.migrate_add_no_response_fields; print('OK')"`
Expected: 输出 OK

---

### Task 7: 前端表单新增无回应配置区

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: 在话术编辑弹窗中新增"无回应配置"区域**

在话术编辑弹窗的宽容时间配置之后、`modal-foot` 之前（约第 427-428 行之间），在 `</div>` 关闭 barge-in 区域的 div 之后，新增：

```html
      <div style="border-top:1px solid var(--border);padding-top:12px;margin-top:12px;margin-bottom:12px">
        <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:10px">无回应配置</div>
        <div class="form-group">
          <label>计数模式</label>
          <select id="sf-no-response-mode">
            <option value="consecutive">连续无回应</option>
            <option value="cumulative">累计无回应</option>
          </select>
          <div class="form-hint">连续：用户回应后清零；累计：整个通话累计计数</div>
        </div>
        <div style="display:flex;gap:16px">
          <div class="form-group" style="flex:1">
            <label>无回应超时（秒）</label>
            <input id="sf-no-response-timeout" type="number" value="3" min="1" max="30"/>
          </div>
          <div class="form-group" style="flex:1">
            <label>触发挂断次数</label>
            <input id="sf-no-response-max-count" type="number" value="3" min="1" max="10"/>
          </div>
        </div>
        <div class="form-group">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
            <input id="sf-no-response-hangup-enabled" type="checkbox" checked/> 启用自定义挂断语
          </label>
        </div>
        <div class="form-group">
          <label>挂断语</label>
          <textarea id="sf-no-response-hangup-msg" rows="2" placeholder="感谢你的接听，让我们下次再联系，再见！">感谢你的接听，让我们下次再联系，再见！</textarea>
          <div class="form-hint">不启用时使用结束语替代</div>
        </div>
      </div>
```

- [ ] **Step 2: 更新 openCreateScript() 初始化新字段**

在 `openCreateScript()` 函数中（第 758-774 行），在 `$('sf-tolerance-ms').value=1000;` 之后新增：

```javascript
  $('sf-no-response-mode').value='consecutive';
  $('sf-no-response-timeout').value=3;
  $('sf-no-response-max-count').value=3;
  $('sf-no-response-hangup-enabled').checked=true;
  $('sf-no-response-hangup-msg').value='感谢你的接听，让我们下次再联系，再见！';
```

- [ ] **Step 3: 更新 openEditScript() 加载新字段**

在 `openEditScript()` 函数中（第 777-797 行），在 `$('sf-tolerance-ms').value=d.tolerance_ms??1000;` 之后新增：

```javascript
    $('sf-no-response-mode').value=d.no_response_mode||'consecutive';
    $('sf-no-response-timeout').value=d.no_response_timeout??3;
    $('sf-no-response-max-count').value=d.no_response_max_count??3;
    $('sf-no-response-hangup-enabled').checked=d.no_response_hangup_enabled!==false;
    $('sf-no-response-hangup-msg').value=d.no_response_hangup_msg||'感谢你的接听，让我们下次再联系，再见！';
```

- [ ] **Step 4: 更新 saveScript() 保存新字段**

在 `saveScript()` 函数中（第 800-842 行），在 `body` 对象中（tolerance_ms 之后）新增：

```javascript
    tolerance_enabled: $('sf-tolerance-enabled').checked,
    tolerance_ms: parseInt($('sf-tolerance-ms').value)||1000,
    no_response_mode: $('sf-no-response-mode').value,
    no_response_timeout: parseInt($('sf-no-response-timeout').value)||3,
    no_response_max_count: parseInt($('sf-no-response-max-count').value)||3,
    no_response_hangup_enabled: $('sf-no-response-hangup-enabled').checked,
    no_response_hangup_msg: $('sf-no-response-hangup-msg').value.trim()||null
  };
```

- [ ] **Step 5: 更新 viewScriptDetail() 展示新字段**

在 `viewScriptDetail()` 函数中，在打断策略 `</table>` 之后、`</div>` 之前（约第 879 行），新增独立的"无回应配置"区域：

```javascript
      <div style="margin-top:14px">
        <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:5px">无回应配置</div>
        <table style="width:100%">
          <tr><td style="color:var(--muted);width:100px;padding:6px 10px">计数模式</td><td style="padding:6px 10px">${d.no_response_mode==='cumulative'?'累计':'连续'}</td></tr>
          <tr><td style="color:var(--muted);padding:6px 10px">无回应超时</td><td style="padding:6px 10px">${d.no_response_timeout??3} 秒</td></tr>
          <tr><td style="color:var(--muted);padding:6px 10px">挂断次数</td><td style="padding:6px 10px">${d.no_response_max_count??3} 次</td></tr>
          <tr><td style="color:var(--muted);padding:6px 10px">挂断语</td><td style="padding:6px 10px">${d.no_response_hangup_enabled?'<span style="color:var(--green)">● 已启用</span>':'<span style="color:var(--muted)">● 使用结束语</span>'}</td></tr>
        </table>
      </div>
```

---

### Task 8: 单元测试 — 无回应配置获取

**Files:**
- Create: `tests/test_no_response_config.py`

- [ ] **Step 1: 编写测试**

```python
"""
无回应配置功能测试
"""
import pytest
from types import SimpleNamespace


class TestGetNoResponseConfig:
    @pytest.mark.asyncio
    async def test_returns_defaults_when_script_not_found(self, monkeypatch):
        from backend.services import async_script_utils as utils

        async def fake_get_script(script_id):
            return None

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        config = await utils.get_no_response_config("nonexistent")

        assert config["timeout"] == 3
        assert config["mode"] == "consecutive"
        assert config["max_count"] == 3
        assert config["hangup_msg"] is None
        assert config["hangup_enabled"] is True
        assert config["closing_script"] is None

    @pytest.mark.asyncio
    async def test_returns_script_config_values(self, monkeypatch):
        from backend.services import async_script_utils as utils

        fake_script = SimpleNamespace(
            no_response_timeout=5,
            no_response_mode="cumulative",
            no_response_max_count=5,
            no_response_hangup_msg="自定义挂断语",
            no_response_hangup_enabled=True,
            closing_script="结束语",
        )

        async def fake_get_script(script_id):
            return fake_script

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        config = await utils.get_no_response_config("test_script")

        assert config["timeout"] == 5
        assert config["mode"] == "cumulative"
        assert config["max_count"] == 5
        assert config["hangup_msg"] == "自定义挂断语"
        assert config["hangup_enabled"] is True
        assert config["closing_script"] == "结束语"

    @pytest.mark.asyncio
    async def test_invalid_mode_falls_back_to_consecutive(self, monkeypatch):
        from backend.services import async_script_utils as utils

        fake_script = SimpleNamespace(
            no_response_timeout=3,
            no_response_mode="invalid_mode",
            no_response_max_count=3,
            no_response_hangup_msg=None,
            no_response_hangup_enabled=None,
            closing_script=None,
        )

        async def fake_get_script(script_id):
            return fake_script

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        config = await utils.get_no_response_config("test_script")

        assert config["mode"] == "consecutive"
        assert config["hangup_enabled"] is True

    @pytest.mark.asyncio
    async def test_none_timeout_falls_back_to_default(self, monkeypatch):
        from backend.services import async_script_utils as utils

        fake_script = SimpleNamespace(
            no_response_timeout=None,
            no_response_mode="consecutive",
            no_response_max_count=None,
            no_response_hangup_msg=None,
            no_response_hangup_enabled=None,
            closing_script=None,
        )

        async def fake_get_script(script_id):
            return fake_script

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        config = await utils.get_no_response_config("test_script")

        assert config["timeout"] == 3
        assert config["max_count"] == 3
```

- [ ] **Step 2: 运行测试**

Run: `cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new && python -m pytest tests/test_no_response_config.py -v --tb=short`
Expected: 全部 4 个测试通过

---

### Task 9: 集成全部现有测试

**Files:**
- N/A (验证所有测试)

- [ ] **Step 1: 运行全部测试**

Run: `cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: 全部测试通过，无新增失败

- [ ] **Step 2: 提交**

```bash
git add backend/models/call_script.py \
        backend/services/script_service.py \
        backend/services/async_script_utils.py \
        backend/api/scripts_api.py \
        backend/scripts/migrate_add_no_response_fields.py \
        backend/core/call_agent.py \
        frontend/index.html \
        tests/test_no_response_config.py
git commit -m "feat: 新增话术无回应挂断配置

- 数据库新增 no_response_timeout/mode/max_count/hangup_msg/hangup_enabled 字段
- CallAgent 对话循环替换硬编码 silence_retries 为可配置无回应逻辑
- 支持连续/累计两种计数模式
- 前端新增无回应配置表单
- 新增 get_no_response_config() 辅助函数
- 数据库迁移脚本"
```
