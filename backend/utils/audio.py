"""
音频处理工具
主要功能：
  - PCM / WAV / MP3 格式互转（通过 ffmpeg）
  - 简单 VAD（静音能量检测）
  - 音频片段拼接
"""
import asyncio
import audioop
import logging
import os
import struct
import wave
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


# ============================================================
# WAV 文件操作
# ============================================================

def write_wav(path: str, pcm_data: bytes, sample_rate: int = 8000, channels: int = 1, sampwidth: int = 2):
    """将 PCM 原始数据写为 WAV 文件"""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)


def read_wav_pcm(path: str) -> tuple[bytes, int, int]:
    """
    读取 WAV 文件，返回 (pcm_bytes, sample_rate, channels)
    自动转换为 16bit PCM
    """
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    return pcm, sample_rate, channels


async def convert_to_wav_8k(input_path: str, output_path: str) -> bool:
    """
    使用 ffmpeg 将任意音频转为 FreeSWITCH 兼容格式
    输出：8000Hz, 16bit, 单声道 WAV
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-i", input_path,
        "-ar", "8000",
        "-ac", "1",
        "-acodec", "pcm_s16le",
        output_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error(f"ffmpeg 转换失败: {stderr.decode()}")
        return False
    return True


async def pcm_to_wav_file(
    pcm_gen: AsyncGenerator[bytes, None],
    output_path: str,
    sample_rate: int = 8000,
) -> str:
    """收集流式 PCM，写为 WAV 文件"""
    chunks = []
    async for chunk in pcm_gen:
        if chunk:
            chunks.append(chunk)
    pcm_data = b"".join(chunks)
    write_wav(output_path, pcm_data, sample_rate=sample_rate)
    return output_path


# ============================================================
# 简单 VAD（Voice Activity Detection）
# 基于能量阈值，比 WebRTC VAD 轻量
# 生产环境建议用 silero-vad 或 WebRTC VAD
# ============================================================

class SimpleVAD:
    """
    基于 RMS 能量的简单 VAD
    用于判断音频帧是否包含语音
    """

    def __init__(
        self,
        sample_rate: int = 8000,
        frame_ms: int = 20,
        energy_threshold: int = 300,    # RMS 能量阈值（0~32768）
        speech_min_frames: int = 3,     # 连续有声帧数才算语音开始
        silence_min_frames: int = 25,   # 连续静音帧数才算语音结束（500ms）
    ):
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * frame_ms / 1000) * 2  # bytes（16bit）
        self.energy_threshold = energy_threshold
        self.speech_min_frames = speech_min_frames
        self.silence_min_frames = silence_min_frames

        self._speech_frames = 0
        self._silence_frames = 0
        self._in_speech = False

    def is_speech_frame(self, frame: bytes) -> bool:
        """判断单帧是否有语音"""
        if len(frame) < 2:
            return False
        rms = audioop.rms(frame, 2)  # 计算 RMS 能量
        return rms > self.energy_threshold

    def process_frame(self, frame: bytes) -> tuple[bool, bool]:
        """
        处理一帧音频
        返回 (is_speech_active, speech_end_detected)
          is_speech_active   : 当前是否处于语音段
          speech_end_detected: 是否检测到语音结束（可送给 ASR）
        """
        is_speech = self.is_speech_frame(frame)
        speech_ended = False

        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0
            if self._speech_frames >= self.speech_min_frames:
                self._in_speech = True
        else:
            self._silence_frames += 1
            self._speech_frames = 0
            if self._in_speech and self._silence_frames >= self.silence_min_frames:
                self._in_speech = False
                speech_ended = True

        return self._in_speech, speech_ended

    def reset(self):
        self._speech_frames = 0
        self._silence_frames = 0
        self._in_speech = False


async def vad_segment_audio(
    audio_gen: AsyncGenerator[bytes, None],
    vad: SimpleVAD,
) -> AsyncGenerator[bytes, None]:
    """
    用 VAD 切割音频流，只输出有声音的片段
    适合在发送给 ASR 前过滤静音，降低 ASR 费用
    """
    buffer = b""
    speech_buffer = b""

    async for chunk in audio_gen:
        buffer += chunk
        frame_size = vad.frame_size

        while len(buffer) >= frame_size:
            frame = buffer[:frame_size]
            buffer = buffer[frame_size:]

            is_active, speech_ended = vad.process_frame(frame)

            if is_active:
                speech_buffer += frame
            elif speech_ended and speech_buffer:
                # 语音段结束，输出并清空缓冲
                yield speech_buffer
                speech_buffer = b""

    # 流结束时输出剩余
    if speech_buffer:
        yield speech_buffer


# ============================================================
# PCMU (G.711 μ-law) ↔ PCM 转换
# FreeSWITCH 默认编解码器是 PCMU，ASR 需要 PCM（线性 PCM）
# ============================================================

def pcmu_to_pcm(pcmu_data: bytes) -> bytes:
    """PCMU（G.711 μ-law） → 16bit 线性 PCM"""
    return audioop.ulaw2lin(pcmu_data, 2)


def pcm_to_pcmu(pcm_data: bytes) -> bytes:
    """16bit 线性 PCM → PCMU（G.711 μ-law）"""
    return audioop.lin2ulaw(pcm_data, 2)


def pcma_to_pcm(pcma_data: bytes) -> bytes:
    """PCMA（G.711 A-law） → 16bit 线性 PCM"""
    return audioop.alaw2lin(pcma_data, 2)


def resample_pcm(pcm_data: bytes, from_rate: int, to_rate: int) -> bytes:
    """重采样 PCM（如 8000Hz → 16000Hz）"""
    if from_rate == to_rate:
        return pcm_data
    resampled, _ = audioop.ratecv(pcm_data, 2, 1, from_rate, to_rate, None)
    return resampled
