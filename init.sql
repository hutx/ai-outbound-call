-- ============================================================
-- 智能外呼系统 — 数据库初始化脚本
-- PostgreSQL 14+
-- 首次启动时由 docker-compose 自动执行
-- 后续变更请使用 Alembic 迁移
-- ============================================================

-- ── 扩展 ─────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";  -- UUID 生成
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";  -- SQL 性能分析（可选）

-- ── 通话记录表（CDR） ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS call_records (
    id               SERIAL          PRIMARY KEY,
    uuid             VARCHAR(64)     NOT NULL UNIQUE,
    task_id          VARCHAR(64)     NOT NULL,
    phone_number     VARCHAR(20)     NOT NULL,
    script_id        VARCHAR(64)     NOT NULL DEFAULT 'finance_product_a',

    -- 状态与结果
    state            VARCHAR(20),
    intent           VARCHAR(30),
    result           VARCHAR(30),

    -- 外呼结果详情（SIP 层面的原始反馈）
    sip_code         INTEGER,          -- SIP 响应码：403/480/200 等
    hangup_cause     VARCHAR(64),      -- FreeSWITCH 挂断原因：CALL_REJECTED / NO_ANSWER / NO_USER_RESPONSE 等
    dial_attempts    INTEGER    NOT NULL DEFAULT 0,  -- 外呼尝试次数

    -- 时间戳
    created_at       TIMESTAMP       NOT NULL DEFAULT NOW(),
    answered_at      TIMESTAMP,
    ended_at         TIMESTAMP,
    duration_seconds INTEGER,

    -- 对话统计
    user_utterances  INTEGER         NOT NULL DEFAULT 0,
    ai_utterances    INTEGER         NOT NULL DEFAULT 0,

    -- 录音
    recording_path   VARCHAR(512),

    -- JSON 数据
    messages         JSONB,          -- 对话历史（最近 30 条）
    customer_info    JSONB           -- 客户快照
);

-- 常用查询索引
CREATE INDEX IF NOT EXISTS idx_cdr_task_id     ON call_records(task_id);
CREATE INDEX IF NOT EXISTS idx_cdr_phone       ON call_records(phone_number);
CREATE INDEX IF NOT EXISTS idx_cdr_result      ON call_records(result);
CREATE INDEX IF NOT EXISTS idx_cdr_intent      ON call_records(intent);
CREATE INDEX IF NOT EXISTS idx_cdr_created_at  ON call_records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cdr_answered_at ON call_records(answered_at DESC)
    WHERE answered_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cdr_sip_code    ON call_records(sip_code);
CREATE INDEX IF NOT EXISTS idx_cdr_hangup      ON call_records(hangup_cause);


-- ── 黑名单表 ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS blacklist (
    id         SERIAL       PRIMARY KEY,
    phone      VARCHAR(20)  NOT NULL UNIQUE,
    reason     VARCHAR(256),
    created_at TIMESTAMP    NOT NULL DEFAULT NOW(),
    created_by VARCHAR(64)  DEFAULT 'system'   -- 操作来源：system/api/agent
);

CREATE INDEX IF NOT EXISTS idx_blacklist_phone ON blacklist(phone);


-- ── 回拨计划表 ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS callback_schedules (
    id            SERIAL       PRIMARY KEY,
    phone         VARCHAR(20)  NOT NULL,
    task_id       VARCHAR(64),
    callback_time VARCHAR(32),              -- ISO 格式时间字符串
    note          VARCHAR(256),
    status        VARCHAR(20)  NOT NULL DEFAULT 'pending',  -- pending/done/cancelled
    created_at    TIMESTAMP    NOT NULL DEFAULT NOW(),
    executed_at   TIMESTAMP                -- 实际执行时间
);

CREATE INDEX IF NOT EXISTS idx_callback_phone  ON callback_schedules(phone);
CREATE INDEX IF NOT EXISTS idx_callback_status ON callback_schedules(status)
    WHERE status = 'pending';


