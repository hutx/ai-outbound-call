-- 话术打断策略配置迁移
-- 执行方式: psql -U <user> -d <db> -f migrations/001_add_barge_in_config.sql
--
-- 为 call_scripts 表添加 5 个打断策略配置字段

ALTER TABLE call_scripts
  ADD COLUMN IF NOT EXISTS opening_barge_in BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS closing_barge_in BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS conversation_barge_in BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS barge_in_protect_start INTEGER NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS barge_in_protect_end INTEGER NOT NULL DEFAULT 3;
