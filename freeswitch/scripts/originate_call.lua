--[[
  ╔══════════════════════════════════════════════════════════════╗
  ║  Lua 脚本: originate_call.lua                               ║
  ║                                                              ║
  ║  通过 Lua 脚本发起外呼，比直接 ESL originate 更灵活：        ║
  ║  - 可在拨号前查 CRM 判断是否在黑名单                         ║
  ║  - 可实现优先级队列                                          ║
  ║  - 可实现重试逻辑                                            ║
  ║                                                              ║
  ║  调用方式：                                                   ║
  ║  fs_cli -x "luarun originate_call.lua 13800138000 task_001"  ║
  ╚══════════════════════════════════════════════════════════════╝
--]]

-- 读取参数
local phone_number = argv[1]  -- 被叫号码
local task_id      = argv[2]  -- 任务 ID
local script_id    = argv[3] or "finance_product_a"  -- 话术模板

-- 参数校验
if not phone_number or not task_id then
  freeswitch.consoleLog("ERR", "用法: luarun originate_call.lua <手机号> <任务ID>\n")
  return
end

-- 号码格式校验（11位手机号）
if not string.match(phone_number, "^1[3-9]%d{9}$") then
  freeswitch.consoleLog("ERR", string.format("无效手机号: %s\n", phone_number))
  return
end

-- 生成通话 UUID
local call_uuid = freeswitch.getUUID()

-- 构建 originate 命令
-- 变量注入到通话频道，ESL Socket 连接后可读取
local originate_cmd = string.format(
  "originate {" ..
  "origination_uuid=%s," ..
  "task_id=%s," ..
  "script_id=%s," ..
  "ai_agent=true," ..
  "originate_timeout=30," ..
  "origination_caller_id_number=%s," ..  -- 主叫号码（需备案）
  "origination_caller_id_name=AI客服," ..
  "ignore_early_media=true," ..
  "hangup_after_bridge=false" ..
  "}" ..
  "sofia/gateway/carrier_trunk/%s " ..   -- 被叫
  "&socket(127.0.0.1:9999 async full)",  -- 接通后交给 CallAgent
  call_uuid,
  task_id,
  script_id,
  "02100000000",  -- 替换为实际备案号码
  phone_number
)

freeswitch.consoleLog("INFO", string.format(
  "[%s] 发起外呼: %s (task=%s)\n",
  call_uuid, phone_number, task_id
))

-- 执行 bgapi（后台执行，不阻塞）
local api = freeswitch.API()
local result = api:executeString("bgapi " .. originate_cmd)

freeswitch.consoleLog("INFO", string.format(
  "[%s] originate 结果: %s\n",
  call_uuid, tostring(result)
))
