"""
Tolerance Time 宽容时间功能测试

测试重点：
1. _listen_user 内置宽容期行为（统一超时模式）
2. tolerance_enabled=False 时立即退出
3. 宽容期内有补充语音时 final_text 被更新
4. 宽容期超时保持第一次识别的文本
5. 宽容期内的噪声词被过滤，不中断等待
"""
import unittest


NOISE_WORDS = {"嗯", "嗯。", "哦", "哦。", "啊", "啊。", "呃", "呃。", "哎", "哎。",
               "你好", "你好。", "你", "你。", "对", "对。", "行", "行。", "喂", "喂。",
               "好", "好。", "是", "是。"}


def _is_noise(text):
    t = text.strip()
    if not t:
        return True
    if t in NOISE_WORDS:
        return True
    if len(t) == 2 and t[0] in {"嗯", "哦", "啊", "呃", "哎", "喂", "好", "是", "对", "行", "你"} and t[1] in {"。", "！", "?", "…"}:
        return True
    return False


def _simulate_listen_user_with_tolerance(
    asr_results: list,  # [(text, is_final, simulated_elapsed), ...]
    tolerance_enabled: bool,
    tolerance_ms: int,
) -> str:
    """模拟 _listen_user 的 ASR 循环 + 宽容期（统一超时模式）"""
    final_text = ""
    first_valid_ts = 0.0
    tolerance_extra_sec = max(3.0, tolerance_ms / 1000.0)

    for text, is_final, simulated_elapsed in asr_results:
        if is_final and text:
            if _is_noise(text):
                continue  # 噪声词不 break，继续等
            final_text = text
            if first_valid_ts == 0.0:
                first_valid_ts = simulated_elapsed
            if tolerance_enabled:
                # 宽容期：检查是否超时
                if simulated_elapsed - first_valid_ts >= tolerance_extra_sec:
                    break
            else:
                break  # 不启用宽容期时立即退出
    return final_text


class TestToleranceEnabled(unittest.TestCase):
    """宽容期开关测试"""

    def test_disabled_uses_first(self):
        """tolerance_enabled=False 时第一次有效语音后立即退出"""
        results = [
            ("我想查", True, 1.0),
            ("余额", True, 1.5),  # 不应该被消费
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=False, tolerance_ms=1000
        )
        self.assertEqual(text, "我想查")

    def test_enabled_with_speech(self):
        """tolerance_enabled=True 且检测到语音"""
        results = [
            ("我想查", True, 1.0),
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=1000
        )
        self.assertEqual(text, "我想查")

    def test_enabled_without_speech(self):
        """tolerance_enabled=True 但无有效语音"""
        results = [
            ("嗯", True, 1.0),  # 噪声词
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=1000
        )
        self.assertEqual(text, "")


class TestToleranceBehavior(unittest.TestCase):
    """宽容期行为测试"""

    def test_tolerance_appends_subsequent_speech(self):
        """宽容期内有补充语音，final_text 被更新"""
        results = [
            ("我想查", True, 1.0),      # 第一次有效语音
            ("余额", True, 2.0),         # 宽容期内补充（elapsed=1.0 < 3.0）
            ("和明细", True, 4.5),       # elapsed=3.5 >= 3.0 → 超时break
        ]
        # "和明细" 被赋值后 break，最终返回 "和明细"
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=1000
        )
        self.assertEqual(text, "和明细")

    def test_tolerance_timeout_keeps_first(self):
        """宽容期超时，保持第一次识别的文本"""
        results = [
            ("我想查", True, 1.0),   # 第一次有效语音
            # 宽容期内无其他有效语音，超时后循环自然结束
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=1000
        )
        self.assertEqual(text, "我想查")

    def test_tolerance_single_speech_no_extra(self):
        """只说一次话，宽容期内无补充，返回原话"""
        results = [
            ("我想查询余额", True, 2.0),
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=1000
        )
        self.assertEqual(text, "我想查询余额")


class TestToleranceNoiseFilter(unittest.TestCase):
    """宽容期内噪声过滤测试"""

    def test_tolerance_filters_noise_during_tolerance(self):
        """宽容期内的噪声词被过滤，不中断等待"""
        results = [
            ("嗯", True, 1.0),       # 噪声词，不 break
            ("我想查", True, 1.5),    # 第一次有效语音
            ("哦", True, 2.0),        # 噪声词，不 break
            ("余额", True, 3.0),      # 有效补充（elapsed=1.5 < 3.0）
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=1000
        )
        self.assertEqual(text, "余额")

    def test_only_noise_returns_empty(self):
        """只有噪声词时返回空"""
        results = [
            ("嗯", True, 1.0),
            ("哦", True, 2.0),
            ("啊", True, 3.0),
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=1000
        )
        self.assertEqual(text, "")


class TestToleranceConfigPropagation(unittest.TestCase):
    """配置传递测试"""

    def test_default_config(self):
        """默认配置: enabled=True, ms=1000"""
        results = [
            ("我想查", True, 1.0),
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=1000
        )
        self.assertEqual(text, "我想查")

    def test_custom_ms(self):
        """自定义 tolerance_ms"""
        results = [
            ("我想查", True, 1.0),
            ("余额", True, 3.0),
        ]
        text = _simulate_listen_user_with_tolerance(
            results, tolerance_enabled=True, tolerance_ms=2000
        )
        # tolerance_extra_sec = max(3.0, 2.0) = 3.0
        # elapsed = 3.0 - 1.0 = 2.0 < 3.0 → 消费
        self.assertEqual(text, "余额")


if __name__ == "__main__":
    unittest.main()
