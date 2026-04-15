"""
ASR 语音识别服务
抽象层：统一接口，底层支持 FunASR（本地）、讯飞、阿里云 NLS 和百炼 ASR
"""
import asyncio
import logging
import json
import time
import uuid
import wave
import io
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional
import websockets

from backend.core.config import config, ASRConfig

logger = logging.getLogger(__name__)




class ASRResult:
    """ASR 识别结果"""
    def __init__(self, text: str, is_final: bool = False, confidence: float = 1.0):
        self.text = text.strip()
        self.is_final = is_final
        self.confidence = confidence

    def __repr__(self):
        flag = "✓" if self.is_final else "..."
        return f"ASRResult({flag} '{self.text}')"


class BaseASR(ABC):
    """ASR 基类"""

    @abstractmethod
    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        """
        流式识别
        输入: 音频数据生成器（PCM bytes）
        输出: 识别结果生成器
        call_uuid: 通话 UUID（用于音频文件命名，便于追溯）
        """
        pass

    async def recognize_once(self, audio_data: bytes, call_uuid: Optional[str] = None) -> str:
        """
        一次性识别（非流式）
        输入: 完整音频 bytes
        输出: 识别文本
        """
        results = []

        async def _gen():
            yield audio_data
            yield b""  # 结束信号

        async for result in self.recognize_stream(_gen(), call_uuid=call_uuid):
            if result.is_final:
                results.append(result.text)

        return " ".join(results)


class FunASRClient(BaseASR):
    """
    FunASR 本地部署客户端
    使用 WebSocket 流式接口
    项目地址: https://github.com/modelscope/FunASR

    安装方式:
    docker pull registry.cn-hangzhou.aliyuncs.com/modelscope-repo/modelscope:funasr-runtime-sdk-online-cpu-0.1.10
    docker run -p 10095:10095 ...
    """

    def __init__(self, cfg: ASRConfig):
        self.host = cfg.funasr_host
        self.port = cfg.funasr_port
        self.sample_rate = cfg.sample_rate
        self.vad_silence_ms = cfg.vad_silence_ms
        self._ws_uri = f"ws://{self.host}:{self.port}"

    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        """
        FunASR WebSocket 流式识别
        协议：先发送 config 包，然后分块发送音频，最后发送结束包
        """
        try:
            async with websockets.connect(
                self._ws_uri,
                ping_interval=None,
                max_size=10 * 1024 * 1024,
            ) as ws:

                # 1. 发送配置包
                config_msg = {
                    "mode": "2pass",            # 2pass: 实时+纠正模式
                    "chunk_size": [5, 10, 5],   # 看窗口大小
                    "chunk_interval": 10,        # 每 10ms 发一帧
                    "wav_name": "stream",
                    "is_speaking": True,
                    "hotwords": "",              # 热词（产品名等）
                    "itn": True,                 # 数字转写
                }
                await ws.send(json.dumps(config_msg))

                # 2. 并发：发送音频 + 接收结果
                send_task = asyncio.create_task(
                    self._send_audio(ws, audio_gen)
                )

                async for message in ws:
                    try:
                        data = json.loads(message)
                        text = data.get("text", "").strip()
                        is_final = data.get("is_final", False)
                        mode = data.get("mode", "")

                        if text:
                            yield ASRResult(
                                text=text,
                                is_final=is_final or mode == "2pass-offline",
                                confidence=data.get("confidence", 1.0),
                            )
                    except json.JSONDecodeError:
                        pass

                await send_task

        except (websockets.exceptions.WebSocketException, OSError) as e:
            logger.error(f"FunASR 连接失败: {e}")
            # 降级：返回空结果，由上层处理
            yield ASRResult(text="", is_final=True)

    async def _send_audio(self, ws, audio_gen):
        """分块发送音频数据"""
        chunk_size = 960  # 60ms @ 8000Hz 16bit = 960 bytes

        buffer = b""
        async for chunk in audio_gen:
            if not chunk:  # 空 bytes 代表结束
                break
            buffer += chunk

            while len(buffer) >= chunk_size:
                await ws.send(buffer[:chunk_size])
                buffer = buffer[chunk_size:]
                await asyncio.sleep(0)  # 让出控制权

        # 发送剩余数据
        if buffer:
            await ws.send(buffer)

        # 发送结束信号
        await ws.send(json.dumps({"is_speaking": False}))


