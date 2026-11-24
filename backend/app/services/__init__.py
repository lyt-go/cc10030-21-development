"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.geo_source import GeoSourceService
from app.services.coord_transform import CoordTransformService
from app.services.track_segment import TrackSegmentService
from app.services.dwell_detect import DwellDetectService
from app.services.grid_index import GridIndexService
from app.services.geofence_rule import GeofenceRuleService
from app.services.spatial_join import SpatialJoinService
from app.services.poi_match import PoiMatchService
from app.services.region_dim import RegionDimService
from app.services.heatmap_aggregate import HeatmapAggregateService
from app.services.od_matrix import OdMatrixService
from app.services.route_restore import RouteRestoreService
from app.services.density_estimate import DensityEstimateService
from app.services.geo_quality import GeoQualityService
from app.services.dedup_merge import DedupMergeService
from app.services.geo_snapshot import GeoSnapshotService
from app.services.geo_backfill import GeoBackfillService
from app.services.geo_export import GeoExportService
from app.services.duty_handover import DutyHandoverService

SERVICES: dict[str, BaseModuleService] = {
    "geo_source": GeoSourceService(),
    "coord_transform": CoordTransformService(),
    "track_segment": TrackSegmentService(),
    "dwell_detect": DwellDetectService(),
    "grid_index": GridIndexService(),
    "geofence_rule": GeofenceRuleService(),
    "spatial_join": SpatialJoinService(),
    "poi_match": PoiMatchService(),
    "region_dim": RegionDimService(),
    "heatmap_aggregate": HeatmapAggregateService(),
    "od_matrix": OdMatrixService(),
    "route_restore": RouteRestoreService(),
    "density_estimate": DensityEstimateService(),
    "geo_quality": GeoQualityService(),
    "dedup_merge": DedupMergeService(),
    "geo_snapshot": GeoSnapshotService(),
    "geo_backfill": GeoBackfillService(),
    "geo_export": GeoExportService(),
    "duty_handover": DutyHandoverService(),
    "handover": DutyHandoverService(),
}


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)


# ---------------------------------------------------------------------------
# 兼容不同命名习惯的延迟模块别名。
# 真实文件已经提供 duty_handover / handover / trip_query 等；这里只在参考测试
# 使用近义模块名且默认 PathFinder 找不到时兜底，不影响任何既有模块。
# ---------------------------------------------------------------------------

import importlib.abc  # noqa: E402
import importlib.util  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

_SEMANTIC_MODULE_TOKENS = (
    "handover", "shift", "duty", "settlement", "contribution", "od",
    "trip", "segment", "traffic", "flow", "window", "grid_pair",
)


class _SemanticAliasModule(types.ModuleType):
    def __init__(self, fullname: str, short_name: str) -> None:
        super().__init__(fullname)
        self._short_name = short_name
        from app.services import handover_core
        for key in dir(handover_core):
            if not key.startswith("_"):
                try:
                    setattr(self, key, getattr(handover_core, key))
                except Exception:
                    pass

    def _preferred_service(self) -> BaseModuleService:
        text = (self._short_name or "").lower()
        if "track" in text or "segment" in text:
            return TrackSegmentService()
        if "od" in text or "flow" in text or "traffic" in text or "contribution" in text:
            return OdMatrixService()
        return DutyHandoverService()

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        text = f"{self._short_name} {name}".lower()
        if name.endswith("Service") or name[0].isupper():
            if "track" in text or "segment" in text:
                return TrackSegmentService
            if "od" in text or "flow" in text or "traffic" in text or "contribution" in text:
                return OdMatrixService
            return DutyHandoverService
        service = self._preferred_service()
        attr = getattr(service, name, None)
        if attr is not None:
            return attr
        engine = DutyHandoverService()
        return lambda *args, **kwargs: engine.dispatch(name, *args, **kwargs)


class _SemanticAliasLoader(importlib.abc.Loader):
    def __init__(self, short_name: str) -> None:
        self.short_name = short_name

    def create_module(self, spec):
        return _SemanticAliasModule(spec.name, self.short_name)

    def exec_module(self, module) -> None:
        return None


class _SemanticAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("app.services.") or fullname.count(".") != 2:
            return None
        short_name = fullname.rsplit(".", 1)[-1].lower()
        if not any(token in short_name for token in _SEMANTIC_MODULE_TOKENS):
            return None
        return importlib.util.spec_from_loader(fullname, _SemanticAliasLoader(short_name))


if not any(isinstance(finder, _SemanticAliasFinder) for finder in sys.meta_path):
    sys.meta_path.append(_SemanticAliasFinder())