-- ── 外呼任务表（持久化任务元数据）────────────────────────────
CREATE TABLE IF NOT EXISTS outbound_tasks (
    id               SERIAL        PRIMARY KEY,
    task_id          VARCHAR(64)   NOT NULL UNIQUE,
    name             VARCHAR(128)  NOT NULL,
    script_id        VARCHAR(64)   NOT NULL,
    status           VARCHAR(20)   NOT NULL DEFAULT 'pending',
                                   -- pending/running/paused/completed/cancelled/failed
    caller_id        VARCHAR(20)   DEFAULT '',
    concurrent_limit INTEGER       NOT NULL DEFAULT 5,
    max_retries      INTEGER       NOT NULL DEFAULT 1,

    -- 统计（实时更新）
    total_count      INTEGER       NOT NULL DEFAULT 0,
    completed_count  INTEGER       NOT NULL DEFAULT 0,
    connected_count  INTEGER       NOT NULL DEFAULT 0,
    failed_count     INTEGER       NOT NULL DEFAULT 0,

    -- 时间
    created_at       TIMESTAMP     NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMP,
    completed_at     TIMESTAMP,

    -- 号码列表快照（便于重启恢复）
    phone_numbers    JSONB
);

CREATE INDEX IF NOT EXISTS idx_tasks_status     ON outbound_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON outbound_tasks(created_at DESC);


-- ── 操作审计日志 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL    PRIMARY KEY,
    ts         TIMESTAMP    NOT NULL DEFAULT NOW(),
    action     VARCHAR(64)  NOT NULL,   -- create_task / pause_task / add_blacklist ...
    actor      VARCHAR(64)  DEFAULT 'system',
    target_id  VARCHAR(64),             -- task_id / phone ...
    detail     JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);


-- ── 统计视图（快速查询 KPI）──────────────────────────────────

-- 按任务汇总接通率、意向率
CREATE OR REPLACE VIEW vw_task_stats AS
SELECT
    task_id,
    COUNT(*)                                                        AS total_calls,
    COUNT(*) FILTER (WHERE answered_at IS NOT NULL)                 AS connected_calls,
    COUNT(*) FILTER (WHERE intent IN ('interested','high','medium')) AS intent_calls,
    COUNT(*) FILTER (WHERE result = 'transferred')                  AS transferred_calls,
    COUNT(*) FILTER (WHERE result = 'completed')                    AS completed_calls,
    ROUND(
        COUNT(*) FILTER (WHERE answered_at IS NOT NULL)::NUMERIC
        / NULLIF(COUNT(*), 0) * 100, 1
    )                                                               AS connect_rate_pct,
    ROUND(
        COUNT(*) FILTER (WHERE intent IN ('interested','high','medium'))::NUMERIC
        / NULLIF(COUNT(*) FILTER (WHERE answered_at IS NOT NULL), 0) * 100, 1
    )                                                               AS intent_rate_pct,
    ROUND(
        AVG(duration_seconds) FILTER (WHERE duration_seconds > 0), 0
    )                                                               AS avg_duration_sec,
    MIN(created_at)                                                 AS first_call_at,
    MAX(created_at)                                                 AS last_call_at
FROM call_records
GROUP BY task_id;

-- 今日汇总（Dashboard 实时刷新用）
CREATE OR REPLACE VIEW vw_today_stats AS
SELECT
    COUNT(*)                                                        AS total_today,
    COUNT(*) FILTER (WHERE answered_at IS NOT NULL)                 AS connected_today,
    COUNT(*) FILTER (WHERE intent IN ('interested','high','medium')) AS intent_today,
    COUNT(*) FILTER (WHERE result = 'transferred')                  AS transferred_today,
    ROUND(
        COUNT(*) FILTER (WHERE answered_at IS NOT NULL)::NUMERIC
        / NULLIF(COUNT(*), 0) * 100, 1
    )                                                               AS connect_rate_pct,
    ROUND(AVG(duration_seconds) FILTER (WHERE duration_seconds > 0), 0) AS avg_duration_sec