class XunfeiASRClient(BaseASR):
    """
    讯飞实时语音转写
    文档: https://www.xfyun.cn/doc/asr/rtasr/API.html
    """

    def __init__(self, cfg: ASRConfig):
        import hashlib
        import hmac
        import base64
        import time
        from urllib.parse import urlencode

        self.appid = cfg.xunfei_appid
        self.apikey = cfg.xunfei_apikey
        self.apisecret = cfg.xunfei_apisecret
        self.sample_rate = cfg.sample_rate

    def _build_auth_url(self) -> str:
        """生成带鉴权的 WebSocket URL"""
        import hashlib, hmac, base64, time
        from urllib.parse import urlencode

        ts = str(int(time.time()))
        base_url = "wss://rtasr.xfyun.cn/v1/ws"
        sig_origin = f"{self.appid}{ts}"
        sig = hmac.new(
            self.apikey.encode("utf-8"),
            sig_origin.encode("utf-8"),
            digestmod=hashlib.sha1
        ).digest()
        sig_b64 = base64.b64encode(sig).decode("utf-8")

        params = {
            "appid": self.appid,
            "ts": ts,
            "signa": sig_b64,
        }
        return f"{base_url}?{urlencode(params)}"

    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        """讯飞流式识别"""
        uri = self._build_auth_url()

        try:
            async with websockets.connect(uri) as ws:
                send_task = asyncio.create_task(
                    self._send_xunfei_audio(ws, audio_gen)
                )

                async for message in ws:
                    data = json.loads(message)
                    if data.get("code") != 0:
                        logger.error(f"讯飞 ASR 错误: {data}")
                        continue

                    action = data.get("action")
                    if action == "result":
                        result_data = json.loads(data.get("data", "{}"))
                        cn = result_data.get("cn", {})
                        st = cn.get("st", {})
                        words = "".join(
                            w.get("w", "") for w in st.get("rt", [{}])[0].get("ws", [{}])
                            if True for w_item in [w.get("cw", [{}])[0]]
                        )
                        is_final = st.get("type") == "0"
                        if words:
                            yield ASRResult(text=words, is_final=is_final)

                    elif action == "started":
                        logger.debug("讯飞 ASR 连接就绪")

                await send_task

        except Exception as e:
            logger.error(f"讯飞 ASR 异常: {e}")
            yield ASRResult(text="", is_final=True)

    async def _send_xunfei_audio(self, ws, audio_gen):
        """讯飞音频发送（1280 bytes / 帧）"""
        frame_size = 1280
        buffer = b""

        async for chunk in audio_gen:
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= frame_size:
                import base64
                frame = buffer[:frame_size]
                buffer = buffer[frame_size:]
                await ws.send(json.dumps({
                    "common": {"app_id": self.appid},
                    "business": {
                        "language": "zh_cn",
                        "domain": "iat",
                        "accent": "mandarin",
                        "dwa": "wpgs",
                    },
                    "data": {
                        "status": 1,
                        "format": f"audio/L16;rate={self.sample_rate}",
                        "audio": base64.b64encode(frame).decode(),
                        "encoding": "raw",
                    }
                }))
                await asyncio.sleep(0.04)  # 控制发送速率

        # 结束帧
        import base64
        await ws.send(json.dumps({
            "data": {
                "status": 2,
                "format": f"audio/L16;rate={self.sample_rate}",
                "audio": base64.b64encode(buffer).decode() if buffer else "",
                "encoding": "raw",
            }
        }))


