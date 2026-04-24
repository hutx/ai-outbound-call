#!/bin/bash
# ============================================================
# Kamailio 数据库初始化脚本
# - 创建 kamailio 数据库（如不存在）
# - 创建 subscriber / location / version 等核心表
# - 插入默认分机账号 1001-1005
# - 幂等执行：重复运行不会报错或重复插入
# ============================================================

set -e

# 数据库连接参数
DB_HOST="${LK_KAMAILIO_DB_HOST:-postgres}"
DB_PORT="${LK_KAMAILIO_DB_PORT:-5432}"
DB_NAME="${LK_KAMAILIO_DB_NAME:-kamailio}"
DB_USER="${LK_KAMAILIO_DB_USER:-postgres}"
DB_PASS="${LK_KAMAILIO_DB_PASSWORD:-postgres}"
SIP_DOMAIN="${LK_KAMAILIO_SIP_DOMAIN:-localhost}"

export PGPASSWORD="$DB_PASS"

# 等待 PostgreSQL 就绪
echo "[kamailio-init] 等待 PostgreSQL 就绪..."
MAX_RETRIES=30
RETRY=0
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q 2>/dev/null; do
    RETRY=$((RETRY + 1))
    if [ $RETRY -ge $MAX_RETRIES ]; then
        echo "[kamailio-init] 错误：PostgreSQL 在 ${MAX_RETRIES} 次重试后仍未就绪，退出"
        exit 1
    fi
    echo "[kamailio-init] PostgreSQL 未就绪，等待中... ($RETRY/$MAX_RETRIES)"
    sleep 2
done
echo "[kamailio-init] PostgreSQL 已就绪"

# 检查并创建 kamailio 数据库
DB_EXISTS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null || echo "")

if [ "$DB_EXISTS" != "1" ]; then
    echo "[kamailio-init] 创建数据库 $DB_NAME ..."
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c "CREATE DATABASE $DB_NAME;"
    echo "[kamailio-init] 数据库 $DB_NAME 创建完成"
else
    echo "[kamailio-init] 数据库 $DB_NAME 已存在，跳过创建"
fi

# 创建 Kamailio 核心表
echo "[kamailio-init] 创建 Kamailio 核心表..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" <<'EOSQL'

-- version 表（Kamailio 用于检查表版本）
CREATE TABLE IF NOT EXISTS version (
    id SERIAL PRIMARY KEY,
    table_name VARCHAR(32) NOT NULL,
    table_version INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT version_table_name_idx UNIQUE (table_name)
);

-- subscriber 表（SIP 用户认证）
CREATE TABLE IF NOT EXISTS subscriber (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    domain VARCHAR(64) NOT NULL DEFAULT '',
    password VARCHAR(64) NOT NULL DEFAULT '',
    ha1 VARCHAR(64) NOT NULL DEFAULT '',
    ha1b VARCHAR(64) NOT NULL DEFAULT '',
    email_address VARCHAR(128) NOT NULL DEFAULT '',
    rpid VARCHAR(64) DEFAULT NULL,
    CONSTRAINT subscriber_account_idx UNIQUE (username, domain)
);