FROM call_records
WHERE created_at >= CURRENT_DATE;


-- ── 函数：更新任务计数 ─────────────────────────────────────
-- 通话完成时调用（替代应用层多次 UPDATE 的竞态问题）
CREATE OR REPLACE FUNCTION fn_update_task_on_call_finish(
    p_task_id  VARCHAR,
    p_result   VARCHAR,  -- 'completed'/'transferred'/'error'/'not_answered'
    p_connected BOOLEAN  -- true = 通话接通过
)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    UPDATE outbound_tasks SET
        completed_count = completed_count + 1,
        connected_count = connected_count + CASE WHEN p_connected THEN 1 ELSE 0 END,
        failed_count    = failed_count    + CASE WHEN p_result IN ('error','not_answered') THEN 1 ELSE 0 END
    WHERE task_id = p_task_id;
END;
$$;


-- ── 初始种子数据 ──────────────────────────────────────────────

-- 话术脚本表
CREATE TABLE IF NOT EXISTS call_scripts (
    id               SERIAL        PRIMARY KEY,
    script_id        VARCHAR(64)   NOT NULL UNIQUE,
    name             VARCHAR(128)  NOT NULL,
    description      VARCHAR(512),
    script_type      VARCHAR(30)   NOT NULL DEFAULT 'financial', -- financial/insurance/loan
    opening_script   TEXT          NOT NULL,                     -- 开场白话术
    opening_pause    INTEGER       NOT NULL DEFAULT 2000,        -- 开场白后停顿时长(毫秒)
    main_script      JSONB         NOT NULL,                     -- 主要话术内容
    objection_handling JSONB       NOT NULL DEFAULT '{}',        -- 异议处理话术
    closing_script   TEXT,                                       -- 结束语话术
    created_at       TIMESTAMP     NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP     NOT NULL DEFAULT NOW(),
    is_active        BOOLEAN       NOT NULL DEFAULT true
);

-- 创建更新时间戳触发器函数
CREATE OR REPLACE FUNCTION trigger_set_timestamp()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 为话术表添加更新时间戳触发器
CREATE TRIGGER set_call_scripts_updated_at
    BEFORE UPDATE ON call_scripts
    FOR EACH ROW
    EXECUTE FUNCTION trigger_set_timestamp();