class AliASRClient(BaseASR):
    """
    阿里云智能语音交互 — 实时语音转写（NLS）
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    产品文档：
      https://help.aliyun.com/document_detail/84428.html

    开通步骤：
      1. 登录阿里云控制台 → 搜索「智能语音交互」→ 开通服务
      2. 创建项目，记录 AppKey
      3. 创建 RAM 子账号，授权 AliyunNLSFullAccess，保存 AK/SK

    鉴权方式：
      AK/SK → 调用 Token API → 获取 NLS Token（24h 有效）
      → 携带 Token 建立 WebSocket 连接

    协议概览（完整 message 序列）：
      Client → Server : {"header": {"name":"StartTranscription", ...}, "payload": {...}}
      Server → Client : {"header": {"name":"TranscriptionStarted"}}
      Client → Server : <binary PCM frames>  (循环发送)
      Server → Client : {"header": {"name":"TranscriptionResultChanged"}, "payload": {"result":"..."}}  # 中间结果
      Server → Client : {"header": {"name":"SentenceEnd"}, "payload": {"result":"..."}}                 # 句子结束
      Client → Server : {"header": {"name":"StopTranscription"}}
      Server → Client : {"header": {"name":"TranscriptionCompleted"}}

    安装依赖：
      pip install aiohttp  # Token 获取用 HTTP，无需额外 SDK
      websockets 已在 requirements.txt 中

    费用参考（2025）：
      实时语音转写：¥3.5 / 小时（按实际音频时长计费）
      首次开通赠 10 小时免费额度
    """

    # Token 缓存（进程级，多路通话共用，避免重复换取）
    _token_cache: dict = {}   # {"token": str, "expires_at": float}
    _token_lock: asyncio.Lock = asyncio.Lock()  # 并发控制锁

    # NLS Token 获取接口
    _TOKEN_URL = "https://nls-meta.cn-shanghai.aliyuncs.com/pop/2018-05-18/tokens"

    def __init__(self, cfg: "ASRConfig"):
        self.appkey    = cfg.ali_asr_appkey
        self.ak_id     = cfg.ali_access_key_id
        self.ak_secret = cfg.ali_access_key_secret
        self.nls_token = cfg.ali_nls_token   # 直接填写时跳过动态获取
        self.nls_url   = cfg.ali_nls_url
        self.sample_rate        = cfg.sample_rate
        self.enable_intermediate = cfg.ali_enable_intermediate
        self.enable_punctuation  = cfg.ali_enable_punctuation
        self.enable_itn          = cfg.ali_enable_itn
        self.vocabulary_id       = cfg.ali_vocabulary_id

    # ── Token 管理 ────────────────────────────────────────────

    async def _get_token(self) -> str:
        """
        获取 NLS Token
        优先级：env 直接配置 > 进程缓存 > 动态换取
        Token 有效期 24h，提前 5min 刷新
        """
        # 1. 直接配置了 Token（测试/调试场景）
        if self.nls_token:
            if not self.nls_token.strip():
                raise ValueError("配置了空的 ALI_NLS_TOKEN，请检查环境变量")
            return self.nls_token

        # 2. 检查进程缓存（带并发保护的双检锁模式）
        cache = AliASRClient._token_cache
        expires_at = cache.get("expires_at", 0)

        # 首次检查：快速路径（无锁）
        if cache.get("token") and expires_at > time.time() + 300:
            return cache["token"]

        # 需要刷新或创建锁
        async with AliASRClient._token_lock:
            # 双检：获取锁后再次确认（防止其他协程已刷新）
            cache = AliASRClient._token_cache
            expires_at = cache.get("expires_at", 0)
            if cache.get("token") and expires_at > time.time() + 300:
                return cache["token"]

            # 3. 用 AK/SK 动态换取
            if not self.ak_id or not self.ak_secret:
                raise RuntimeError(
                    "阿里云 ASR 未配置认证信息，请设置以下任一组合：\n"
                    "  方案A（推荐）: ALI_ACCESS_KEY_ID + ALI_ACCESS_KEY_SECRET\n"
                    "  方案B（临时）: ALI_NLS_TOKEN"
                )

            token, expire_time = await self._fetch_token_from_api()
            AliASRClient._token_cache = {
                "token": token,
                "expires_at": float(expire_time),
            }
            logger.info(f"阿里云 NLS Token 已刷新，有效至 {expire_time}")
            return token

    async def _fetch_token_from_api(self) -> tuple[str, int]:
        """
        调用阿里云 Token API 换取 NLS Token
        文档：https://help.aliyun.com/document_detail/450514.html
        """
        import time, hmac, hashlib, base64, uuid as _uuid
        from urllib.parse import quote, urlencode
        import aiohttp

        # 构造公共参数
        params = {
            "AccessKeyId":        self.ak_id,
            "Action":             "CreateToken",
            "Format":             "JSON",
            "RegionId":           "cn-shanghai",
            "SignatureMethod":    "HMAC-SHA1",
            "SignatureNonce":     str(_uuid.uuid4()),
            "SignatureVersion":   "1.0",
            "Timestamp":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Version":           "2019-02-28",
        }

        # 签名：按字典序排列参数，HMAC-SHA1
        sorted_params = sorted(params.items())
        query_str = urlencode(sorted_params)
        str_to_sign = "GET&%2F&" + quote(query_str, safe="")
        sign_key = (self.ak_secret + "&").encode("utf-8")
        signature = base64.b64encode(
            hmac.new(sign_key, str_to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        params["Signature"] = signature

        url = self._TOKEN_URL + "?" + urlencode(params)

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=True,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Token API 返回 {resp.status}: {text}")

                body = await resp.json()
                token_obj = body.get("Token", {})
                token = token_obj.get("Id", "")
                expire_time = token_obj.get("ExpireTime", 0)

                if not token:
                    raise RuntimeError(f"Token API 响应异常: {body}")

                return token, expire_time

    # ── 实时转写主逻辑 ────────────────────────────────────────

    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        """
        阿里云 NLS 实时语音转写（WebSocket 流式）

        帧格式：PCM 16bit 单声道，每帧 100ms = 1600 bytes @ 8000Hz / 3200 bytes @ 16000Hz
        建议发送速率与实际录音速率一致（实时倍速），否则服务端可能断开
        """
        try:
            token = await self._get_token()
        except Exception as e:
            logger.error(f"阿里云 ASR Token 获取失败: {e}")
            yield ASRResult(text="", is_final=True)
            return

        # NLS URL 携带 Token 参数
        ws_url = f"{self.nls_url}?token={token}"

        try:
            async with websockets.connect(
                ws_url,
                ping_interval=None,
                max_size=4 * 1024 * 1024,
                extra_headers={"X-NLS-Token": token},
            ) as ws:

                # 1. 发送 StartTranscription 指令
                task_id = self._new_task_id()
                start_msg = {
                    "header": {
                        "message_id":  self._new_msg_id(),
                        "task_id":     task_id,
                        "namespace":   "SpeechTranscriber",
                        "name":        "StartTranscription",
                        "appkey":      self.appkey,
                    },
                    "payload": {
                        "format":              "pcm",
                        "sample_rate":         self.sample_rate,
                        "enable_intermediate_result":     self.enable_intermediate,
                        "enable_punctuation_prediction":  self.enable_punctuation,
                        "enable_inverse_text_normalization": self.enable_itn,
                        # 热词：提升行业词汇识别率（如产品名、专有名词）
                        **({"vocabulary_id": self.vocabulary_id} if self.vocabulary_id else {}),
                        # 最大单句静音时长（ms），超过则强制断句
                        "max_sentence_silence": 800,
                        # 语言模型：通用场景用 normal，金融场景可申请定制
                        "model": "normal",
                    },
                }
                await ws.send(json.dumps(start_msg))

                # 2. 等待 TranscriptionStarted 确认
                started = False
                async for raw in ws:
                    msg = json.loads(raw)
                    name = msg.get("header", {}).get("name", "")
                    status = msg.get("header", {}).get("status", 0)

                    if name == "TranscriptionStarted":
                        started = True
                        logger.debug(f"阿里云 ASR 转写已启动, task_id={task_id}")
                        break
                    elif name == "TaskFailed":
                        err = msg.get("header", {}).get("status_message", "未知错误")
                        logger.error(f"阿里云 ASR 启动失败: {err}")
                        yield ASRResult(text="", is_final=True)
                        return

                if not started:
                    logger.error("阿里云 ASR 未收到 TranscriptionStarted")
                    yield ASRResult(text="", is_final=True)
                    return

                # 3. 并发：发送音频 + 接收结果
                result_queue: asyncio.Queue = asyncio.Queue()

                send_task = asyncio.create_task(
                    self._send_audio_frames(ws, audio_gen, task_id)
                )
                recv_task = asyncio.create_task(
                    self._recv_results(ws, result_queue, task_id)
                )

                # 从队列中 yield 结果
                while True:
                    try:
                        item = await asyncio.wait_for(result_queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        logger.warning("阿里云 ASR 接收超时")
                        break

                    if item is None:   # 哨兵，转写完成
                        break
                    yield item

                # 等待任务结束
                send_task.cancel()
                recv_task.cancel()
                await asyncio.gather(send_task, recv_task, return_exceptions=True)

        except (websockets.exceptions.WebSocketException, OSError, ConnectionRefusedError) as e:
            logger.error(f"阿里云 ASR WebSocket 连接失败: {e}")
            yield ASRResult(text="", is_final=True)
        except Exception as e:
            logger.error(f"阿里云 ASR 未知异常: {e}", exc_info=True)
            yield ASRResult(text="", is_final=True)

    async def _send_audio_frames(self, ws, audio_gen, task_id: str):
        """
        将音频流分帧发送给 NLS
        帧大小：100ms 对应的字节数
          8000Hz  × 0.1s × 2bytes = 1600 bytes
          16000Hz × 0.1s × 2bytes = 3200 bytes
        发送速率应与实际录音速率一致（实时倍速）
        """
        import time as _time

        frame_ms   = 100                                          # 每帧 100ms
        frame_size = int(self.sample_rate * frame_ms / 1000 * 2) # bytes（16bit）
        buffer     = b""
        frame_interval = frame_ms / 1000.0                       # 发送间隔（秒）

        try:
            async for chunk in audio_gen:
                if not chunk:
                    break
                buffer += chunk

                while len(buffer) >= frame_size:
                    frame  = buffer[:frame_size]
                    buffer = buffer[frame_size:]
                    await ws.send(frame)                          # 直接发 binary
                    await asyncio.sleep(frame_interval)           # 实时速率控制

            # 发送剩余不足一帧的数据
            if buffer:
                await ws.send(buffer)

            # 发送 StopTranscription 指令，触发最后一句的 SentenceEnd
            stop_msg = {
                "header": {
                    "message_id": self._new_msg_id(),
                    "task_id":    task_id,
                    "namespace":  "SpeechTranscriber",
                    "name":       "StopTranscription",
                    "appkey":     self.appkey,
                },
            }
            await ws.send(json.dumps(stop_msg))
            logger.debug(f"阿里云 ASR 发送 StopTranscription, task_id={task_id}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"阿里云 ASR 音频发送异常: {e}")

    async def _recv_results(self, ws, queue: asyncio.Queue, task_id: str):
        """
        接收 NLS 服务端推送的识别结果
        事件类型：
          TranscriptionResultChanged  中间结果（实时刷新，不是最终文本）
          SentenceEnd                 句子结束（最终文本，is_final=True）
          TranscriptionCompleted      全部转写完成
          TaskFailed                  服务端报错
        """
        try:
            async for raw in ws:
                # NLS 返回 text frame（JSON）
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                msg    = json.loads(raw)
                header = msg.get("header", {})
                name   = header.get("name", "")
                payload = msg.get("payload", {})

                if name == "TranscriptionResultChanged":
                    # 中间结果：实时显示，但不作为最终文本使用
                    text = payload.get("result", "").strip()
                    if text:
                        await queue.put(ASRResult(text=text, is_final=False))

                elif name == "SentenceEnd":
                    # 句子结束：最终文本，置信度由 confidence 字段给出
                    text       = payload.get("result", "").strip()
                    confidence = payload.get("confidence", 1.0)
                    stash_id   = payload.get("index", 0)
                    if text:
                        logger.debug(f"阿里云 ASR 句子[{stash_id}]: {text!r} (conf={confidence:.2f})")
                        await queue.put(ASRResult(
                            text=text,
                            is_final=True,
                            confidence=confidence,
                        ))

                elif name == "TranscriptionCompleted":
                    logger.debug(f"阿里云 ASR 转写完成, task_id={task_id}")
                    await queue.put(None)   # 哨兵
                    return

                elif name == "TaskFailed":
                    status  = header.get("status", 0)
                    err_msg = header.get("status_message", "")
                    logger.error(f"阿里云 ASR TaskFailed [{status}]: {err_msg}")
                    await queue.put(None)
                    return

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"阿里云 ASR 结果接收异常: {e}")
            await queue.put(None)

    # ── 工具方法 ──────────────────────────────────────────────

    @staticmethod
    def _new_task_id() -> str:
        import uuid as _uuid
        return _uuid.uuid4().hex   # 32位小写十六进制，NLS 要求

    @staticmethod
    def _new_msg_id() -> str:
        import uuid as _uuid
        return _uuid.uuid4().hex


class MockASRClient(BaseASR):
    """
    Mock ASR 客户端（用于本地开发测试，不依赖实际 ASR 服务）
    模拟用户说话，按预设脚本依次返回
    """
    MOCK_RESPONSES = [
        "可以，你说吧",
        "收益率是多少",
        "那风险怎么样",
        "我考虑一下",
        "好，发给我看看",
    ]

    def __init__(self):
        self._idx = 0

    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        # 消耗音频流
        async for _ in audio_gen:
            pass

        await asyncio.sleep(0.5)  # 模拟识别延迟

        text = self.MOCK_RESPONSES[self._idx % len(self.MOCK_RESPONSES)]
        self._idx += 1
        yield ASRResult(text=text, is_final=True)


class BailianASRClient(BaseASR):
    """
    阿里云百炼平台 ASR 语音识别服务 — dashscope Python SDK
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    使用 dashscope.audio.asr.Recognition 双向流式调用。
    文档：https://help.aliyun.com/zh/model-studio/fun-asr-realtime-python-sdk

    ★ 关键：paraformer-realtime-v1 要求 16kHz 输入采样率。
    输入音频（通常 8kHz）会通过线性插值上采样到 16kHz 后发送。

    调用流程：
      recognition.start()           → 建立连接
      recognition.send_audio_frame() → 逐帧发送 PCM
      recognition.stop()            → 发送结束信号
      callback.on_event()           ← 接收中间/最终结果
    """

    # ★ 百炼 ASR 要求的采样率（固定 16kHz）
    SEND_SAMPLE_RATE = 16000

    def __init__(self, cfg: ASRConfig):
        self.api_key = cfg.bailian_access_token
        self.model_name = cfg.bailian_asr_model or "paraformer-realtime-v1"
        self.input_sample_rate = cfg.sample_rate  # 通常 8000Hz

        # 设置 dashscope API Key
        if self.api_key:
            import dashscope
            dashscope.api_key = self.api_key
            logger.info(f"百炼 ASR: API Key → {self.api_key[:10]}****")
        else:
            logger.error("百炼 ASR: BAILIAN_ACCESS_TOKEN 未配置")

    @staticmethod
    def _upsample_8k_to_16k(pcm_8k: bytes) -> bytes:
        """将 8kHz 16-bit PCM 上采样到 16kHz（线性插值）"""
        if len(pcm_8k) < 2:
            return pcm_8k
        import struct as _struct
        n = len(pcm_8k) // 2
        samples = _struct.unpack(f'<{n}h', pcm_8k[:n * 2])
        out = []
        for i in range(n):
            out.append(samples[i])
            if i < n - 1:
                out.append((samples[i] + samples[i + 1]) // 2)
        out.append(samples[-1])
        return _struct.pack(f'<{len(out)}h', *out)

    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        if not self.api_key:
            logger.error("百炼 ASR: BAILIAN_ACCESS_TOKEN 未配置")
            yield ASRResult(text="", is_final=True)
            return

        # 加载 SDK
        try:
            from dashscope.audio.asr import (
                Recognition, RecognitionCallback, RecognitionResult,
            )
        except ImportError as exc:
            raise RuntimeError("请安装 dashscope: pip install dashscope") from exc

        # 捕获原始音频用于调试
        import os, re, time as _time
        dump_dir = "/recordings/debug"
        os.makedirs(dump_dir, exist_ok=True)
        dump_pcm = b""
        final_text = ""

        # 用 queue 桥接 SDK 同步回调 → async 消费
        import queue as _queue

        result_queue: _queue.Queue = _queue.Queue(maxsize=200)
        error_ref = {"value": None}

        class _Callback(RecognitionCallback):
            def on_open(self):
                logger.info("百炼 ASR SDK: 连接已打开")

            def on_event(self, result: RecognitionResult):
                """识别结果事件（中间或最终）"""
                try:
                    output = getattr(result, 'output', None)
                    if not output:
                        return

                    # ★ 关键：SDK 的 RecognitionResult.output 结构为：
                    #     {"sentence": {"begin_time": 250, "end_time": null, "text": "有，", "words": [...]}}
                    #     sentence.end_time 不为 null 表示句子结束（最终结果）
                    sentence = output.get("sentence", {}) or {}
                    text = sentence.get("text", "").strip()
                    end_time = sentence.get("end_time")
                    is_final = end_time is not None  # end_time 有值 = 句子结束

                    if text:
                        if is_final:
                            logger.info(f"百炼 ASR 最终结果: {text!r}")
                        else:
                            logger.debug(f"百炼 ASR 中间结果: {text!r}")
                        try:
                            result_queue.put_nowait(ASRResult(text=text, is_final=is_final))
                        except _queue.Full:
                            logger.warning("百炼 ASR 结果队列已满，丢弃结果")
                except Exception as e:
                    logger.error(f"百炼 ASR on_event 处理异常: {e}", exc_info=True)

            def on_complete(self):
                logger.info("百炼 ASR SDK: 识别完成")
                try:
                    result_queue.put_nowait(None)  # 哨兵
                except _queue.Full:
                    pass

            def on_error(self, message):
                error_ref["value"] = Exception(f"百炼 ASR 错误: {message}")
                logger.error(f"百炼 ASR SDK on_error: {message}")
                try:
                    result_queue.put_nowait(None)
                except _queue.Full:
                    pass

            def on_close(self):
                logger.info("百炼 ASR SDK: 连接已关闭")

        # 创建 Recognition 实例
        # ★ 关键：paraformer-realtime-v1 要求 16kHz 采样率
        recognition = Recognition(
            model=self.model_name,
            callback=_Callback(),
            format="pcm",
            sample_rate=self.SEND_SAMPLE_RATE,  # 16000Hz
        )

        # 在线程池中启动 start()（可能是阻塞调用）
        import concurrent.futures

        loop = asyncio.get_event_loop()

        async def _send_audio():
            """逐帧发送音频数据"""
            nonlocal dump_pcm
            frame_ms = 100
            # ★ 使用 16kHz 计算帧大小
            frame_size = int(self.SEND_SAMPLE_RATE * frame_ms / 1000 * 2)  # 3200 bytes @ 16kHz
            buffer = b""
            total_sent = 0
            frame_count = 0
            input_chunk_count = 0
            total_input_bytes = 0

            try:
                async for chunk in audio_gen:
                    if not chunk:
                        break

                    input_chunk_count += 1
                    total_input_bytes += len(chunk)
                    dump_pcm += chunk

                    # ★ 上采样 8k → 16k
                    if self.input_sample_rate < self.SEND_SAMPLE_RATE:
                        chunk_16k = self._upsample_8k_to_16k(chunk)
                    else:
                        chunk_16k = chunk
                    buffer += chunk_16k

                    while len(buffer) >= frame_size:
                        frame = buffer[:frame_size]
                        buffer = buffer[frame_size:]
                        recognition.send_audio_frame(frame)
                        total_sent += len(frame)
                        frame_count += 1

                # 发送剩余数据
                if buffer:
                    recognition.send_audio_frame(buffer)
                    total_sent += len(buffer)
                    frame_count += 1

                logger.info(
                    f"百炼 ASR: 音频发送完毕, "
                    f"输入 {input_chunk_count} chunks / {total_input_bytes} bytes, "
                    f"发送 {frame_count} 帧 / {total_sent} bytes "
                    f"({total_sent / (self.input_sample_rate * 2) * 1000:.0f}ms)"
                )

            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"百炼 ASR 音频发送异常: {e}")
                error_ref["value"] = e

        # 启动识别会话
        def _start_recognition():
            try:
                recognition.start()
            except Exception as e:
                error_ref["value"] = e
                logger.error(f"百炼 ASR start 异常: {e}")

        await loop.run_in_executor(None, _start_recognition)

        if error_ref["value"]:
            logger.error(f"百炼 ASR 启动失败: {error_ref['value']}")
            yield ASRResult(text="", is_final=True)
            return

        logger.info(f"百炼 ASR: 识别会话已启动, model={self.model_name}")

        # 并发发送音频 + 接收结果
        send_task = asyncio.create_task(_send_audio())

        # 从队列中 yield 结果
        while True:
            try:
                item = await asyncio.wait_for(
                    loop.run_in_executor(None, result_queue.get, True, 15.0),
                    timeout=20.0,
                )
            except asyncio.TimeoutError:
                logger.warning("百炼 ASR 接收超时")
                break

            if item is None:  # 哨兵，转写完成
                break
            yield item
            if item.is_final:
                final_text = item.text

        # 停止识别
        def _stop_recognition():
            try:
                recognition.stop()
            except Exception as e:
                logger.warning(f"百炼 ASR stop 异常: {e}")

        await loop.run_in_executor(None, _stop_recognition)

        # 清理发送任务
        send_task.cancel()
        await asyncio.gather(send_task, return_exceptions=True)

        # 保存调试音频
        if dump_pcm:
            try:
                dur = len(dump_pcm) / (self.input_sample_rate * 2)
                text_tag = re.sub(r'[^\w\u4e00-\u9fff]', '', final_text)[:20] if final_text else "silent"
                prefix = call_uuid[:8] if call_uuid else f"asr_{int(_time.time())}"
                dump_path = os.path.join(
                    dump_dir, f"asr_{prefix}_{text_tag}_{uuid.uuid4().hex[:4]}.wav"
                )
                with wave.open(dump_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(self.input_sample_rate)
                    wf.writeframes(dump_pcm)
                logger.info(
                    f"百炼 ASR 音频已保存: {dump_path} "
                    f"({len(dump_pcm)} bytes, {dur:.1f}s, text='{final_text}')"
                )
            except Exception as e:
                logger.warning(f"百炼 ASR WAV 保存失败: {e}")


def create_asr_client(cfg: Optional[ASRConfig] = None) -> BaseASR:
    """工厂函数：根据配置创建对应的 ASR 客户端"""
    cfg = cfg or config.asr

    if cfg.provider == "funasr_local":
        logger.info("使用 FunASR 本地服务")
        return FunASRClient(cfg)
    elif cfg.provider == "xunfei":
        logger.info("使用讯飞实时语音转写")
        return XunfeiASRClient(cfg)
    elif cfg.provider == "ali":
        logger.info(f"使用阿里云 NLS 实时转写 (appkey={cfg.ali_asr_appkey[:6]}...)")
        return AliASRClient(cfg)
    elif cfg.provider == "bailian":
        logger.info("使用阿里云百炼 ASR 服务")
        return BailianASRClient(cfg)
    elif cfg.provider == "mock":
        logger.info("使用 Mock ASR（仅开发测试）")
        return MockASRClient()
    else:
        raise ValueError(f"不支持的 ASR 提供商: {cfg.provider}")
