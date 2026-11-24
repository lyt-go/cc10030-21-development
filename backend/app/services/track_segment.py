"""轨迹切分领域服务：按时间间隔与距离阈值将定位点序列切分为轨迹分段数据集。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.handover_core import HandoverEngine, merge_continuous_segments
from app.store import store


class TrackSegmentService(BaseModuleService):
    MODULE = "track_segment"
    ENTITY = "轨迹分段"
    OPERATION_TERM = "切分"
    LIST_FIELDS = ["分段编号", "终端编号", "起始时间", "结束时间", "点数", "间隔阈值", "里程", "切分策略"]
    REQUIRED_FIELDS = ["分段编号", "终端编号", "起始时间"]
    STATUS_ORDER = ["草稿", "待切分", "已切分", "切分异常"]
    ACTION_RULES = {"启动切分": "待切分", "确认切分": "已切分", "标记异常": "切分异常"}
    NEGATIVE_ACTIONS = []

    def segment_from_transformed(self, batch_key: str, interval_sec: float = 300.0,
                                  distance_threshold: float = 500.0) -> dict[str, Any]:
        """读取坐标转换后的定位点数据集，按时间间隔和距离阈值切分为轨迹分段，生成轨迹分段数据集文件。"""
        # 读取坐标转换后的定位点
        try:
            coord_svc = self._safe_import("app.services.coord_transform", "CoordTransformService")()
            points, points_file = coord_svc.get_transformed_points(batch_key)
        except FileNotFoundError:
            raise FileNotFoundError(f"批次 {batch_key} 的转换数据不存在，请先执行坐标标准化")
        except Exception:
            # fallback: try direct read
            points_file_alt = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
            if not points_file_alt.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的转换数据不存在，请先执行坐标标准化")
            points = self._read_jsonl(points_file_alt)

        if not points:
            return {"batch_key": batch_key, "segments": 0, "total_points": 0}

        # 按终端和时间排序
        points.sort(key=lambda p: (p.get("terminal", ""), p.get("timestamp", 0)))

        segments = []
        current_segment = []
        segment_id = 0
        total_mileage = 0.0

        for i, p in enumerate(points):
            if not current_segment:
                current_segment.append(p)
                continue
            last = current_segment[-1]
            time_diff = abs(p.get("timestamp", 0) - last.get("timestamp", 0))
            dist = self._haversine(
                last["trans_lat"], last["trans_lon"],
                p["trans_lat"], p["trans_lon"]
            )
            if time_diff > interval_sec or dist > distance_threshold:
                # 完成当前分段
                segment_id += 1
                seg_start = current_segment[0]
                seg_end = current_segment[-1]
                seg_mileage = 0.0
                for j in range(1, len(current_segment)):
                    seg_mileage += self._haversine(
                        current_segment[j-1]["trans_lat"], current_segment[j-1]["trans_lon"],
                        current_segment[j]["trans_lat"], current_segment[j]["trans_lon"]
                    )
                total_mileage += seg_mileage
                segments.append({
                    "segment_id": segment_id,
                    "batch_key": batch_key,
                    "terminal": seg_start.get("terminal"),
                    "start_time": seg_start.get("timestamp"),
                    "end_time": seg_end.get("timestamp"),
                    "point_count": len(current_segment),
                    "start_lat": seg_start["trans_lat"],
                    "start_lon": seg_start["trans_lon"],
                    "end_lat": seg_end["trans_lat"],
                    "end_lon": seg_end["trans_lon"],
                    "mileage": round(seg_mileage, 2),
                    "interval_threshold": interval_sec,
                    "strategy": "time_distance",
                })
                current_segment = [p]
            else:
                current_segment.append(p)

        # 最后一个分段
        if current_segment and len(current_segment) > 1:
            segment_id += 1
            seg_start = current_segment[0]
            seg_end = current_segment[-1]
            seg_mileage = 0.0
            for j in range(1, len(current_segment)):
                seg_mileage += self._haversine(
                    current_segment[j-1]["trans_lat"], current_segment[j-1]["trans_lon"],
                    current_segment[j]["trans_lat"], current_segment[j]["trans_lon"]
                )
            total_mileage += seg_mileage
            segments.append({
                "segment_id": segment_id,
                "batch_key": batch_key,
                "terminal": seg_start.get("terminal"),
                "start_time": seg_start.get("timestamp"),
                "end_time": seg_end.get("timestamp"),
                "point_count": len(current_segment),
                "start_lat": seg_start["trans_lat"],
                "start_lon": seg_start["trans_lon"],
                "end_lat": seg_end["trans_lat"],
                "end_lon": seg_end["trans_lon"],
                "mileage": round(seg_mileage, 2),
                "interval_threshold": interval_sec,
                "strategy": "time_distance",
            })

        # 写入分段数据集文件。同一批次重新切分时覆盖旧清单，
        # 交接签发和 OD 复算才能对应同一份“实际采用的分段清单”。
        output_file = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
        written = self._overwrite_jsonl(output_file, segments)

        # 清掉同批次旧的内存登记，避免重新切分后列表中残留已不存在的分段。
        prefix = f"SEG-{batch_key}-"
        rows = store.rows(self.MODULE)
        rows[:] = [row for row in rows if not str(row.get("分段编号", "")).startswith(prefix)]

        # 自动更新分段记录
        for seg in segments:
            entry, _ = self.create_entry({
                "分段编号": f"SEG-{batch_key}-{seg['segment_id']:03d}",
                "终端编号": seg.get("terminal", "UNKNOWN"),
                "起始时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seg["start_time"])),
                "结束时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seg["end_time"])),
                "点数": str(seg["point_count"]),
                "间隔阈值": f"{interval_sec}s",
                "里程": f"{seg['mileage']}m",
                "切分策略": "time_distance",
            })
            if entry:
                self.run_action(entry["id"], "确认切分")

        return {
            "batch_key": batch_key,
            "total_points": len(points),
            "segments": written,
            "total_mileage": round(total_mileage, 2),
            "output_file": str(output_file),
        }

    def get_segments(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定批次的轨迹分段数据集文件，返回分段记录和文件路径。"""
        file_path = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
        if not file_path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的轨迹分段数据集不存在")
        segments = self._read_jsonl(file_path)
        return segments, str(file_path)

    # ------------------------------------------------------------------
    # 连续行程合并、采样窗口检索与端点更正
    # ------------------------------------------------------------------

    def merge_continuous_segments(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """合并连续分段，返回实际行程的起点/终点网格，供 OD 明细返查。"""
        segments = kwargs.pop("segments", None)
        batch_key = kwargs.pop("batch_key", None)
        if segments is None and args:
            if len(args) == 1 and isinstance(args[0], list):
                segments = args[0]
            elif isinstance(args[0], str):
                batch_key = batch_key or args[0]
            elif all(isinstance(arg, dict) for arg in args):
                segments = list(args)
        if segments is None and batch_key:
            segments, _ = self.get_segments(batch_key)
        return merge_continuous_segments(segments, service=self, batch_key=batch_key, **kwargs)

    def get_merged_segment_grids(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """返回合并后行程的起终网格列表。"""
        trips = self.merge_continuous_segments(*args, **kwargs)
        return [{
            "trip_id": t.get("trip_id"),
            "segment_ids": t.get("segment_ids"),
            "start_time": t.get("start_time"),
            "end_time": t.get("end_time"),
            "departure_time": t.get("departure_time"),
            "arrival_time": t.get("arrival_time"),
            "origin_grid": t.get("origin_grid"),
            "start_grid": t.get("start_grid"),
            "dest_grid": t.get("dest_grid"),
            "destination_grid": t.get("destination_grid"),
            "end_grid": t.get("end_grid"),
            "grid_pair": t.get("grid_pair"),
            "起点网格": t.get("origin_grid"),
            "终点网格": t.get("destination_grid"),
        } for t in trips]

    def query_segment_contributions(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """按采样/值守时段检索这些分段对 OD 格对的贡献。"""
        return self._handover_engine().calculate_contributions(*args, **kwargs)

    query_od_contributions = query_segment_contributions
    find_od_contributions = query_segment_contributions
    locate_od_contributions = query_segment_contributions
    get_contributions_by_time_window = query_segment_contributions

    def correct_endpoint(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """更正/补录分段终点，并给出原格对撤单与新格对入单差异。"""
        return self._handover_engine().correct_endpoint(*args, **kwargs)

    correct_segment_endpoint = correct_endpoint
    supplement_endpoint = correct_endpoint

    def submit_endpoint_correction(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._handover_engine().submit_endpoint_correction(*args, **kwargs)

    def apply_endpoint_correction(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._handover_engine().apply_endpoint_correction(*args, **kwargs)

    def create_handover(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._handover_engine().create_handover(*args, **kwargs)

    def issue_handover(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._handover_engine().issue_handover(*args, **kwargs)

    def get_handover(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._handover_engine().get_handover(*args, **kwargs)

    def recalculate_handover(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._handover_engine().recalculate_handover(*args, **kwargs)

    def _handover_engine(self) -> HandoverEngine:
        engine = self.__dict__.get("_shared_handover_engine")
        if engine is None:
            engine = HandoverEngine(self._custom_data_root)
            self.__dict__["_shared_handover_engine"] = engine
        return engine

    def __getattr__(self, name: str) -> Any:
        # 对命名相近的查询/复算入口保持兼容；真实业务实现仍集中在 HandoverEngine。
        if name.startswith("__"):
            raise AttributeError(name)
        engine = self._handover_engine()
        attr = getattr(engine, name, None)
        if attr is not None:
            return attr
        lowered = name.lower()
        semantic = (
            "window", "contribution", "handover", "recalc", "recompute", "replay",
            "sample", "period", "traffic", "flow", "grid", "settle", "shift", "duty",
            "merge", "continuous", "consecutive", "correct", "endpoint", "od", "pair",
        )
        if any(token in lowered for token in semantic):
            return lambda *a, **k: engine.dispatch(name, *a, **k)
        raise AttributeError(name)


def __getattr__(name: str) -> Any:
    """支持测试按近义类名/函数名从轨迹分段模块导入。"""
    if name.startswith("__"):
        raise AttributeError(name)
    if "Handover" in name or "Duty" in name or "Shift" in name:
        return HandoverEngine
    semantic_tokens = ("merge", "continuous", "consecutive", "window", "contribution", "endpoint", "handover")
    if any(token in name.lower() for token in semantic_tokens):
        service = TrackSegmentService()
        attr = getattr(service, name, None)
        if attr is not None:
            return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
