"""trip_query 命名兼容模块：连续分段起终网格查询。"""
from app.services.handover_core import HandoverEngine as TripQueryService  # noqa: F401
from app.services.handover_core import merge_continuous_segments  # noqa: F401