-- location 表（SIP 注册位置信息）
CREATE TABLE IF NOT EXISTS location (
    id SERIAL PRIMARY KEY,
    ruid VARCHAR(64) NOT NULL DEFAULT '',
    username VARCHAR(64) NOT NULL DEFAULT '',
    domain VARCHAR(64) DEFAULT NULL,
    contact VARCHAR(512) NOT NULL DEFAULT '',
    received VARCHAR(128) DEFAULT NULL,
    path VARCHAR(512) DEFAULT NULL,
    expires TIMESTAMP NOT NULL DEFAULT '2030-05-28 21:32:15',
    q REAL NOT NULL DEFAULT 1.0,
    callid VARCHAR(255) NOT NULL DEFAULT 'Default-Call-ID',
    cseq INTEGER NOT NULL DEFAULT 1,
    last_modified TIMESTAMP NOT NULL DEFAULT '2000-01-01 00:00:01',
    flags INTEGER NOT NULL DEFAULT 0,
    cflags INTEGER NOT NULL DEFAULT 0,
    user_agent VARCHAR(255) NOT NULL DEFAULT '',
    socket VARCHAR(64) DEFAULT NULL,
    methods INTEGER DEFAULT NULL,
    instance VARCHAR(255) DEFAULT NULL,
    reg_id INTEGER NOT NULL DEFAULT 0,
    server_id INTEGER NOT NULL DEFAULT 0,
    connection_id INTEGER NOT NULL DEFAULT 0,
    keepalive INTEGER NOT NULL DEFAULT 0,
    partition INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT location_ruid_idx UNIQUE (ruid)
);
CREATE INDEX IF NOT EXISTS location_account_contact_idx ON location (username, domain, contact);
CREATE INDEX IF NOT EXISTS location_expires_idx ON location (expires);

-- location_attrs 表（注册位置扩展属性）
CREATE TABLE IF NOT EXISTS location_attrs (
    id SERIAL PRIMARY KEY,
    ruid VARCHAR(64) NOT NULL DEFAULT '',
    username VARCHAR(64) NOT NULL DEFAULT '',
    domain VARCHAR(64) DEFAULT NULL,
    aname VARCHAR(64) NOT NULL DEFAULT '',
    atype INTEGER NOT NULL DEFAULT 0,
    avalue VARCHAR(512) NOT NULL DEFAULT '',
    last_modified TIMESTAMP NOT NULL DEFAULT '2000-01-01 00:00:01'
);
CREATE INDEX IF NOT EXISTS location_attrs_account_record_idx ON location_attrs (username, domain, ruid);

-- 插入表版本信息（Kamailio 启动时检查）
INSERT INTO version (table_name, table_version) VALUES ('subscriber', 7)
    ON CONFLICT (table_name) DO UPDATE SET table_version = 7;
INSERT INTO version (table_name, table_version) VALUES ('location', 9)
    ON CONFLICT (table_name) DO UPDATE SET table_version = 9;
INSERT INTO version (table_name, table_version) VALUES ('location_attrs', 1)
    ON CONFLICT (table_name) DO UPDATE SET table_version = 1;

EOSQL

echo "[kamailio-init] 核心表创建完成"

# 插入默认分机账号 1001-1005
echo "[kamailio-init] 插入默认分机账号..."
for EXT in 1001 1002 1003 1004 1005; do
    # 计算 HA1: MD5(username:domain:password)
    HA1=$(printf '%s' "${EXT}:${SIP_DOMAIN}:${EXT}" | md5sum | awk '{print $1}')
    # 计算 HA1B: MD5(username@domain:domain:password)
    HA1B=$(printf '%s' "${EXT}@${SIP_DOMAIN}:${SIP_DOMAIN}:${EXT}" | md5sum | awk '{print $1}')

    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c \
        "INSERT INTO subscriber (username, domain, password, ha1, ha1b)
         VALUES ('${EXT}', '${SIP_DOMAIN}', '${EXT}', '${HA1}', '${HA1B}')
         ON CONFLICT (username, domain) DO UPDATE SET
             password = EXCLUDED.password,
             ha1 = EXCLUDED.ha1,
             ha1b = EXCLUDED.ha1b;" \
        2>/dev/null

    echo "[kamailio-init]   分机 ${EXT} -> ha1=${HA1}"
done

echo "[kamailio-init] ================================================"
echo "[kamailio-init] Kamailio 数据库初始化完成！"
echo "[kamailio-init]   数据库: $DB_NAME"
echo "[kamailio-init]   域名: $SIP_DOMAIN"
echo "[kamailio-init]   分机: 1001-1005 (密码与账号相同)"
echo "[kamailio-init] ================================================"
