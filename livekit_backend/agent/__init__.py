"""LiveKit Agent 模块

包含外呼 Agent Worker、对话 Agent 核心逻辑和对话管理器。

注意：不要在 __init__.py 中 eager import worker / outbound_agent，
否则 python -m livekit_backend.agent.worker 启动时会触发
RuntimeWarning: module found in sys.modules prior to execution。
"""


def __getattr__(name):
    """延迟导入，避免 -m 运行时的 sys.modules 冲突"""
    if name == "DialogManager":
        from livekit_backend.agent.dialog_manager import DialogManager
        return DialogManager
    if name == "BargeInConfig":
        from livekit_backend.agent.dialog_manager import BargeInConfig
        return BargeInConfig
    if name == "ToleranceConfig":
        from livekit_backend.agent.dialog_manager import ToleranceConfig
        return ToleranceConfig
    if name == "NoResponseConfig":
        from livekit_backend.agent.dialog_manager import NoResponseConfig
        return NoResponseConfig
    if name == "OutboundCallAgent":
        from livekit_backend.agent.outbound_agent import OutboundCallAgent
        return OutboundCallAgent
    if name == "OutboundAgentWorker":
        from livekit_backend.agent.worker import OutboundAgentWorker
        return OutboundAgentWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DialogManager",
    "BargeInConfig",
    "ToleranceConfig",
    "NoResponseConfig",
    "OutboundCallAgent",
    "OutboundAgentWorker",
]
