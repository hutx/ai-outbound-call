"""LiveKit 智能外呼系统配置管理"""
import os
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """系统配置"""

    # ---- LiveKit Server ----
    livekit_url: str = "ws://localhost:7880"
    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "secret"

    # ---- LiveKit SIP ----
    sip_trunk_id: str = ""
    sip_outbound_number: str = ""  # 外显号码
    sip_domain: str = ""

    # ---- Aliyun ASR (Paraformer) ----
    aliyun_asr_api_key: str = ""
    aliyun_asr_model: str = "paraformer-realtime-v2"
    aliyun_asr_ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

    # ---- Aliyun TTS (CosyVoice) ----
    aliyun_tts_api_key: str = ""
    aliyun_tts_model: str = "cosyvoice-v1"
    aliyun_tts_voice: str = "longxiaochun"
    aliyun_tts_ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    aliyun_tts_speech_rate: float = 1.0
    aliyun_tts_volume: int = 50

    # ---- Aliyun LLM (Qwen via DashScope OpenAI compat) ----
    aliyun_llm_api_key: str = ""
    aliyun_llm_model: str = "qwen3.5-plus"
    aliyun_llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    aliyun_llm_temperature: float = 0.4
    aliyun_llm_max_tokens: int = 500

    # ---- PostgreSQL ----
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_database: str = "ai_outbound_livekit"

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- Server ----
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False
    log_level: str = "INFO"

    # ---- Call Defaults ----
    default_concurrent_limit: int = 5
    default_max_retries: int = 1
    call_timeout_sec: int = 300
    originate_timeout_sec: int = 30

    # ---- Kamailio ----
    kamailio_sip_domain: str = "localhost"
    kamailio_sip_port: int = 5080

    # ---- SIP Provider (PSTN 网关) ----
    sip_provider_host: str = "59.110.91.40"
    sip_provider_port: int = 6922
    sip_provider_caller_id: str = "202603311547"
    sip_provider_prefix: str = "97776"

    model_config = {"env_file": ".env", "env_prefix": "LK_", "case_sensitive": False, "extra": "ignore"}


settings = Settings()
