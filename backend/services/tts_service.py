"""
TTS 语音合成服务
抽象层：支持阿里云 TTS 和 CosyVoice 本地部署
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
        """基于文本内容生成缓存文件路径"""
        text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
        output_dir = config.tts.output_dir
        os.makedirs(output_dir, exist_ok=True)
        return os.path.join(output_dir, f"tts_{text_hash}.{ext}")


class AliTTSClient(BaseTTS):
    """
    阿里云智能语音服务 TTS
    文档: https://help.aliyun.com/document_detail/84435.html

    安装依赖:
    pip install aliyun-python-sdk-core nls-python-sdk
    """

    def __init__(self, cfg: TTSConfig):
        self.appkey = cfg.ali_appkey
        self.token = cfg.ali_token
        self.voice = cfg.voice
        self.speech_rate = int((cfg.speech_rate - 1.0) * 100)  # 转换为 -500~500
        self.format = cfg.audio_format

    async def synthesize(self, text: str) -> str:
        cache_path = self._get_cache_path(text)
        if os.path.exists(cache_path):
            return cache_path

        # 阿里云 TTS SDK 是同步的，用线程池包装
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
                    text=text,
                    voice=self.voice,
                    aformat=self.format,
                    speech_rate=self.speech_rate,
                    volume=80,
                    sample_rate=8000,
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
        """降级到 Edge TTS（免费，质量较好）"""
        logger.warning("阿里云 TTS 失败，降级到 Edge TTS")
        return await EdgeTTSClient().synthesize(text)


class EdgeTTSClient(BaseTTS):
    """
    Microsoft Edge TTS（免费方案，无需 API Key）
    质量不错，适合开发测试和低成本场景

    安装依赖:
    pip install edge-tts
    """

    VOICE_MAP = {
        "female_standard": "zh-CN-XiaoxiaoNeural",  # 女声，标准
        "male_standard":   "zh-CN-YunxiNeural",      # 男声，标准
        "female_warm":     "zh-CN-XiaoyiNeural",     # 女声，温柔
    }

    def __init__(self, voice_key: str = "female_warm"):
        self.voice = self.VOICE_MAP.get(voice_key, self.VOICE_MAP["female_warm"])

    async def synthesize(self, text: str) -> str:
        cache_path = self._get_cache_path(f"edge_{text}")
        if os.path.exists(cache_path):
            return cache_path

        try:
            import edge_tts
            # Edge TTS 输出 MP3，需要转换为 WAV（8000Hz，16bit，mono）供 FreeSWITCH 使用
            mp3_path = cache_path.replace(".wav", ".mp3")
            communicate = edge_tts.Communicate(text, self.voice, rate="+5%")
            await communicate.save(mp3_path)

            # 用 ffmpeg 转换格式
            wav_path = cache_path
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", mp3_path,
                "-ar", "8000",     # FreeSWITCH 内部采样率
                "-ac", "1",        # 单声道
                "-acodec", "pcm_s16le",  # 16bit PCM
                wav_path,
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
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r=8000:cl=mono",
                "-t", str(duration_sec),
                "-acodec", "pcm_s16le",
                path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        return path


class CosyVoiceClient(BaseTTS):
    """
    CosyVoice 本地 TTS（阿里开源，效果极佳）
    项目地址: https://github.com/FunAudioLLM/CosyVoice

    假设已启动 HTTP 推理服务：
    python webui.py --port 50000
    """

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
                payload = {
                    "text": text,
                    "spk_id": self.voice,
                    "speed": self.speed,
                    "stream": False,
                }
                async with session.post(
                    f"{self.url}/inference_sft",
                    json=payload,
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
        # 生成一个极短的静音 WAV
        path = "/tmp/tts_mock.wav"
        if not os.path.exists(path):
            # 生成 0.1s 静音 WAV (44 bytes header + minimal data)
            import struct
            sample_rate = 8000
            duration = 0.3
            num_samples = int(sample_rate * duration)
            audio_data = b"\x00\x00" * num_samples  # 16bit 零值
            with open(path, "wb") as f:
                # WAV header
                f.write(b"RIFF")
                f.write(struct.pack("<I", 36 + len(audio_data)))
                f.write(b"WAVE")
                f.write(b"fmt ")
                f.write(struct.pack("<I", 16))   # chunk size
                f.write(struct.pack("<H", 1))    # PCM
                f.write(struct.pack("<H", 1))    # mono
                f.write(struct.pack("<I", sample_rate))
                f.write(struct.pack("<I", sample_rate * 2))
                f.write(struct.pack("<H", 2))    # block align
                f.write(struct.pack("<H", 16))   # bits per sample
                f.write(b"data")
                f.write(struct.pack("<I", len(audio_data)))
                f.write(audio_data)
        return path


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
    elif cfg.provider == "mock":
        logger.info("使用 Mock TTS（仅开发测试）")
        return MockTTSClient()
    else:
        logger.warning(f"未知 TTS 提供商 {cfg.provider}，使用 Edge TTS")
        return EdgeTTSClient()
