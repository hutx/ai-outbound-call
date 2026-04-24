"""LiveKit SIP Trunk 自动初始化脚本

启动后自动完成以下操作：
1. 等待 LiveKit Server 就绪
2. 检查是否已存在 SIP Trunk（幂等）
3. 创建 Outbound SIP Trunk（指向 Kamailio）
4. 创建 Inbound SIP Trunk（接收来自 Kamailio 的呼叫）
5. 创建 SIP Dispatch Rule（将来电路由到 Agent）
6. 输出配置值（可用于填入 .env）

使用方式：
    python -m livekit_backend.scripts.init_sip_trunk

环境变量：
    LK_LIVEKIT_URL       — LiveKit Server 地址（默认 http://localhost:7880）
    LK_LIVEKIT_API_KEY   — API Key（默认 devkey）
    LK_LIVEKIT_API_SECRET — API Secret（默认 secret）
    LK_SIP_OUTBOUND_NUMBER — 外显号码（默认 +8610000000）
    LK_KAMAILIO_SIP_DOMAIN — Kamailio 地址（默认 kamailio）

依赖：livekit-api >= 0.7
"""

import asyncio
import logging
import os
import sys
import time

import httpx
from livekit import api as livekit_api

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[sip-init] %(asctime)s %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sip-init")

# ---------------------------------------------------------------------------
# 环境变量（可通过 .env 或 Docker env 传入）
# ---------------------------------------------------------------------------
# LiveKit URL：脚本用 HTTP 访问，如果传入 ws:// 自动转换
_raw_url = os.getenv("LK_LIVEKIT_URL", "http://localhost:7880")
LIVEKIT_URL = _raw_url.replace("ws://", "http://").replace("wss://", "https://")