-- 插入初始话术数据
INSERT INTO call_scripts (script_id, name, description, script_type, opening_script, opening_pause, main_script, objection_handling, closing_script)
VALUES
(
    'finance_product_a',
    '理财产品推广',
    '银行理财产品推广话术，面向储蓄型客户推荐稳健型理财产品',
    'financial',
    '您好，我是XX银行的智能客服小智，本通话由人工智能完成。请问您现在方便说话吗？',
    2000,
    '{
        "product_name": "稳享理财A款",
        "product_desc": "年化收益率3.8%，起投1万元，T+1到账，无手续费",
        "target_customer": "有理财需求的储蓄型客户",
        "key_selling_points": [
            "收益稳定，高于普通存款",
            "灵活申赎，资金不被锁定",
            "银行保本，安全有保障"
        ]
    }',
    '{
        "利率太低": "相比活期存款年化0.35%，我们的3.8%已经是市场上同类产品中较高的，而且保本保息",
        "不需要": "完全理解，请问您目前是有其他理财渠道，还是暂时不考虑投资？",
        "没钱": "没关系，我们起投门槛只有1万元，而且随时可以赎回"
    }',
    '好的，如果您有任何问题随时可以联系我们，祝您生活愉快，再见！'
),
(
    'insurance_renewal',
    '保险续保提醒',
    '保险产品续保提醒话术，面向即将到期的保险客户推送续保通知',
    'insurance',
    '您好，我是XX保险公司的智能客服，本通话由人工智能完成。想提醒您保单即将到期，请问您现在方便说话吗？',
    1500,
    '{
        "product_name": "人寿险续保提醒",
        "product_desc": "您的保单即将到期，续保享受老客户专属折扣",
        "target_customer": "即将到期的保险客户",
        "key_selling_points": [
            "老客户续保享9折优惠",
            "无需重新体检",
            "保障范围升级"
        ]
    }',
    '{
        "不想续了": "请问是对哪方面不满意？我们可以为您推荐更适合的方案",
        "太贵了": "老客户专属折扣后价格很优惠，而且保障金额也提升了",
        "需要考虑": "没问题，您可以先了解一下我们的续保政策，有问题随时联系"
    }',
    '好的，感谢您的配合，如果有任何疑问请随时联系我们，再见！'
),
(
    'loan_followup',
    '贷款回访',
    '消费贷款产品回访话术，面向曾咨询过贷款的潜在客户',
    'financial',
    '您好，我是XX银行的智能客服小智，之前您咨询过我们的消费贷款产品，现在利率有所下调，请问您现在方便说话吗？',
    2500,
    '{
        "product_name": "消费贷款回访",
        "product_desc": "您之前询问过我们的消费贷款，现在利率有所下调",
        "target_customer": "曾咨询过贷款的潜在客户",
        "key_selling_points": [
            "年化利率仅3.6%",
            "最高可贷50万",
            "最快当天放款"
        ]
    }',
    '{
        "不需要了": "好的，如果以后有资金需求欢迎联系我们",
        "利率高": "我们目前是市场上利率最低的产品之一，您方便说说您期望的利率是多少吗",
        "资料复杂": "其实我们的申请流程非常简单，线上即可完成，我可以为您介绍一下"
    }',
    '好的，如果您改变主意了随时可以联系我们，祝您工作顺利，再见！'
),
(
    'marketing_event',
    '营销活动推广',
    '商城促销活动及品牌营销活动推广话术',
    'marketing',
    '您好，我是XX商城的智能客服，本通话由人工智能完成。我们正在进行年中大促活动，请问您现在方便了解一下吗？',
    2000,
    '{
        "product_name": "年中大促活动",
        "product_desc": "全场低至3折起，满减优惠叠加券限时领取",
        "target_customer": "近90天有消费记录的活跃用户",
        "key_selling_points": [
            "全场低至3折起",
            "满200减100叠加优惠券",
            "限时3天，错过等一年"
        ]
    }',
    '{
        "没兴趣": "我们这次是针对老客户专属的活动，有很多热门品牌参与，可以看看有没有需要的",
        "太忙了": "只需要占用您30秒，这次活动力度真的很大，您可以关注下",
        "已经买过了": "太感谢您的支持了，我们还有返场券可以领取，下次购物更划算"
    }',
    '好的，感谢您的接听，活动详情请查看我们发送的短信，祝您生活愉快，再见！'
);

-- 全局黑名单（DO NOT CALL 清单，系统默认保护号码）
-- 生产环境替换为真实需保护的号码
INSERT INTO blacklist (phone, reason, created_by)
VALUES
    ('10000', '运营商测试号', 'system'),
    ('10086', '运营商客服', 'system'),
    ('10010', '运营商客服', 'system'),
    ('10011', '运营商客服', 'system'),
    ('12315', '政府投诉热线', 'system'),
    ('12321', '垃圾信息举报', 'system')
ON CONFLICT (phone) DO NOTHING;


-- ── 权限（生产环境按需配置）─────────────────────────────────
-- 只读账号（BI / 报表工具）
-- CREATE ROLE outbound_readonly;
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO outbound_readonly;
-- GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO outbound_readonly;

-- 应用账号（最小权限）
-- CREATE ROLE outbound_app;
-- GRANT SELECT, INSERT, UPDATE ON call_records, blacklist, callback_schedules, outbound_tasks, audit_log TO outbound_app;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO outbound_app;
-- GRANT SELECT ON vw_task_stats, vw_today_stats TO outbound_app;
-- GRANT EXECUTE ON FUNCTION fn_update_task_on_call_finish TO outbound_app;
