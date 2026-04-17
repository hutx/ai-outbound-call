"""
配置管理模块
所有配置从环境变量读取，支持 .env 文件
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv


load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() == "true"


@dataclass
class FreeSwitchConfig:
    """FreeSWITCH ESL 连接配置"""
    host: str = "127.0.0.1"
    port: int = 8021
    password: str = "ClueCon"
    # ESL Socket 服务器端口（FreeSWITCH 反连后端用）
    socket_port: int = 9999
    # mod_audio_stream WebSocket 接收端口
    audio_stream_port: int = 8765
    # mod_forkzstream WebSocket 接收端口
    forkzstream_port: int = 8766
    # SIP Gateway 名称（sofia.conf.xml 中配置的 gateway name）
    gateway: str = "carrier_trunk"
    # 内部分机呼叫时使用的目录域
    internal_domain: str = "192.168.5.15"
    # 外呼超时秒数（30s 无人接听则放弃）
    originate_timeout: int = 30
    # 录音存储路径
    recording_path: str = "/recordings"

    def __post_init__(self):
        self.host = _env("FS_HOST", self.host)
        self.port = _env_int("FS_ESL_PORT", self.port)
        self.password = _env("FS_ESL_PASSWORD", self.password)
        self.socket_port = _env_int("FS_SOCKET_PORT", self.socket_port)
        self.audio_stream_port = _env_int("FS_AUDIO_STREAM_PORT", self.audio_stream_port)
        self.forkzstream_port = _env_int("FS_FORKZSTREAM_PORT", self.forkzstream_port)
        self.gateway = _env("FS_GATEWAY", self.gateway)
        self.internal_domain = _env("FS_INTERNAL_DOMAIN", self.internal_domain)
        self.originate_timeout = _env_int("FS_ORIGINATE_TIMEOUT", self.originate_timeout)
        self.recording_path = _env("FS_RECORDING_PATH", self.recording_path)


@dataclass
class ASRConfig:
    """ASR 语音识别配置"""
    # 支持: funasr_local | xunfei | ali | bailian | mock
    provider: str = "ali"

    # FunASR 本地服务地址
    funasr_host: str = "127.0.0.1"
    funasr_port: int = 10095

    # 讯飞实时语音转写
    xunfei_appid: str = ""
    xunfei_apikey: str = ""
    xunfei_apisecret: str = ""

    # ── 阿里云智能语音交互 NLS ─────────────────────────────
    # 获取方式: 阿里云控制台 → 智能语音交互 → 项目管理
    # AppKey: 每个项目唯一，用于标识调用来源
    ali_asr_appkey: str = ""
    # AccessKey ID + Secret（RAM 子账号，授权 AliyunNLSFullAccess）
    ali_access_key_id: str = ""
    ali_access_key_secret: str = ""
    # NLS Token（由 AK 换取，有效期 24h，SDK 会自动刷新）
    # 也可以直接填写 Token 跳过动态获取（适合测试）
    ali_nls_token: str = ""
    # NLS 服务地址（默认上海，可换 beijing / cn-hangzhou）
    # 上海: wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1
    # 北京: wss://nls-gateway-cn-beijing.aliyuncs.com/ws/v1
    ali_nls_url: str = "wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1"
    # 实时转写：是否开启中间结果（true = 边说边出结果，false = 只返回最终句子）
    ali_enable_intermediate: bool = True
    # 实时转写：是否开启标点预测
    ali_enable_punctuation: bool = True
    # 实时转写：是否开启数字转写（把"一百二十三"转为"123"）
    ali_enable_itn: bool = True
    # 热词 ID（在阿里云控制台配置，提升产品名/专有名词识别率）
    ali_vocabulary_id: str = ""

    # ── 阿里云百炼平台 ASR ──────────────────────────────────
    # 百炼 API Key（sk-xxx，控制台获取）
    bailian_access_token: str = ""
    # 百炼 ASR 模型（默认 fun-asr-realtime，SDK 支持的实时转写模型）
    bailian_asr_model: str = "fun-asr-realtime"

    # 通用参数
    # 采样率（Hz），FreeSWITCH PCMU/PCMA 默认 8000Hz
    sample_rate: int = 8000
    # VAD 静音检测阈值（ms），超过此时长则认为用户说完
    # 通话场景优先响应速度，默认控制在 400ms 左右。
    vad_silence_ms: int = 400

    def __post_init__(self):
        self.provider = _env("ASR_PROVIDER", self.provider)
        self.funasr_host = _env("FUNASR_HOST", self.funasr_host)
        self.funasr_port = _env_int("FUNASR_PORT", self.funasr_port)
        self.xunfei_appid = _env("XUNFEI_APPID", self.xunfei_appid)
        self.xunfei_apikey = _env("XUNFEI_APIKEY", self.xunfei_apikey)
        self.xunfei_apisecret = _env("XUNFEI_APISECRET", self.xunfei_apisecret)
        # 阿里云 ASR
        self.ali_asr_appkey = _env("ALI_ASR_APPKEY", self.ali_asr_appkey)
        self.ali_access_key_id = _env("ALI_ACCESS_KEY_ID", self.ali_access_key_id)
        self.ali_access_key_secret = _env("ALI_ACCESS_KEY_SECRET", self.ali_access_key_secret)
        self.ali_nls_token = _env("ALI_NLS_TOKEN", self.ali_nls_token)
        self.ali_nls_url = _env("ALI_NLS_URL", self.ali_nls_url)
        self.ali_enable_intermediate = _env_bool("ALI_ASR_INTERMEDIATE", self.ali_enable_intermediate)
        self.ali_enable_punctuation = _env_bool("ALI_ASR_PUNCTUATION", self.ali_enable_punctuation)
        self.ali_enable_itn = _env_bool("ALI_ASR_ITN", self.ali_enable_itn)
        self.ali_vocabulary_id = _env("ALI_ASR_VOCABULARY_ID", self.ali_vocabulary_id)
        # 百炼 ASR
        self.bailian_access_token = _env("BAILIAN_ACCESS_TOKEN", self.bailian_access_token)
        self.bailian_asr_model = _env("BAILIAN_ASR_MODEL", self.bailian_asr_model)
        self.sample_rate = _env_int("ASR_SAMPLE_RATE", self.sample_rate)
        self.vad_silence_ms = _env_int("VAD_SILENCE_MS", self.vad_silence_ms)


@dataclass
class TTSConfig:
    """TTS 语音合成配置"""
    # 支持: cosyvoice_local | ali | bailian | edge | mock
    provider: str = "ali"
    # 阿里云 TTS
    ali_appkey: str = ""
    ali_token: str = ""
    # CosyVoice 本地服务
    cosyvoice_url: str = "http://127.0.0.1:50000"
    # 发音人
    voice: str = "longxiaochun_v3"
    # 语速 (0.5 ~ 2.0)，提高至 1.2 以缩短 TTS 播报耗时
    speech_rate: float = 1.2
    # 音频格式
    audio_format: str = "wav"
    # 合成音频临时目录
    output_dir: str = "/tmp/tts_cache"

    # ── 阿里云百炼平台 TTS ──────────────────────────────────
    # 百炼 API Key（sk-xxx，控制台获取）
    bailian_access_token: str = ""
    # 百炼 TTS 模型（cosyvoice-v3-flash / cosyvoice-v3-plus 等）
    bailian_tts_model: str = "cosyvoice-v3-flash"

    def __post_init__(self):
        self.provider = _env("TTS_PROVIDER", self.provider)
        self.ali_appkey = _env("ALI_TTS_APPKEY", self.ali_appkey)
        self.ali_token = _env("ALI_TTS_TOKEN", self.ali_token)
        self.cosyvoice_url = _env("COSYVOICE_URL", self.cosyvoice_url)
        self.voice = _env("TTS_VOICE", self.voice)
        self.speech_rate = _env_float("TTS_SPEECH_RATE", self.speech_rate)
        self.output_dir = _env("TTS_OUTPUT_DIR", self.output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        # 百炼 TTS
        self.bailian_access_token = _env("BAILIAN_ACCESS_TOKEN", self.bailian_access_token)
        self.bailian_tts_model = _env("BAILIAN_TTS_MODEL", self.bailian_tts_model)


@dataclass
class LLMConfig:
    """LLM 对话引擎配置"""
    # 支持: auto | anthropic | dashscope_compatible | anthropic_compatible
    provider: str = "auto"
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    model: str = "qwen3.5-plus"
    max_tokens: int = 500
    # 温度：外呼场景建议 0.3~0.5，保证话术稳定性
    temperature: float = 0.4
    # 对话历史保留轮数（节省 token）
    max_history_turns: int = 20
    stream: bool = True

    def __post_init__(self):
        self.provider = _env("LLM_PROVIDER", self.provider)
        self.anthropic_api_key = _env("ANTHROPIC_API_KEY", self.anthropic_api_key)
        self.anthropic_base_url = _env("ANTHROPIC_BASE_URL", self.anthropic_base_url)
        self.model = _env("LLM_MODEL", self.model)
        self.max_tokens = _env_int("LLM_MAX_TOKENS", self.max_tokens)
        self.temperature = _env_float("LLM_TEMPERATURE", self.temperature)
        self.max_history_turns = _env_int("LLM_MAX_HISTORY", self.max_history_turns)
        self.stream = _env_bool("LLM_STREAM", self.stream)


@dataclass
class DatabaseConfig:
    url: str = "postgresql://postgres:password@localhost:5432/outbound_call"

    def __post_init__(self):
        self.url = _env("DATABASE_URL", self.url)


@dataclass
class RedisConfig:
    url: str = "redis://localhost:6379/0"

    def __post_init__(self):
        self.url = _env("REDIS_URL", self.url)


@dataclass
class AppConfig:
    max_concurrent_calls: int = 50
    api_port: int = 8000
    debug: bool = False
    # 单路通话最长时长（秒），超时强制挂断
    max_call_duration: int = 300
    # API 鉴权 Token（空 = 开放，仅限开发）
    api_token: str = ""

    def __post_init__(self):
        self.max_concurrent_calls = _env_int("MAX_CONCURRENT_CALLS", self.max_concurrent_calls)
        self.api_port = _env_int("API_PORT", self.api_port)
        self.debug = _env_bool("DEBUG", self.debug)
        self.max_call_duration = _env_int("MAX_CALL_DURATION", self.max_call_duration)
        self.api_token = _env("API_TOKEN", self.api_token)
        self.freeswitch = FreeSwitchConfig()
        self.asr = ASRConfig()
        self.tts = TTSConfig()
        self.llm = LLMConfig()
        self.db = DatabaseConfig()
        self.redis = RedisConfig()

    def validate_runtime(self):
        """启动时校验关键运行配置。"""
        if not self.debug and not self.api_token.strip():
            raise ValueError("生产模式必须配置 API_TOKEN（当前 DEBUG=false）。")


# 全局单例
config = AppConfig()
