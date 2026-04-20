# 无回应挂断功能设计

**日期**: 2026-04-20
**状态**: 待审核

## 需求概述

在 AI 外呼通话中，当用户接通后多次无回应（静音或 ASR 超时），系统应在达到配置次数后播放挂断语并结束通话。

## 设计原则

- 复用现有的 `is_all_silent`（VAD 全程静音）+ `is_asr_timeout`（ASR 超时）判定逻辑
- 每套话术独立配置，通过数据库持久化
- 支持两种计数模式：连续（用户回应清零）/ 累计（全生命周期计数）

## 架构

### 数据库模型（`backend/models/call_script.py`）

在 `CallScript` 模型中新增字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `no_response_timeout` | Integer | 3 | 无回应判定超时（秒） |
| `no_response_mode` | String(20) | `consecutive` | 计数模式：`consecutive` / `cumulative` |
| `no_response_max_count` | Integer | 3 | 触发挂断的次数阈值 |
| `no_response_hangup_msg` | Text | "感谢你的接听，让我们下次再联系，再见！" | 自定义挂断语 |
| `no_response_hangup_enabled` | Boolean | True | 是否启用自定义挂断语 |

### 前端表单（`frontend/index.html`）

在话术编辑弹窗中，打断策略和宽容时间之后新增"无回应配置"区域：

1. 下拉选择：计数模式（连续 / 累计）
2. 数字输入：无回应超时（秒），默认 3，范围 1-30
3. 数字输入：触发挂断次数，默认 3，范围 1-10
4. 复选框：启用自定义挂断语
5. 文本域：挂断语内容（勾选复选框时启用）

布局跟随现有"打断策略配置"风格。

### CallAgent 逻辑改造（`backend/core/call_agent.py`）

#### 配置读取

在 `CallAgent.run()` 启动时从话术配置读取参数，或通过 `get_no_response_config(script_id)` 获取。

#### 对话循环改造

`_conversation_loop()` 中替换硬编码的 `silence_retries` 逻辑：

```
no_response_count = 0

循环中：
  user_text, is_all_silent, is_asr_timeout = await self._listen_user(...)

  if not user_text:
    # 无回应判定：VAD 全程静音 或 ASR 超时
    if is_all_silent or is_asr_timeout:
      no_response_count += 1
      if no_response_count >= no_response_max_count:
        # 播放挂断语
        hangup_msg = no_response_hangup_msg if no_response_hangup_enabled else script.closing_script
        await self._say(hangup_msg or "感谢接听，再见！")
        break
      else:
        await self._say("您好，请问您还在吗？")
        continue

    # 其他情况（ASR 返回空但非静音/超时），也计为无回应
    no_response_count += 1
    if no_response_count >= no_response_max_count:
      hangup_msg = no_response_hangup_msg if no_response_hangup_enabled else script.closing_script
      await self._say(hangup_msg or "感谢接听，再见！")
      break
    await self._say("您好，请问您还在吗？")
    continue

  # 用户有效回应
  if no_response_mode == "consecutive":
    no_response_count = 0
```

### 新增辅助函数（`backend/services/async_script_utils.py`）

```python
async def get_no_response_config(script_id: str) -> dict:
    """获取话术的无回应配置。"""
    # 从数据库读取话术配置，返回包含 no_response_* 字段的 dict
    # 老数据字段为 None 时使用默认值
    return {
        "timeout": script.no_response_timeout or 3,
        "mode": script.no_response_mode or "consecutive",
        "max_count": script.no_response_max_count or 3,
        "hangup_msg": script.no_response_hangup_msg,
        "hangup_enabled": script.no_response_hangup_enabled if script.no_response_hangup_enabled is not None else True,
    }
```

### API 改动（`backend/api/scripts_api.py`）

- `save_script()` 接收新增的 5 个字段
- `get_script()` 返回新增的 5 个字段
- 前端 `openCreateScript()` 和 `editScript()` 函数同步更新

### 数据库迁移

创建 `backend/scripts/migrate_add_no_response_fields.py`，参照已有的 `migrate_add_tolerance_fields.py` 模式：

- 新增 5 个列到 `call_scripts` 表
- 设置默认值
- 幂等执行（已存在列则跳过）

### 错误处理

- 话术无回应字段为 None（老数据）：使用硬编码默认值
- 挂断语为空字符串或 None：fallback 到 "感谢接听，再见！"
- 计数模式值异常：fallback 到 `consecutive`

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `backend/models/call_script.py` | 修改 | 新增 5 个字段 |
| `backend/core/call_agent.py` | 修改 | 替换硬编码无回应逻辑 |
| `backend/services/async_script_utils.py` | 修改 | 新增 `get_no_response_config()` |
| `backend/api/scripts_api.py` | 修改 | 接收/返回新字段 |
| `backend/scripts/migrate_add_no_response_fields.py` | 新增 | 数据库迁移脚本 |
| `frontend/index.html` | 修改 | 新增无回应配置表单 |
