"""
本地测试入口

用途：
1. 统一关闭宿主机里自动注入的第三方 pytest 插件
2. 复用项目内的 pytest.ini 和测试依赖

用法：
    python scripts/run_tests.py -q
    python scripts/run_tests.py tests/test_all.py -q
"""

import os
import subprocess
import sys


def main() -> int:
    env = os.environ.copy()
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    cmd = [sys.executable, "-m", "pytest", *sys.argv[1:]]
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
