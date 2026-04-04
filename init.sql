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
