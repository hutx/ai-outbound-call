-- LiveKit 智能外呼系统数据库初始化
-- 在现有 PostgreSQL 实例中创建新的 schema

-- 话术配置表
CREATE TABLE IF NOT EXISTS lk_scripts (
    id SERIAL PRIMARY KEY,
    script_id VARCHAR(64) UNIQUE NOT NULL,
    name VARCHAR(128) NOT NULL,
    description TEXT DEFAULT '',
    script_type VARCHAR(32) DEFAULT 'general',

    -- 开场白/结束语
    opening_text TEXT DEFAULT '',
    opening_pause_ms INTEGER DEFAULT 2000,
    main_prompt TEXT NOT NULL,
    closing_text TEXT DEFAULT '感谢您的接听，再见！',

    -- 打断配置
    barge_in_opening BOOLEAN DEFAULT FALSE,
    barge_in_conversation BOOLEAN DEFAULT TRUE,
    barge_in_closing BOOLEAN DEFAULT FALSE,
    barge_in_protect_start_sec REAL DEFAULT 1.0,
    barge_in_protect_end_sec REAL DEFAULT 1.0,

    -- 宽容期配置
    tolerance_enabled BOOLEAN DEFAULT TRUE,
    tolerance_ms INTEGER DEFAULT 1000,

    -- 无响应配置
    no_response_timeout_sec INTEGER DEFAULT 5,
    no_response_mode VARCHAR(16) DEFAULT 'consecutive',
    no_response_max_count INTEGER DEFAULT 3,
    no_response_prompt TEXT DEFAULT '您好，请问您还在吗？',
    no_response_hangup_text TEXT DEFAULT '感谢您的时间，再见！',

    -- 状态
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 外呼任务表
CREATE TABLE IF NOT EXISTS lk_tasks (
    id SERIAL PRIMARY KEY,
    task_id VARCHAR(64) UNIQUE NOT NULL,
    name VARCHAR(128) NOT NULL,
    script_id VARCHAR(64) NOT NULL REFERENCES lk_scripts(script_id),
    status VARCHAR(16) DEFAULT 'pending',
    concurrent_limit INTEGER DEFAULT 5,
    max_retries INTEGER DEFAULT 1,
    total_phones INTEGER DEFAULT 0,
    completed_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 任务号码表
CREATE TABLE IF NOT EXISTS lk_task_phones (
    id SERIAL PRIMARY KEY,
    task_id VARCHAR(64) NOT NULL REFERENCES lk_tasks(task_id),
    phone VARCHAR(32) NOT NULL,
    status VARCHAR(16) DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    last_call_id VARCHAR(128),
    last_error TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_phones_task_id ON lk_task_phones(task_id);
CREATE INDEX IF NOT EXISTS idx_task_phones_status ON lk_task_phones(task_id, status);

-- 通话记录表 (CDR)
CREATE TABLE IF NOT EXISTS lk_call_records (
    id SERIAL PRIMARY KEY,
    call_id VARCHAR(128) UNIQUE NOT NULL,
    task_id VARCHAR(64) REFERENCES lk_tasks(task_id),
    phone VARCHAR(32) NOT NULL,
    script_id VARCHAR(64) REFERENCES lk_scripts(script_id),
    status VARCHAR(32) DEFAULT 'initiating',
    intent VARCHAR(32) DEFAULT 'unknown',
    result VARCHAR(32) DEFAULT '',
    duration_sec REAL DEFAULT 0,
    user_talk_time_sec REAL DEFAULT 0,
    ai_talk_time_sec REAL DEFAULT 0,
    rounds INTEGER DEFAULT 0,
    transcript JSONB DEFAULT '[]'::jsonb,
    sip_code INTEGER,
    hangup_cause VARCHAR(64),
    recording_url TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    started_at TIMESTAMP,
    answered_at TIMESTAMP,
    ended_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_call_records_task_id ON lk_call_records(task_id);
CREATE INDEX IF NOT EXISTS idx_call_records_phone ON lk_call_records(phone);
CREATE INDEX IF NOT EXISTS idx_call_records_created_at ON lk_call_records(created_at);

-- 插入默认话术
INSERT INTO lk_scripts (script_id, name, script_type, opening_text, main_prompt, closing_text)
VALUES (
    'default',
    '默认话术',
    'general',
    '您好，我是智能助手，请问有什么可以帮助您的吗？',
    '你是一个专业的电话客服助手。请根据用户的回答进行自然的对话。',
    '感谢您的接听，祝您生活愉快，再见！'
) ON CONFLICT (script_id) DO NOTHING;

-- ============================================================
-- 文件信息表
-- ============================================================
CREATE TABLE IF NOT EXISTS lk_files (
    id SERIAL PRIMARY KEY,
    file_id VARCHAR(128) UNIQUE NOT NULL,
    call_id VARCHAR(128),
    file_type VARCHAR(32) NOT NULL,
    storage_path TEXT NOT NULL,
    storage_bucket VARCHAR(64),
    file_name VARCHAR(256),
    mime_type VARCHAR(64),
    file_size_bytes BIGINT DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    sample_rate INTEGER,
    download_url TEXT,
    egress_id VARCHAR(128),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_files_call_id ON lk_files(call_id);
CREATE INDEX IF NOT EXISTS idx_files_file_type ON lk_files(file_type);

-- ============================================================
-- 通话记录详情表（问答形式）
-- ============================================================
CREATE TABLE IF NOT EXISTS lk_call_record_details (
    id SERIAL PRIMARY KEY,
    call_record_id INTEGER NOT NULL REFERENCES lk_call_records(id),
    call_id VARCHAR(128) NOT NULL,
    round_num INTEGER NOT NULL,
    question TEXT,
    question_audio_file_id VARCHAR(128),
    question_duration_sec REAL DEFAULT 0,
    answer_content TEXT,
    answer_audio_file_id VARCHAR(128),
    answer_duration_sec REAL DEFAULT 0,
    is_interrupted BOOLEAN DEFAULT FALSE,
    interrupted_at_sec REAL,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    stt_latency_ms INTEGER,
    llm_latency_ms INTEGER,
    tts_latency_ms INTEGER,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_call_record_details_call_id ON lk_call_record_details(call_id);
CREATE INDEX IF NOT EXISTS idx_call_record_details_record_id ON lk_call_record_details(call_record_id);
CREATE INDEX IF NOT EXISTS idx_call_record_details_round ON lk_call_record_details(call_id, round_num);

-- ============================================================
-- 扩展通话记录表
-- ============================================================
ALTER TABLE lk_call_records ADD COLUMN IF NOT EXISTS recording_file_id VARCHAR(128);
ALTER TABLE lk_call_records ADD COLUMN IF NOT EXISTS egress_id VARCHAR(128);
ALTER TABLE lk_call_records ADD COLUMN IF NOT EXISTS total_duration_sec REAL DEFAULT 0;

-- 号码表新增 SIP 呼叫状态字段
ALTER TABLE lk_task_phones ADD COLUMN IF NOT EXISTS sip_call_status VARCHAR(32);
ALTER TABLE lk_task_phones ADD COLUMN IF NOT EXISTS sip_response_message TEXT;

-- 号码表新增通话时长字段
ALTER TABLE lk_task_phones ADD COLUMN IF NOT EXISTS last_call_duration_sec REAL DEFAULT 0;
