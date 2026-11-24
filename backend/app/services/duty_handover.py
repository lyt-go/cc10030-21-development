"""值守交接服务入口；核心实现见 handover_core。"""
from app.services.handover_core import *  # noqa: F401,F403
from app.services.handover_core import (  # noqa: F401
    DutyHandoverService,
    HandoverEngine,
    HandoverService,
    ShiftHandoverService,
    TrackHandoverService,
)
from app.services import handover_core as _core


def __getattr__(name: str):
    return getattr(_core, name)
