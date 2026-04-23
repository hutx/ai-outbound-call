# 打断与宽容期机制：快速参考指南

## 核心机制速览

### 1. 打断（Barge-in）机制 - 让用户能打断AI

**简单来说**：用户说话时立即停止AI播报，切换到听用户的模式。

**关键配置**：
```
开场白打断：opening_barge_in = False（通常不打断）
结束语打断：closing_barge_in = False（通常不打断）
对话打断：conversation_barge_in = True（通常打断）
保护期起始：barge_in_protect_start = 3秒（播放前3秒不打断）
保护期结束：barge_in_protect_end = 3秒（播放后3秒不打断）
```

**工作流程**：
1. AI开始播报 → 启动后台ASR监听（_barge_in_asr_task）
2. 用户开始说话 → ASR识别文本
3. 检查是否在保护期 → 否则触发打断
4. 停止TTS播报 → 返回打断的文本
5. LLM用打断文本继续对话

**常见问题**：
- Q: 为什么开场白不支持打断？
  A: 让用户完整理解系统身份，避免误解。
- Q: 保护期的意义是什么？
  A: 前3秒避免用户反应慢导致误打断；后3秒避免打断结束语。

---

### 2. 宽容期（Tolerance）机制 - 让用户可以补充

**简单来说**：用户说完第一句后，系统在1秒内等待补充内容。

**关键配置**：
```
启用开关：tolerance_enabled = True（默认启用）
等待时长：tolerance_ms = 1000（1秒）
```

**工作流程**：
1. 用户说"我想查"→ ASR识别完成，返回第一句
2. 系统不立即发给LLM，而是启动宽容期
3. 在宽容期内等待补充 → 用户继续说"余额"
4. 收到补充文本 → 合并为"我想查 余额"
5. 合并的文本送给LLM处理

**宽容期如何工作**：
```
使用情况1：用户分段说话
  用户：我想查（第1句）
  系统：启动宽容期等待...
  用户：余额（第2句，在宽容期内）
  结果：合并为"我想查 余额"发给LLM

使用情况2：用户一次说完，宽容期内无补充
  用户：我想查余额（一句完）
  系统：启动宽容期等待...
  [宽容期超时]
  结果：直接用"我想查余额"发给LLM

使用情况3：禁用宽容期
  tolerance_enabled = False
  用户：我想查
  结果：立即发给LLM，不等补充
```

---

### 3. 无响应（No Response）处理 - 用户没反应怎么办

**简单来说**：用户3次没反应就挂断，中间会追问。

**关键配置**：
```
快速超时：no_response_timeout = 3秒
重试模式：no_response_mode = "consecutive"（推荐）
最大次数：no_response_max_count = 3
挂断语：no_response_hangup_msg = "感谢接听，再见！"
```

**两种重试模式**：

**模式A：Consecutive（连续模式）** - 推荐
```
含义：每次有用户有效回应，计数重置
流程：
  循环1：AI问 → 用户无反应(count=1) → 追问1
  循环2：AI追问 → 用户有回应 → count重置=0
  循环3：AI继续 → 用户又无反应(count=1) → 追问1
  
适用场景：用户可能反应慢但愿意配合
```

**模式B：Cumulative（累计模式）** - 严格
```
含义：累计所有无反应，不重置
流程：
  循环1：AI问 → 无反应(count=1) → 追问1
  循环2：AI追问 → 有回应 → count保持=1（不重置！）
  循环3：AI继续 → 无反应(count=2) → 追问2
  循环4：AI追问 → 无反应(count=3) → 挂断
  
适用场景：需要纯净输入，不容易分神的用户
```

**判定标准**：
```
什么算无响应？
  1. VAD全程未检测到语音 → 继续监听，不计入无响应
  2. 有语音但ASR识别为空 → 计入无响应，追问
  3. ASR超时 → 计入无响应，追问

什么不算无响应？
  - 用户说噪声词（"嗯"、"哦"、"好"等）→ 噪声过滤，继续等
  - 系统识别不出来 → 继续等，不立即判无响应
```

---

## 三个机制的协作关系

### 完整对话流程

```
┌─────────────────────────────────────────────────────────┐
│ 对话循环（_conversation_loop）                          │
└─────────────────────────────────────────────────────────┘
                         ↓
    ┌────────────────────┴────────────────────┐
    │ 第1步：播放开场白                        │
    │ (通常opening_barge_in=False，不打断)    │
    └────────────────────┬────────────────────┘
                         ↓
    ┌────────────────────┴────────────────────┐
    │ 第2步：监听用户（_listen_user）         │
    │ - VAD检测用户说话                       │
    │ - ASR识别用户文本                       │
    │ - 如果是全静音 → 不计入无响应，继续     │
    │ - 如果有文本 → 返回                     │
    └────────────────────┬────────────────────┘
                         ↓
    ┌────────────────────┴────────────────────┐
    │ 第3步：启动宽容期（如果启用）           │
    │ - 用户说"我想查" → 不立即发LLM          │
    │ - 等待1秒看有无补充                     │
    │ - 收到"余额" → 合并为"我想查 余额"     │
    │ - 超时无补充 → 用"我想查"发LLM         │
    └────────────────────┬────────────────────┘
                         ↓
    ┌────────────────────┴────────────────────┐
    │ 第4步：LLM处理                          │
    │ (可能流式生成多句话)                    │
    └────────────────────┬────────────────────┘
                         ↓
    ┌────────────────────┴────────────────────┐
    │ 第5步：播放AI回复                       │
    │ - TTS开始播报 → 启动barge-in监听        │
    │ - 用户打断 → 停止TTS，返回打断文本      │
    │ - 无打断 → 播报完成，继续监听           │
    └────────────────────┬────────────────────┘
                         ↓
    ┌────────────────────┴────────────────────┐
    │ 第6步：判定是否继续对话                 │
    │ - 如果action=end → 挂断                 │
    │ - 如果action=transfer → 转人工          │
    │ - 否则 → 回到第2步继续                  │
    │                                         │
    │ 无响应处理：                            │
    │ - 如果用户无回应且达到max_count → 挂断 │
    └────────────────────┬────────────────────┘
```