LIVEKIT_API_KEY = os.getenv("LK_LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.getenv("LK_LIVEKIT_API_SECRET", "secret")
OUTBOUND_NUMBER = os.getenv("LK_SIP_OUTBOUND_NUMBER", "+8610000000")
# Outbound Trunk 目标地址：使用 Docker 内部主机名，而非外部 IP
# LK_KAMAILIO_SIP_DOMAIN 是外部可达地址（给软电话用），不适合容器间通信
OUTBOUND_HOST = os.getenv("LK_SIP_OUTBOUND_HOST", "kamailio")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
OUTBOUND_TRUNK_NAME = "kamailio-outbound"
INBOUND_TRUNK_NAME = "kamailio-inbound"
DISPATCH_RULE_NAME = "outbound-agent-dispatch"

MAX_WAIT_SECONDS = 30
RETRY_INTERVAL = 2


# ---------------------------------------------------------------------------
# 等待 LiveKit Server 就绪
# ---------------------------------------------------------------------------
async def wait_for_livekit() -> None:
    """循环检查 LiveKit Server 是否可连接，最多等待 MAX_WAIT_SECONDS 秒。"""
    logger.info("等待 LiveKit Server 就绪 (%s) ...", LIVEKIT_URL)
    deadline = time.monotonic() + MAX_WAIT_SECONDS

    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(LIVEKIT_URL)
                if resp.status_code < 500:
                    logger.info("LiveKit Server 已就绪 (HTTP %d)", resp.status_code)
                    return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            remaining = int(deadline - time.monotonic())
            logger.info("LiveKit Server 未就绪，%d 秒后重试（剩余 %ds）...", RETRY_INTERVAL, remaining)
            await asyncio.sleep(RETRY_INTERVAL)

    logger.error("LiveKit Server 在 %d 秒内未就绪，退出", MAX_WAIT_SECONDS)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 核心初始化逻辑
# ---------------------------------------------------------------------------
async def init_sip_trunk() -> None:
    """创建 Outbound Trunk、Inbound Trunk 及 Dispatch Rule（幂等）。"""

    lk = livekit_api.LiveKitAPI(
        url=LIVEKIT_URL,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )

    try:
        outbound_trunk_id = await _ensure_outbound_trunk(lk)
        inbound_trunk_id = await _ensure_inbound_trunk(lk)
        dispatch_rule_id = await _ensure_dispatch_rule(lk, inbound_trunk_id)
        _print_summary(outbound_trunk_id, inbound_trunk_id, dispatch_rule_id)
    finally:
        await lk.aclose()


# ---------------------------------------------------------------------------
# Outbound Trunk
# ---------------------------------------------------------------------------
async def _ensure_outbound_trunk(lk: livekit_api.LiveKitAPI) -> str:
    """检查并创建 Outbound SIP Trunk，返回 trunk_id。

    如果同名 Trunk 已存在但 numbers 不同，先删除再重建。
    """
    logger.info("检查 Outbound SIP Trunk ...")

    resp = await lk.sip.list_sip_outbound_trunk(
        livekit_api.ListSIPOutboundTrunkRequest()
    )
    expected_address = f"{OUTBOUND_HOST}:5060"
    for trunk in resp.items:
        if trunk.name == OUTBOUND_TRUNK_NAME:
            # 检查 numbers 和 address 是否与当前配置一致
            need_rebuild = False
            if OUTBOUND_NUMBER not in trunk.numbers:
                logger.warning(
                    "Outbound Trunk numbers 不一致: 当前=%s, 期望=%s，需要重建",
                    trunk.numbers, [OUTBOUND_NUMBER],
                )
                need_rebuild = True
            if trunk.address != expected_address:
                logger.warning(
                    "Outbound Trunk address 不一致: 当前=%s, 期望=%s，需要重建",
                    trunk.address, expected_address,
                )
                need_rebuild = True
            if need_rebuild:
                await lk.sip.delete_sip_trunk(
                    livekit_api.DeleteSIPTrunkRequest(
                        sip_trunk_id=trunk.sip_trunk_id,
                    )
                )
                logger.info("旧 Outbound Trunk 已删除: id=%s", trunk.sip_trunk_id)
                break  # 退出循环，继续创建新 Trunk
            logger.info("Outbound Trunk 已存在: %s (id=%s, address=%s)，跳过创建", trunk.name, trunk.sip_trunk_id, trunk.address)
            return trunk.sip_trunk_id

    logger.info("创建 Outbound SIP Trunk: name=%s, address=%s:5060", OUTBOUND_TRUNK_NAME, OUTBOUND_HOST)
    trunk = await lk.sip.create_sip_outbound_trunk(
        livekit_api.CreateSIPOutboundTrunkRequest(
            trunk=livekit_api.SIPOutboundTrunkInfo(
                name=OUTBOUND_TRUNK_NAME,
                address=f"{OUTBOUND_HOST}:5060",
                numbers=[OUTBOUND_NUMBER],
            )
        )
    )
    logger.info("Outbound Trunk 创建成功: id=%s, numbers=%s", trunk.sip_trunk_id, [OUTBOUND_NUMBER])
    return trunk.sip_trunk_id


# ---------------------------------------------------------------------------
# Inbound Trunk
# ---------------------------------------------------------------------------
async def _ensure_inbound_trunk(lk: livekit_api.LiveKitAPI) -> str:
    """检查并创建 Inbound SIP Trunk，返回 trunk_id。

    如果同名 Trunk 已存在但 numbers 不同，先删除再重建。
    """
    logger.info("检查 Inbound SIP Trunk ...")

    resp = await lk.sip.list_sip_inbound_trunk(
        livekit_api.ListSIPInboundTrunkRequest()
    )
    for trunk in resp.items:
        if trunk.name == INBOUND_TRUNK_NAME:
            # 检查 numbers 是否与当前配置一致
            if OUTBOUND_NUMBER not in trunk.numbers:
                logger.warning(
                    "Inbound Trunk numbers 不一致: 当前=%s, 期望=%s，删除并重建",
                    trunk.numbers, [OUTBOUND_NUMBER],
                )
                await lk.sip.delete_sip_trunk(
                    livekit_api.DeleteSIPTrunkRequest(
                        sip_trunk_id=trunk.sip_trunk_id,
                    )
                )
                logger.info("旧 Inbound Trunk 已删除: id=%s", trunk.sip_trunk_id)
                break  # 退出循环，继续创建新 Trunk
            logger.info("Inbound Trunk 已存在: %s (id=%s)，跳过创建", trunk.name, trunk.sip_trunk_id)
            return trunk.sip_trunk_id

    logger.info("创建 Inbound SIP Trunk: name=%s, numbers=%s", INBOUND_TRUNK_NAME, [OUTBOUND_NUMBER])
    trunk = await lk.sip.create_sip_inbound_trunk(
        livekit_api.CreateSIPInboundTrunkRequest(
            trunk=livekit_api.SIPInboundTrunkInfo(
                name=INBOUND_TRUNK_NAME,
                numbers=[OUTBOUND_NUMBER],
                allowed_addresses=["0.0.0.0/0"],  # 开发环境允许所有来源
            )
        )
    )
    logger.info("Inbound Trunk 创建成功: id=%s, numbers=%s", trunk.sip_trunk_id, [OUTBOUND_NUMBER])
    return trunk.sip_trunk_id


# ---------------------------------------------------------------------------
# Dispatch Rule
# ---------------------------------------------------------------------------
async def _ensure_dispatch_rule(lk: livekit_api.LiveKitAPI, inbound_trunk_id: str) -> str:
    """检查并创建 SIP Dispatch Rule，返回 rule_id。

    如果同名 Rule 已存在但 trunk_ids 不匹配，先删除再重建。
    """
    logger.info("检查 SIP Dispatch Rule ...")

    resp = await lk.sip.list_sip_dispatch_rule(
        livekit_api.ListSIPDispatchRuleRequest()
    )
    for rule in resp.items:
        if rule.name == DISPATCH_RULE_NAME:
            # 检查 trunk_ids 是否包含当前 inbound_trunk_id
            if inbound_trunk_id not in rule.trunk_ids:
                logger.warning(
                    "Dispatch Rule trunk_ids 不一致: 当前=%s, 期望包含=%s，删除并重建",
                    rule.trunk_ids, [inbound_trunk_id],
                )
                await lk.sip.delete_sip_dispatch_rule(
                    livekit_api.DeleteSIPDispatchRuleRequest(
                        sip_dispatch_rule_id=rule.sip_dispatch_rule_id,
                    )
                )
                logger.info("旧 Dispatch Rule 已删除: id=%s", rule.sip_dispatch_rule_id)
                break  # 退出循环，继续创建新 Rule
            logger.info("Dispatch Rule 已存在: %s (id=%s)，跳过创建", rule.name, rule.sip_dispatch_rule_id)
            return rule.sip_dispatch_rule_id

    logger.info("创建 SIP Dispatch Rule: name=%s, trunk_ids=%s", DISPATCH_RULE_NAME, [inbound_trunk_id])
    rule = await lk.sip.create_sip_dispatch_rule(
        livekit_api.CreateSIPDispatchRuleRequest(
            rule=livekit_api.SIPDispatchRule(
                dispatch_rule_direct=livekit_api.SIPDispatchRuleDirect(
                    room_name="",  # 空字符串表示自动创建新 Room
                    pin="",
                ),
            ),
            trunk_ids=[inbound_trunk_id],
            name=DISPATCH_RULE_NAME,
        )
    )
    logger.info("Dispatch Rule 创建成功: id=%s, trunk_ids=%s", rule.sip_dispatch_rule_id, [inbound_trunk_id])
    return rule.sip_dispatch_rule_id


# ---------------------------------------------------------------------------
# 输出汇总
# ---------------------------------------------------------------------------
def _print_summary(outbound_id: str, inbound_id: str, dispatch_id: str) -> None:
    """打印初始化结果摘要。"""
    print()
    print("=" * 52)
    print("  SIP Trunk 初始化完成！")
    print("=" * 52)
    print(f"  Outbound Trunk ID : {outbound_id}")
    print(f"  Inbound Trunk ID  : {inbound_id}")
    print(f"  Dispatch Rule ID  : {dispatch_id}")
    print()
    print("  请将以下值填入 .env 文件（如尚未配置）：")
    print(f"  LK_SIP_TRUNK_ID={outbound_id}")
    print(f"  LK_SIP_OUTBOUND_NUMBER={OUTBOUND_NUMBER}")
    print(f"  LK_SIP_DOMAIN={OUTBOUND_HOST}")
    print("=" * 52)
    print()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def main() -> None:
    await wait_for_livekit()
    await init_sip_trunk()


if __name__ == "__main__":
    asyncio.run(main())
