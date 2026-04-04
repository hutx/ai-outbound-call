"""
CRM 服务 — 生产级
──────────────────
- 黑名单持久化到数据库（进程重启不丢失）
- 启动时从 DB 预热内存缓存（查询走内存，写入双写）
- 客户信息查询接口（可对接实际 CRM API）
- 短信发送（阿里云/腾讯云 SDK stub，生产替换）
- 回拨计划写入数据库
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── 内存缓存（启动时从 DB 预热） ─────────────────────────────
_BLACKLIST: set = set()    # 黑名单号码集合（内存缓存）
_INTENTS: dict = {}        # 意向记录 {phone: {level, uuid, note}}

# ── 示例客户数据（生产环境替换为 CRM API 调用） ──────────────
_MOCK_CUSTOMERS: dict = {
    "13800138000": {
        "name": "张三", "age": 35, "balance": 120000,
        "risk_level": "stable",
        "note": "曾咨询过定期理财，对收益率较敏感",
        "tags": ["存量客户", "理财意向"],
    },
    "13912345678": {
        "name": "李四", "age": 42, "balance": 50000,
        "risk_level": "conservative",
        "note": "老客户，偏好保守型产品",
        "tags": ["存量客户"],
    },
}


class CRMService:
    """
    CRM 工具调用服务
    生产环境：将 _MOCK_CUSTOMERS 的查询替换为 HTTP 调用实际 CRM API
    """

    async def startup(self):
        """应用启动时调用，从数据库预热黑名单缓存"""
        global _BLACKLIST
        try:
            from backend.utils.db import db_load_blacklist
            _BLACKLIST = await db_load_blacklist()
            logger.info(f"黑名单已从数据库加载: {len(_BLACKLIST)} 个号码")
        except Exception as e:
            logger.warning(f"黑名单预热失败（数据库未就绪），使用空缓存: {e}")
            _BLACKLIST = set()

    # ── 客户信息查询 ─────────────────────────────────────────

    async def query_customer_info(self, phone: str) -> dict:
        """
        查询客户信息
        生产环境替换为：
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{CRM_API}/customers/{phone}", headers=...)
                return r.json()
        """
        if self.is_blacklisted(phone):
            logger.info(f"[CRM] 黑名单号码: {phone}")
            return {"blacklisted": True, "found": False, "name": "", "note": ""}

        customer = _MOCK_CUSTOMERS.get(phone)
        if customer:
            logger.debug(f"[CRM] 查询客户: {phone} → {customer['name']}")
            return {
                "found":      True,
                "blacklisted": False,
                "name":       customer["name"],
                "risk_level": customer.get("risk_level", "unknown"),
                "note":       customer.get("note", ""),
                "tags":       customer.get("tags", []),
            }

        logger.debug(f"[CRM] 未知客户: {phone}")
        return {"found": False, "blacklisted": False, "name": "", "risk_level": "unknown",
                "note": "", "tags": []}

    # ── 黑名单管理 ───────────────────────────────────────────

    def is_blacklisted(self, phone: str) -> bool:
        """O(1) 内存查询"""
        return phone in _BLACKLIST

    async def add_to_blacklist(self, phone: str, reason: str = "用户拒绝") -> bool:
        """加入黑名单（内存 + DB 双写）"""
        _BLACKLIST.add(phone)
        logger.info(f"[CRM] 加入黑名单: {phone}，原因: {reason}")
        try:
            from backend.utils.db import db_add_blacklist
            await db_add_blacklist(phone, reason)
        except Exception as e:
            logger.error(f"[CRM] 黑名单写入 DB 失败（内存已更新）: {e}")
        return True

    async def remove_from_blacklist(self, phone: str) -> bool:
        """从黑名单移除"""
        _BLACKLIST.discard(phone)
        try:
            from backend.utils.db import db_remove_blacklist
            await db_remove_blacklist(phone)
        except Exception as e:
            logger.error(f"[CRM] 黑名单从 DB 移除失败: {e}")
        return True

    async def list_blacklist(self) -> list:
        try:
            from backend.utils.db import db_list_blacklist
            return await db_list_blacklist()
        except Exception:
            return [{"phone": p, "reason": ""}  for p in sorted(_BLACKLIST)]

    # ── 意向记录 ─────────────────────────────────────────────

    async def record_intent(
        self,
        phone: str,
        intent_level: str,
        call_uuid: str,
        note: str = "",
    ) -> bool:
        """
        记录客户意向等级
        intent_level: high / medium / low / none
        生产环境：调用 CRM API 触发销售跟进流程
        """
        _INTENTS[phone] = {
            "level":       intent_level,
            "uuid":        call_uuid,
            "note":        note,
            "recorded_at": datetime.now().isoformat(),
        }
        logger.info(f"[CRM] 意向记录: {phone} → {intent_level}")
        # 生产环境：
        # await crm_api.update_lead(phone, {"intent": intent_level, "call_id": call_uuid})
        return True

    # ── 回拨计划 ─────────────────────────────────────────────

    async def schedule_callback(
        self,
        phone: str,
        task_id: str = "",
        callback_time: Optional[str] = None,
        note: str = "",
    ) -> dict:
        """预约回拨，写入数据库"""
        if not callback_time:
            callback_time = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        try:
            from backend.utils.db import db_save_callback
            await db_save_callback(phone, task_id, callback_time, note)
        except Exception as e:
            logger.error(f"[CRM] 回拨计划写入失败: {e}")
        logger.info(f"[CRM] 回拨预约: {phone} → {callback_time}")
        return {"success": True, "callback_time": callback_time}

    # ── 短信发送 ─────────────────────────────────────────────

    async def send_sms(
        self,
        phone: str,
        template_id: str,
        params: Optional[dict] = None,
        sign_name: str = "XX银行",
    ) -> bool:
        """
        发送短信
        ★ 生产环境取消注释并填入真实 AccessKey：

        import json
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_dysmsapi20170525 import models as sms_models
        from alibabacloud_tea_openapi import models as open_api_models

        cfg = open_api_models.Config(
            access_key_id=os.environ['ALI_ACCESS_KEY_ID'],
            access_key_secret=os.environ['ALI_ACCESS_KEY_SECRET'],
        )
        cfg.endpoint = 'dysmsapi.aliyuncs.com'
        client = Client(cfg)
        req = sms_models.SendSmsRequest(
            phone_numbers=phone,
            sign_name=sign_name,
            template_code=template_id,
            template_param=json.dumps(params or {}),
        )
        resp = client.send_sms(req)
        return resp.body.code == "OK"
        """
        params = params or {}
        logger.info(f"[CRM] 短信发送（stub）: {phone} tmpl={template_id} params={params}")
        # TODO: 接入真实短信 SDK
        return True


# 全局单例
crm = CRMService()
