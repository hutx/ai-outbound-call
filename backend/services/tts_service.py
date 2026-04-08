"""
TTS 语音合成服务
抽象层：支持阿里云 TTS、百炼 CosyVoice 和 Edge TTS
"""
import asyncio
import logging
import os
import hashlib
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import aiohttp
import aiofiles
import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat

from backend.core.config import config, TTSConfig

logger = logging.getLogger(__name__)


class BaseTTS(ABC):
    """TTS 基类"""

    @abstractmethod
    async def synthesize(self, text: str) -> str:
        """
        合成语音
        输入: 文本字符串
        输出: 音频文件路径（WAV 格式，FreeSWITCH 可直接播放）
        """
        pass

    def _get_cache_path(self, text: str, ext: str = "wav") -> str:
        """基于文本内容和音色生成缓存文件路径"""
        # 缓存 key 包含音色信息，换音色时自动失效
        voice = getattr(self, 'voice', '')
        cache_key = f"tts_{voice}_{text}"
        text_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        output_dir = config.tts.output_dir
        os.makedirs(output_dir, exist_ok=True)
        return os.path.join(output_dir, f"tts_{text_hash}.{ext}")


class AliTTSClient(BaseTTS):
    """
    阿里云智能语音服务 TTS
    文档: https://help.aliyun.com/document_detail/84435.html
    """

    def __init__(self, cfg: TTSConfig):
        self.appkey = cfg.ali_appkey
        self.token = cfg.ali_token
        self.voice = cfg.voice
        self.speech_rate = int((cfg.speech_rate - 1.0) * 100)
        self.format = cfg.audio_format

    async def synthesize(self, text: str) -> str:
        cache_path = self._get_cache_path(text)
        if os.path.exists(cache_path):
            return cache_path

        import threading
        loop = asyncio.get_event_loop()

        def _sync_tts():
            try:
                import nls
                result_audio = b""

                def on_data(data, *args):
                    nonlocal result_audio
                    result_audio += data

                def on_completed(*args):
                    pass

                def on_error(msg, *args):
                    logger.error(f"阿里云 TTS 错误: {msg}")

                tts = nls.NlsSpeechSynthesizer(
                    url="wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1",
                    token=self.token,
                    appkey=self.appkey,
                    on_data=on_data,
                    on_completed=on_completed,
                    on_error=on_error,
                )

                tts.start(
                    text=text, voice=self.voice, aformat=self.format,
                    speech_rate=self.speech_rate, volume=80, sample_rate=8000,
                )
                return result_audio
            except Exception as e:
                logger.error(f"阿里云 TTS 调用失败: {e}")
                return b""

        audio_data = await loop.run_in_executor(None, _sync_tts)
        if audio_data:
            async with aiofiles.open(cache_path, "wb") as f:
                await f.write(audio_data)
            return cache_path
        else:
            return await self._fallback_tts(text)

    async def _fallback_tts(self, text: str) -> str:
        logger.warning("阿里云 TTS 失败，降级到 Edge TTS")
        return await EdgeTTSClient().synthesize(text)


class EdgeTTSClient(BaseTTS):
    """
    Microsoft Edge TTS（免费方案，无需 API Key）
    """

    VOICE_MAP = {
        "female_standard": "zh-CN-XiaoxiaoNeural",
        "male_standard":   "zh-CN-YunxiNeural",
        "female_warm":     "zh-CN-XiaoyiNeural",
    }

    def __init__(self, voice_key: str = "female_warm"):
        self.voice = self.VOICE_MAP.get(voice_key, self.VOICE_MAP["female_warm"])

    async def synthesize(self, text: str) -> str:
        cache_path = self._get_cache_path(f"edge_{text}")
        if os.path.exists(cache_path):
            return cache_path

        try:
            import edge_tts
            mp3_path = cache_path.replace(".wav", ".mp3")
            communicate = edge_tts.Communicate(text, self.voice, rate="+5%")
            await communicate.save(mp3_path)

            wav_path = cache_path
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", mp3_path,
                "-ar", "8000", "-ac", "1", "-acodec", "pcm_s16le", wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            os.remove(mp3_path)
            return wav_path
        except Exception as e:
            logger.error(f"Edge TTS 失败: {e}")
            return await self._generate_silence(1.0)

    async def _generate_silence(self, duration_sec: float) -> str:
        """生成静音文件（最终降级）"""
        path = os.path.join(config.tts.output_dir, "silence.wav")
        if not os.path.exists(path):
            import struct
            sample_rate = 8000
            num_samples = int(sample_rate * duration_sec)
            audio_data = b"\x00\x00" * num_samples
            with open(path, "wb") as f:
                f.write(b"RIFF")
                f.write(struct.pack("<I", 36 + len(audio_data)))
                f.write(b"WAVEfmt ")
                f.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                     sample_rate * 2, 2, 16))
                f.write(b"data")
                f.write(struct.pack("<I", len(audio_data)))
                f.write(audio_data)
        return path


