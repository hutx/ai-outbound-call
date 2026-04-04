"""
TTS 缓存清理工具
backend/utils/tts_cache.py

功能：
  - 定期清理超过 TTL 的 TTS 音频缓存文件
  - 限制缓存目录总大小（超过阈值时删最老的文件）
  - 可作为后台任务由 main.py 启动，也可独立运行

使用方式（main.py lifespan 中）：
    from backend.utils.tts_cache import start_cache_cleaner
    cleaner_task = asyncio.create_task(start_cache_cleaner())
    yield
    cleaner_task.cancel()
"""
import asyncio
import logging
import os
import time
from pathlib import Path

from backend.core.config import config

logger = logging.getLogger(__name__)

# 缓存文件最长保留时间（秒），默认 2 小时
CACHE_TTL_SECONDS = int(os.environ.get("TTS_CACHE_TTL", 7200))
# 缓存目录最大总大小（字节），默认 500 MB
CACHE_MAX_BYTES = int(os.environ.get("TTS_CACHE_MAX_MB", 500)) * 1024 * 1024
# 清理间隔（秒）
CLEAN_INTERVAL = int(os.environ.get("TTS_CACHE_CLEAN_INTERVAL", 300))  # 5 分钟


def _get_cache_files(cache_dir: str) -> list[tuple[float, int, Path]]:
    """
    扫描缓存目录，返回 (mtime, size, path) 列表，按 mtime 升序（最老的在前）
    """
    result = []
    try:
        for p in Path(cache_dir).glob("tts_*.wav"):
            if p.is_file():
                stat = p.stat()
                result.append((stat.st_mtime, stat.st_size, p))
    except Exception as e:
        logger.warning(f"TTS 缓存扫描失败: {e}")
    result.sort(key=lambda x: x[0])
    return result


def clean_cache_sync(cache_dir: str) -> tuple[int, int]:
    """
    同步清理（可在线程池中调用）
    返回: (删除文件数, 释放字节数)
    """
    files = _get_cache_files(cache_dir)
    if not files:
        return 0, 0

    now = time.time()
    deleted_count = 0
    freed_bytes = 0

    # 1. 按 TTL 删除过期文件
    for mtime, size, path in files:
        if now - mtime > CACHE_TTL_SECONDS:
            try:
                path.unlink(missing_ok=True)
                deleted_count += 1
                freed_bytes += size
            except Exception as e:
                logger.warning(f"删除缓存文件失败 {path}: {e}")

    # 2. 按总大小限制删除最老的文件
    files = _get_cache_files(cache_dir)  # 重新扫描
    total_size = sum(s for _, s, _ in files)

    if total_size > CACHE_MAX_BYTES:
        for mtime, size, path in files:
            if total_size <= CACHE_MAX_BYTES:
                break
            try:
                path.unlink(missing_ok=True)
                total_size -= size
                deleted_count += 1
                freed_bytes += size
            except Exception as e:
                logger.warning(f"删除缓存文件失败 {path}: {e}")

    return deleted_count, freed_bytes


async def start_cache_cleaner():
    """
    后台缓存清理协程
    在 main.py lifespan 中以 create_task 启动
    """
    cache_dir = config.tts.output_dir
    logger.info(f"TTS 缓存清理器启动: dir={cache_dir} TTL={CACHE_TTL_SECONDS}s max={CACHE_MAX_BYTES//1024//1024}MB")

    while True:
        await asyncio.sleep(CLEAN_INTERVAL)
        try:
            loop = asyncio.get_event_loop()
            count, freed = await loop.run_in_executor(None, clean_cache_sync, cache_dir)
            if count > 0:
                logger.info(
                    f"TTS 缓存清理: 删除 {count} 个文件，"
                    f"释放 {freed / 1024 / 1024:.1f} MB"
                )
        except asyncio.CancelledError:
            logger.info("TTS 缓存清理器已停止")
            break
        except Exception as e:
            logger.error(f"TTS 缓存清理异常: {e}")


if __name__ == "__main__":
    # 独立运行示例
    import sys
    cache_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tts_cache"
    count, freed = clean_cache_sync(cache_dir)
    print(f"清理完成: 删除 {count} 个文件，释放 {freed/1024/1024:.1f} MB")
