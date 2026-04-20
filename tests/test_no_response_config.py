"""
无回应配置功能测试
"""
import pytest
from types import SimpleNamespace


class TestGetNoResponseConfig:
    @pytest.mark.asyncio
    async def test_returns_defaults_when_script_not_found(self, monkeypatch):
        from backend.services import async_script_utils as utils

        async def fake_get_script(script_id):
            return None

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        config = await utils.get_no_response_config("nonexistent")

        assert config["timeout"] == 3
        assert config["mode"] == "consecutive"
        assert config["max_count"] == 3
        assert config["hangup_msg"] is None
        assert config["hangup_enabled"] is True
        assert config["closing_script"] is None

    @pytest.mark.asyncio
    async def test_returns_script_config_values(self, monkeypatch):
        from backend.services import async_script_utils as utils

        fake_script = SimpleNamespace(
            no_response_timeout=5,
            no_response_mode="cumulative",
            no_response_max_count=5,
            no_response_hangup_msg="自定义挂断语",
            no_response_hangup_enabled=True,
            closing_script="结束语",
        )

        async def fake_get_script(script_id):
            return fake_script

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        config = await utils.get_no_response_config("test_script")

        assert config["timeout"] == 5
        assert config["mode"] == "cumulative"
        assert config["max_count"] == 5
        assert config["hangup_msg"] == "自定义挂断语"
        assert config["hangup_enabled"] is True
        assert config["closing_script"] == "结束语"

    @pytest.mark.asyncio
    async def test_invalid_mode_falls_back_to_consecutive(self, monkeypatch):
        from backend.services import async_script_utils as utils

        fake_script = SimpleNamespace(
            no_response_timeout=3,
            no_response_mode="invalid_mode",
            no_response_max_count=3,
            no_response_hangup_msg=None,
            no_response_hangup_enabled=None,
            closing_script=None,
        )

        async def fake_get_script(script_id):
            return fake_script

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        config = await utils.get_no_response_config("test_script")

        assert config["mode"] == "consecutive"
        assert config["hangup_enabled"] is True

    @pytest.mark.asyncio
    async def test_none_timeout_falls_back_to_default(self, monkeypatch):
        from backend.services import async_script_utils as utils

        fake_script = SimpleNamespace(
            no_response_timeout=None,
            no_response_mode="consecutive",
            no_response_max_count=None,
            no_response_hangup_msg=None,
            no_response_hangup_enabled=None,
            closing_script=None,
        )

        async def fake_get_script(script_id):
            return fake_script

        monkeypatch.setattr(utils.script_service, "get_script", fake_get_script)
        config = await utils.get_no_response_config("test_script")

        assert config["timeout"] == 3
        assert config["max_count"] == 3
