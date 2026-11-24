"""handover 命名兼容模块。"""
from app.services.handover_core import *  # noqa: F401,F403
from app.services.handover_core import HandoverEngine, HandoverService  # noqa: F401
from app.services import handover_core as _core


def __getattr__(name: str):
    return getattr(_core, name)