---

## 配置建议

### 场景1：金融产品咨询（需要精准打断）

```python
# 打断配置
opening_barge_in = False          # 开场白需要完整表述
closing_barge_in = False          # 结束语不能被打断
conversation_barge_in = True      # 对话中可以打断

barge_in_protect_start = 3        # 前3秒让用户听清
barge_in_protect_end = 3          # 后3秒完成语句

# 宽容期配置
tolerance_enabled = True          # 用户可能分段提问
tolerance_ms = 1500               # 给足补充时间

# 无响应配置
no_response_mode = "consecutive"  # 用户反应可能慢
no_response_max_count = 3         # 追问3次后挂断
no_response_timeout = 3           # 快速判定
```

### 场景2：商业推广（快速处理）

```python
# 打断配置
conversation_barge_in = True
barge_in_protect_start = 2        # 较短保护期
barge_in_protect_end = 2

# 宽容期配置
tolerance_enabled = False         # 不等补充，快速反馈
# 理由：推广通常一句完整

# 无响应配置
no_response_mode = "cumulative"   # 严格计数
no_response_max_count = 2         # 追问少
```

### 场景3：售后服务（友好沟通）

```python
# 打断配置
opening_barge_in = True           # 可以打断开场白
barge_in_protect_start = 2        # 让用户快速打断
conversation_barge_in = True

# 宽容期配置
tolerance_enabled = True
tolerance_ms = 2000               # 给足等待时间

# 无响应配置
no_response_mode = "consecutive"  # 用户为主
no_response_max_count = 4         # 多追问一次
no_response_timeout = 4           # 宽松超时
```

---

## 核心代码位置速查

| 需求 | 文件 | 方法 |
|------|------|------|
| 修改打断行为 | backend/core/call_agent.py | _main_asr_barge_in_loop |
| 修改宽容期 | backend/core/call_agent.py | _listen_tolerance |
| 修改无响应 | backend/core/call_agent.py | _conversation_loop (第392-450行) |
| 修改配置 | backend/models/call_script.py | CallScript表 |
| 获取配置 | backend/services/async_script_utils.py | get_barge_in_config / get_no_response_config |
| 状态管理 | backend/core/state_machine.py | StateMachine / CallState |
| 环境配置 | backend/core/config.py | AppConfig |

---

## 常见问题解答

**Q: 用户说"好"被当成噪声了，怎么办？**
A: 在barge-in中，"好"确实被过滤。如果你需要"好"被识别为有效打断词，需要修改call_agent.py第871-876行的NOISE_WORDS定义。但要注意这会导致真正的语气词也被识别为打断。

**Q: 宽容期内用户没有补充内容，系统会等多久？**
A: tolerance_ms毫秒。但实际等待时间是max(3.0, tolerance_ms/1000)秒。也就是说，即使tolerance_ms=500，也会等待3秒。

**Q: 用户3次无响应就挂断，太残酷了吧？**
A: 可以改max_count。另外，"无响应"的定义是ASR无有效识别，用户说"嗯""啊"这样的语气词不计入（被噪声过滤）。

**Q: 为什么打断后要发送ttsstart来恢复管道？**
A: 因为发送了stop_playback()命令后，FreeSWITCH的TTS管道会关闭。下次播报需要重新打开，所以要发ttsstart。如果不发，下一次播报会失败。

**Q: 打断、宽容期、无响应三者冲突怎么办？**
A: 优先级是：打断 > 宽容期 > 无响应。
- 如果用户打断了，barge_in_occurred=True，宽容期被跳过
- 如果宽容期内用户打断，宽容期监听任务被取消
- 无响应是监听到空文本时的兜底判定

---

## 日志调试

启用debug模式查看关键日志：
```bash
export DEBUG=true
# 或在 .env 中配置 DEBUG=true
```

关键日志关键词：
```
"⚡ barge-in 触发"          → 打断被触发
"宽容期收到补充语音"        → 宽容期收到补充
"宽容期超时"               → 宽容期等待超时
"第 N 次无回应"            → 无响应计数
"通话超时信号设置"          → 通话超时触发
"ASR 过滤噪声"             → 噪声被过滤
"本轮全静音，不计入无回应"  → 全静音判定
```

---

## 性能考虑

1. **并发任务数量**：
   - 主ASR + 打断ASR + 宽容期ASR 可能同时运行
   - 需要确保ASR连接池足够

2. **内存使用**：
   - 音频队列最大500个chunk
   - 消息历史保留20轮
   - 多路并发通话时注意内存

3. **超时设置**：
   - ASR_TIMEOUT=15s 确保不卡住
   - LLM_TIMEOUT=30s 避免LLM过慢
   - TTS_TIMEOUT=10s 防止TTS合成卡
   - VAD_SILENCE_MS=400ms 检测语音停顿

