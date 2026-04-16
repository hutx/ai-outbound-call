"""
音频处理工具
主要功能：
  - PCM / WAV 文件读写
  - 简单 VAD（静音能量检测）
"""
import math
import struct
import wave


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


def write_wav_header_to_fd(fd: int, sample_rate: int = 8000, channels: int = 1, sampwidth: int = 2):
    """向已打开的文件描述符写入 44 字节 WAV header（不关闭 fd）"""
    import struct, os
    # data_size 设为最大值，流式场景后续追加 PCM 数据
    data_size = 0x7FFFFFFF
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', data_size + 36, b'WAVE', b'fmt ',
        16, 1, channels, sample_rate,
        sample_rate * channels * sampwidth,
        channels * sampwidth, sampwidth * 8,
        b'data', data_size)
    os.write(fd, header)


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


def _pcm16_rms(frame: bytes) -> int:
    """计算 16bit little-endian PCM 的 RMS 能量。"""
    if len(frame) < 2:
        return 0
    sample_count = len(frame) // 2
    energy_sum = 0
    for (sample,) in struct.iter_unpack("<h", frame[: sample_count * 2]):
        energy_sum += sample * sample
    if sample_count == 0:
        return 0
    return math.isqrt(energy_sum // sample_count)


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
        rms = self.frame_rms(frame)
        return rms > self.energy_threshold

    def frame_rms(self, frame: bytes) -> int:
        """返回单帧 RMS，便于上层记录音量/是否有人声。"""
        return _pcm16_rms(frame)

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
