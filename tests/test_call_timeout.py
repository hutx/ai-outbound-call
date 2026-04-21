"""
通话时长限制功能测试
"""
from unittest.mock import patch


class TestCallTimeoutConfig:
    """测试通话时长配置从环境变量正确读取。"""

    def test_default_values(self):
        """默认值：5 分钟总时长，20 秒缓冲。"""
        from backend.core.config import AppConfig
        with patch.dict("os.environ", {}, clear=True):
            cfg = AppConfig()
            assert cfg.max_call_duration_seconds == 300
            assert cfg.call_end_buffer_seconds == 20

    def test_custom_values_from_env(self):
        """环境变量覆盖配置。"""
        from backend.core import config as config_module
        with patch.dict("os.environ", {
            "MAX_CALL_DURATION_SECONDS": "600",
            "CALL_END_BUFFER_SECONDS": "30",
        }, clear=True):
            # 重新实例化以测试 __post_init__
            cfg = config_module.AppConfig()
            assert cfg.max_call_duration_seconds == 600
            assert cfg.call_end_buffer_seconds == 30