class CosyVoiceClient(BaseTTS):
    """CosyVoice 本地 TTS（本地 HTTP 推理服务）"""

    def __init__(self, cfg: TTSConfig):
        self.url = cfg.cosyvoice_url
        self.voice = cfg.voice
        self.speed = cfg.speech_rate

    async def synthesize(self, text: str) -> str:
        cache_path = self._get_cache_path(f"cosyvoice_{text}")
        if os.path.exists(cache_path):
            return cache_path

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.url}/inference_sft",
                    json={"text": text, "spk_id": self.voice,
                          "speed": self.speed, "stream": False},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        audio_data = await resp.read()
                        async with aiofiles.open(cache_path, "wb") as f:
                            await f.write(audio_data)
                        return cache_path
                    else:
                        logger.error(f"CosyVoice 返回错误: {resp.status}")
                        return await EdgeTTSClient().synthesize(text)
        except Exception as e:
            logger.error(f"CosyVoice 调用失败: {e}")
            return await EdgeTTSClient().synthesize(text)


class MockTTSClient(BaseTTS):
    """Mock TTS（开发测试用，直接返回静音文件）"""

    async def synthesize(self, text: str) -> str:
        logger.info(f"[Mock TTS] 合成文本: {text[:50]}...")
        path = "/tmp/tts_mock.wav"
        if not os.path.exists(path):
            import struct
            sample_rate, duration = 8000, 0.3
            audio_data = b"\x00\x00" * int(sample_rate * duration)
            with open(path, "wb") as f:
                f.write(b"RIFF")
                f.write(struct.pack("<I", 36 + len(audio_data)))
                f.write(b"WAVEfmt ")
                f.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                     sample_rate * 2, 2, 16))
                f.write(b"data")
                f.write(struct.pack("<I", len(audio_data)))
                f.write(audio_data)
        return path


class BailianCosyVoiceClient(BaseTTS):
    """
    阿里云百炼平台 CosyVoice 语音合成服务
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    使用 dashscope.audio.tts_v2.SpeechSynthesizer SDK
    文档：https://help.aliyun.com/zh/model-studio/cosyvoice-python-sdk
    """

    # FreeSWITCH 需要 WAV 8000Hz 16bit mono
    AUDIO_FORMAT = AudioFormat.WAV_8000HZ_MONO_16BIT

    def __init__(self, cfg: TTSConfig):
        self.model_name = cfg.bailian_tts_model or "cosyvoice-v3-flash"
        self.voice = cfg.voice or "longyingxiao_v3"
        # speech_rate: [0.5, 2.0], 默认 1.0
        self.speech_rate = max(0.5, min(2.0, cfg.speech_rate))
        self.output_dir = cfg.output_dir
        os.makedirs(self.output_dir, exist_ok=True)

        # 设置 dashscope API Key（SDK 全局配置）
        api_key = cfg.bailian_access_token or ""
        logger.info(f"百炼 TTS: API Key → {api_key[:10]}****" if api_key else "百炼 TTS: API Key 为空")
        if not api_key:
            logger.error("百炼 TTS: BAILIAN_ACCESS_TOKEN 未配置")
        dashscope.api_key = api_key

    async def synthesize(self, text: str) -> str:
        logger.info(f"百炼 TTS: API Key → {dashscope.api_key}")
        cache_path = self._get_cache_path(f"bailian_{text}")
        if os.path.exists(cache_path):
            return cache_path

        if not dashscope.api_key:
            logger.error("百炼 TTS: API Key 为空，降级 Edge TTS")
            return await EdgeTTSClient().synthesize(text)

        try:
            loop = asyncio.get_event_loop()
            audio_data = await loop.run_in_executor(
                None, self._sync_call, text,
            )

            if audio_data:
                async with aiofiles.open(cache_path, "wb") as f:
                    await f.write(audio_data)
                logger.info(f"百炼 TTS 成功 → {cache_path}")
                return cache_path
            else:
                logger.warning("百炼 TTS 返回空音频，降级 Edge TTS")
                return await EdgeTTSClient().synthesize(text)

        except Exception as e:
            logger.error(f"百炼 TTS 调用失败: {e}", exc_info=True)
            return await EdgeTTSClient().synthesize(text)

    def _sync_call(self, text: str) -> bytes:
        """在线程池中同步调用 dashscope TTS SDK"""
        synth = SpeechSynthesizer(
            model=self.model_name,
            voice=self.voice,
            format=self.AUDIO_FORMAT,
            volume=50,
            speech_rate=self.speech_rate,
            pitch_rate=1.0,
        )
        return synth.call(text)


def create_tts_client(cfg: Optional[TTSConfig] = None) -> BaseTTS:
    """工厂函数：根据配置创建对应的 TTS 客户端"""
    cfg = cfg or config.tts

    if cfg.provider == "ali":
        logger.info("使用阿里云 TTS")
        return AliTTSClient(cfg)
    elif cfg.provider == "edge":
        logger.info("使用 Edge TTS（免费）")
        return EdgeTTSClient(cfg.voice)
    elif cfg.provider == "cosyvoice_local":
        logger.info("使用 CosyVoice 本地 TTS")
        return CosyVoiceClient(cfg)
    elif cfg.provider == "bailian":
        logger.info("使用阿里云百炼 CosyVoice TTS")
        return BailianCosyVoiceClient(cfg)
    elif cfg.provider == "mock":
        logger.info("使用 Mock TTS（仅开发测试）")
        return MockTTSClient()
    else:
        logger.warning(f"未知 TTS 提供商 {cfg.provider}，使用 Edge TTS")
        return EdgeTTSClient()


# 全局单例
tts = create_tts_client()
