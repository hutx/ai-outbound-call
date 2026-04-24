"""
迁移脚本：添加 Egress 相关表和字段
- 创建 lk_files 表
- 创建 lk_call_record_details 表
- 扩展 lk_call_records 表
"""
import asyncio
import os

import asyncpg


async def migrate():
    conn = await asyncpg.connect(
        host=os.getenv("LK_PG_HOST", "localhost"),
        port=int(os.getenv("LK_PG_PORT", "5432")),
        user=os.getenv("LK_PG_USER", "postgres"),
        password=os.getenv("LK_PG_PASSWORD", "postgres"),
        database=os.getenv("LK_PG_DATABASE", "ai_outbound_livekit"),
    )
    try:
        print("开始执行迁移...")

        # 创建 lk_files 表
        await conn.execute(
            """
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
            )
            """
        )
        print("lk_files 表已创建或已存在")

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_call_id ON lk_files(call_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_file_type ON lk_files(file_type)"
        )
        print("lk_files 索引已创建或已存在")

        # 创建 lk_call_record_details 表
        await conn.execute(
            """
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
            )
            """
        )
        print("lk_call_record_details 表已创建或已存在")

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_call_record_details_call_id ON lk_call_record_details(call_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_call_record_details_record_id ON lk_call_record_details(call_record_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_call_record_details_round ON lk_call_record_details(call_id, round_num)"
        )
        print("lk_call_record_details 索引已创建或已存在")

        # 扩展 lk_call_records 表
        await conn.execute(
            "ALTER TABLE lk_call_records ADD COLUMN IF NOT EXISTS recording_file_id VARCHAR(128)"
        )
        await conn.execute(
            "ALTER TABLE lk_call_records ADD COLUMN IF NOT EXISTS egress_id VARCHAR(128)"
        )
        await conn.execute(
            "ALTER TABLE lk_call_records ADD COLUMN IF NOT EXISTS total_duration_sec REAL DEFAULT 0"
        )
        print("lk_call_records 表已扩展")

        print("迁移完成")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
