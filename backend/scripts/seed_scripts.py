"""
话术脚本初始化数据
从数据库直接插入种子数据
"""
import asyncio
import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.models.call_script import CallScript
from backend.utils.session_manager import session_manager

logger = logging.getLogger(__name__)

# ============================================================
# 话术种子数据
# ============================================================
SEED_SCRIPTS = [
    {
        "script_id": "finance_product_a",
        "name": "理财产品推广",
        "description": "银行理财产品推广话术，面向储蓄型客户推荐稳健型理财产品",
        "script_type": "financial",
        "opening_script": "您好，我是XX银行的智能客服小智，本通话由人工智能完成。请问您现在方便说话吗？",
        "opening_pause": 2000,
        "main_script": {
            "product_name": "稳享理财A款",
            "product_desc": "年化收益率3.8%，起投1万元，T+1到账，无手续费",
            "target_customer": "有理财需求的储蓄型客户",
            "key_selling_points": [
                "收益稳定，高于普通存款",
                "灵活申赎，资金不被锁定",
                "银行保本，安全有保障"
            ]
        },
        "objection_handling": {
            "利率太低": "相比活期存款年化0.35%，我们的3.8%已经是市场上同类产品中较高的，而且保本保息",
            "不需要": "完全理解，请问您目前是有其他理财渠道，还是暂时不考虑投资？",
            "没钱": "没关系，我们起投门槛只有1万元，而且随时可以赎回"
        },
        "closing_script": "好的，如果您有任何问题随时可以联系我们，祝您生活愉快，再见！"
    },
    {
        "script_id": "insurance_renewal",
        "name": "保险续保提醒",
        "description": "保险产品续保提醒话术，面向即将到期的保险客户推送续保通知",
        "script_type": "insurance",
        "opening_script": "您好，我是XX保险公司的智能客服，本通话由人工智能完成。想提醒您保单即将到期，请问您现在方便说话吗？",
        "opening_pause": 1500,
        "main_script": {
            "product_name": "人寿险续保提醒",
            "product_desc": "您的保单即将到期，续保享受老客户专属折扣",
            "target_customer": "即将到期的保险客户",
            "key_selling_points": [
                "老客户续保享9折优惠",
                "无需重新体检",
                "保障范围升级"
            ]
        },
        "objection_handling": {
            "不想续了": "请问是对哪方面不满意？我们可以为您推荐更适合的方案",
            "太贵了": "老客户专属折扣后价格很优惠，而且保障金额也提升了",
            "需要考虑": "没问题，您可以先了解一下我们的续保政策，有问题随时联系"
        },
        "closing_script": "好的，感谢您的配合，如果有任何疑问请随时联系我们，再见！"
    },
    {
        "script_id": "loan_followup",
        "name": "贷款回访",
        "description": "消费贷款产品回访话术，面向曾咨询过贷款的潜在客户",
        "script_type": "financial",
        "opening_script": "您好，我是XX银行的智能客服小智，之前您咨询过我们的消费贷款产品，现在利率有所下调，请问您现在方便说话吗？",
        "opening_pause": 2500,
        "main_script": {
            "product_name": "消费贷款回访",
            "product_desc": "您之前询问过我们的消费贷款，现在利率有所下调",
            "target_customer": "曾咨询过贷款的潜在客户",
            "key_selling_points": [
                "年化利率仅3.6%",
                "最高可贷50万",
                "最快当天放款"
            ]
        },
        "objection_handling": {
            "不需要了": "好的，如果以后有资金需求欢迎联系我们",
            "利率高": "我们目前是市场上利率最低的产品之一，您方便说说您期望的利率是多少吗",
            "资料复杂": "其实我们的申请流程非常简单，线上即可完成，我可以为您介绍一下"
        },
        "closing_script": "好的，如果您改变主意了随时可以联系我们，祝您工作顺利，再见！"
    },
    {
        "script_id": "marketing_event",
        "name": "营销活动推广",
        "description": "商城促销活动及品牌营销活动推广话术",
        "script_type": "marketing",
        "opening_script": "您好，我是XX商城的智能客服，本通话由人工智能完成。我们正在进行年中大促活动，请问您现在方便了解一下吗？",
        "opening_pause": 2000,
        "main_script": {
            "product_name": "年中大促活动",
            "product_desc": "全场低至3折起，满减优惠叠加券限时领取",
            "target_customer": "近90天有消费记录的活跃用户",
            "key_selling_points": [
                "全场低至3折起",
                "满200减100叠加优惠券",
                "限时3天，错过等一年"
            ]
        },
        "objection_handling": {
            "没兴趣": "我们这次是针对老客户专属的活动，有很多热门品牌参与，可以看看有没有需要的",
            "太忙了": "只需要占用您30秒，这次活动力度真的很大，您可以关注下",
            "已经买过了": "太感谢您的支持了，我们还有返场券可以领取，下次购物更划算"
        },
        "closing_script": "好的，感谢您的接听，活动详情请查看我们发送的短信，祝您生活愉快，再见！"
    }
]


async def seed_scripts(force: bool = False) -> list[str]:
    """
    插入话术种子数据

    Args:
        force: True=覆盖已有数据, False=跳过已存在的

    Returns:
        已成功插入/更新的 script_id 列表
    """
    results = []

    async with session_manager.get_session() as session:
        for seed in SEED_SCRIPTS:
            sid = seed["script_id"]

            # 检查是否已存在
            stmt = select(CallScript).where(CallScript.script_id == sid)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                if force:
                    # 覆盖更新
                    for key, value in seed.items():
                        if key == "main_script":
                            setattr(existing, key, json.dumps(value, ensure_ascii=False))
                        elif key == "objection_handling":
                            setattr(existing, key, json.dumps(value, ensure_ascii=False))
                        else:
                            setattr(existing, key, value)
                    results.append(f"[更新] {sid} - {seed['name']}")
                    logger.info(f"已更新话术: {sid}")
                else:
                    results.append(f"[跳过] {sid} - 已存在")
                    logger.info(f"跳过已存在的话术: {sid}")
                continue

            # 插入新数据
            new_script = CallScript(
                script_id=seed["script_id"],
                name=seed["name"],
                description=seed["description"],
                script_type=seed["script_type"],
                opening_script=seed["opening_script"],
                opening_pause=seed["opening_pause"],
                main_script=json.dumps(seed["main_script"], ensure_ascii=False),
                objection_handling=json.dumps(seed["objection_handling"], ensure_ascii=False),
                closing_script=seed.get("closing_script"),
                is_active=True
            )
            session.add(new_script)
            results.append(f"[新增] {sid} - {seed['name']}")
            logger.info(f"已新增话术: {sid}")

        await session.commit()

    return results


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="初始化话术脚本种子数据")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的数据")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    print("=" * 50)
    print("话术脚本初始化")
    print("=" * 50)

    results = await seed_scripts(force=args.force)
    for r in results:
        print(f"  {r}")

    print(f"\n完成: {len(results)} 条记录已处理")


if __name__ == "__main__":
    asyncio.run(main())
