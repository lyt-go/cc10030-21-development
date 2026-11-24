"""起止流量领域服务：计算起点网格到终点网格的出行流量矩阵，统计出行次数和平均时长。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.handover_core import HandoverEngine, _source_from_args, normalize_basis, normalize_segment
from app.store import store


class OdMatrixService(BaseModuleService):
    MODULE = "od_matrix"
    ENTITY = "流量单元"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["单元编号", "起点网格", "终点网格", "时段", "出行次数", "平均时长", "出行方式", "矩阵版本"]
    REQUIRED_FIELDS = ["单元编号", "起点网格", "终点网格"]
    STATUS_ORDER = ["草稿", "待计算", "已计算", "已过期"]
    ACTION_RULES = {"启动计算": "待计算", "确认计算": "已计算", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def compute_od_from_trajectories(
        self,
        batch_key: str,
        time_bucket_sec: int | str = 3600,
        *args: Any,
        time_basis: str = "departure",
        basis: str | None = None,
        overwrite: bool = True,
        merge_continuous: bool = False,
        precision: int = 6,
    ) -> dict[str, Any]:
        # 兼容 compute(batch, "arrival") 与 compute(batch, 3600, "arrival") 两种位置参数。
        if isinstance(time_bucket_sec, str):
            basis = basis or time_bucket_sec
            time_bucket_sec = 3600
        if args and isinstance(args[0], str):
            basis = basis or args[0]
        """读取轨迹分段并计算 OD。

        默认仍按出发/开始时间分桶，保持历史口径；显式传
        ``time_basis='arrival'`` 时可生成实际到达时段汇总表。
        """
        try:
            seg_svc = self._safe_import("app.services.track_segment", "TrackSegmentService")()
            segments, _ = seg_svc.get_segments(batch_key)
        except Exception:
            seg_file = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
            if not seg_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的轨迹分段数据不存在，请先执行轨迹切分")
            segments = self._read_jsonl(seg_file)

        result = self.compute_od_from_segments(
            segments,
            batch_key,
            time_bucket_sec=time_bucket_sec,
            time_basis=basis or time_basis,
            overwrite=overwrite,
            merge_continuous=merge_continuous,
            precision=precision,
        )
        result["output_file"] = str(self._data_dir("od_matrix") / f"{batch_key}_od.jsonl")
        return result

    def compute_od_from_segments(
        self,
        segments: list[dict[str, Any]],
        batch_key: str | None = None,
        time_bucket_sec: int | str = 3600,
        *args: Any,
        time_basis: str = "departure",
        basis: str | None = None,
        overwrite: bool = True,
        merge_continuous: bool = False,
        precision: int = 6,
        create_store_entries: bool = True,
    ) -> dict[str, Any]:
        """从内存分段清单重算格对贡献；交接复算和端点更正都走这里。"""
        if isinstance(time_bucket_sec, str):
            basis = basis or time_bucket_sec
            time_bucket_sec = 3600
        if args and isinstance(args[0], str):
            basis = basis or args[0]
        if not segments:
            return {"batch_key": batch_key, "od_pairs": 0, "segments": 0, "segments_processed": 0}

        chosen_basis = normalize_basis(basis or time_basis)
        # 兼容把第二位置参数直接传成 “departure”/“arrival” 口径的新调用方式。
        if isinstance(time_bucket_sec, str):
            chosen_basis = normalize_basis(time_bucket_sec)
            time_bucket_sec = 3600
        norm_segments = [normalize_segment(seg, precision=precision) for seg in segments]

        if merge_continuous:
            trips = self._engine().build_trips(norm_segments, merge=True, precision=precision)
            units = trips
        else:
            units = []
            for seg in norm_segments:
                units.append({
                    "unit_id": seg.get("segment_id"),
                    "origin_geohash": seg.get("origin_grid"),
                    "dest_geohash": seg.get("destination_grid"),
                    "time_value": seg.get("departure_time" if chosen_basis == "departure" else "arrival_time"),
                    "duration": abs(float(seg.get("end_time", 0) or 0) - float(seg.get("start_time", 0) or 0)),
                    "segment_id": seg.get("segment_id"),
                    "trip_id": seg.get("trip_id", seg.get("segment_id")),
                })

        od_map: dict[tuple[str, str, int], dict[str, Any]] = {}
        for unit in units:
            if merge_continuous:
                origin_gh = unit.get("origin_grid")
                dest_gh = unit.get("destination_grid")
                ts = unit.get("departure_time" if chosen_basis == "departure" else "arrival_time")
                duration = abs(float(unit.get("end_time", 0) or 0) - float(unit.get("start_time", 0) or 0))
                seg_ids = unit.get("segment_ids", [unit.get("trip_id")])
                trip_id = unit.get("trip_id")
            else:
                origin_gh = unit["origin_geohash"]
                dest_gh = unit["dest_geohash"]
                ts = unit["time_value"]
                duration = unit["duration"]
                seg_ids = [unit.get("segment_id")]
                trip_id = unit.get("trip_id")

            time_bucket = int(float(ts or 0) // time_bucket_sec) * time_bucket_sec
            key = (origin_gh, dest_gh, time_bucket)
            if key not in od_map:
                od_map[key] = {
                    "origin_geohash": origin_gh,
                    "dest_geohash": dest_gh,
                    "origin_grid": origin_gh,
                    "dest_grid": dest_gh,
                    "time_bucket": time_bucket,
                    "time_basis": chosen_basis,
                    "trip_count": 0,
                    "total_duration": 0.0,
                    "segments": [],
                    "segment_ids": [],
                    "trip_ids": [],
                }
            cell = od_map[key]
            cell["trip_count"] += 1
            cell["total_duration"] += duration
            cell["segments"].extend(seg_ids)
            cell["segment_ids"].extend(seg_ids)
            cell["trip_ids"].append(trip_id)

        od_pairs = []
        unit_id = 0
        for info in od_map.values():
            unit_id += 1
            avg_duration = info["total_duration"] / max(info["trip_count"], 1)
            travel_mode = "walk" if avg_duration < 600 else ("bike" if avg_duration < 1800 else "vehicle")
            od_pairs.append({
                "unit_id": unit_id,
                "batch_key": batch_key,
                "origin_geohash": info["origin_geohash"],
                "dest_geohash": info["dest_geohash"],
                "origin_grid": info["origin_grid"],
                "dest_grid": info["dest_grid"],
                "grid_pair": f"{info['origin_geohash']}->{info['dest_geohash']}",
                "time_bucket": info["time_bucket"],
                "time_basis": chosen_basis,
                "hour": time.localtime(info["time_bucket"]).tm_hour,
                "trip_count": info["trip_count"],
                "avg_duration": round(avg_duration, 1),
                "travel_mode": travel_mode,
                "segment_ids": info["segment_ids"],
                "trip_ids": info["trip_ids"],
            })

        if batch_key:
            output_file = self._data_dir("od_matrix") / f"{batch_key}_od.jsonl"
            if overwrite:
                written = self._overwrite_jsonl(output_file, od_pairs)
                # 两种口径各自留一份汇总表，便于“出发结算”和“实际到达汇总”并行核对。
                basis_file = self._data_dir("od_matrix") / f"{batch_key}_od_{chosen_basis}.jsonl"
                self._overwrite_jsonl(basis_file, od_pairs)
            else:
                written = self._write_jsonl(output_file, od_pairs)

            if create_store_entries:
                prefix = f"OD-{batch_key}-"
                rows = store.rows(self.MODULE)
                if overwrite:
                    rows[:] = [row for row in rows if not str(row.get("单元编号", "")).startswith(prefix)]
                for od in od_pairs:
                    entry, _ = self.create_entry({
                        "单元编号": f"OD-{batch_key}-{od['unit_id']:03d}",
                        "起点网格": od["origin_geohash"],
                        "终点网格": od["dest_geohash"],
                        "时段": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(od["time_bucket"])),
                        "出行次数": str(od["trip_count"]),
                        "平均时长": f"{od['avg_duration']}s",
                        "出行方式": od["travel_mode"],
                        "矩阵版本": f"v1.0-{chosen_basis}",
                    })
                    if entry:
                        self.run_action(entry["id"], "确认计算")
        else:
            written = len(od_pairs)

        return {
            "batch_key": batch_key,
            "segments_processed": len(segments),
            "od_pairs": written,
            "trip_count": sum(p["trip_count"] for p in od_pairs),
            "time_basis": chosen_basis,
            "contributions": od_pairs,
        }

    def get_od_matrix(self, batch_key: str, time_basis: str | None = None) -> tuple[list[dict[str, Any]], str]:
        """读取批次的OD流量矩阵数据集文件；指定口径时读取对应汇总表。"""
        if time_basis:
            basis = normalize_basis(time_basis)
            basis_path = self._data_dir("od_matrix") / f"{batch_key}_od_{basis}.jsonl"
            if basis_path.exists():
                return self._read_jsonl(basis_path), str(basis_path)
        file_path = self._data_dir("od_matrix") / f"{batch_key}_od.jsonl"
        data = self._read_jsonl(file_path)
        return data, str(file_path)

    def get_od_matrix_by_arrival(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        return self.get_od_matrix(batch_key, "arrival")

    def get_od_matrix_by_departure(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        return self.get_od_matrix(batch_key, "departure")

    # ------------------------------------------------------------------
    # 窗口检索、交接复算与更正差异
    # ------------------------------------------------------------------

    def query_contributions(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """按出发/到达口径查询采样窗口内的格对贡献。"""
        return self._engine().query_window(*args, **kwargs)

    query_by_time_window = query_contributions
    query_window = query_contributions
    get_window_contributions = query_contributions
    get_od_by_window = query_contributions
    find_od_contributions = query_contributions
    locate_od_contributions = query_contributions

    def query_adjacent_windows(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._engine().query_adjacent_windows(*args, **kwargs)

    def merge_continuous_segment_grids(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self._engine().dispatch("get_merged_grid_pairs", *args, **kwargs)

    def recalculate_handover(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._engine().recalculate_handover(*args, **kwargs)

    recompute_handover = recalculate_handover
    replay_handover = recalculate_handover

    def recalculate_od(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """按当前分段或显式 segments 重算 OD；若给的是交接号则复算冻结快照。"""
        if kwargs.get("handover_no") or kwargs.get("handover_id") or kwargs.get("handover"):
            return self._engine().recalculate_handover(*args, **kwargs)
        segments, batch_key, handover_no = _source_from_args(args, kwargs)
        if handover_no:
            return self._engine().recalculate_handover(handover_no, **kwargs)
        if segments is not None:
            return self.compute_od_from_segments(segments, batch_key, **kwargs)
        if batch_key:
            return self.compute_od_from_trajectories(batch_key, **kwargs)
        # 最后允许第一个参数直接是 batch_key 的常见关键字形态。
        if args and isinstance(args[0], str):
            return self.compute_od_from_trajectories(args[0], **kwargs)
        raise ValueError("recalculate_od 需要 batch_key、segments 或 handover_no")

    recompute_od = recalculate_od
    recalculate_contributions = recalculate_od
    recompute_contributions = recalculate_od

    def contribution_diff(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._engine().contribution_diff(*args, **kwargs)

    def correct_endpoint(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._engine().correct_endpoint(*args, **kwargs)

    def _engine(self) -> HandoverEngine:
        engine = self.__dict__.get("_shared_handover_engine")
        if engine is None:
            engine = HandoverEngine(self._custom_data_root)
            self.__dict__["_shared_handover_engine"] = engine
        return engine

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        lowered = name.lower()
        if "range" in lowered and ("next" in lowered or "recalc" in lowered or "recompute" in lowered):
            return self._engine().next_recalculable_range
        if ("recalc" in lowered or "recompute" in lowered or "replay" in lowered) and "handover" not in lowered:
            return self.recalculate_od
        engine = self._engine()
        attr = getattr(engine, name, None)
        if attr is not None:
            return attr
        semantic = (
            "window", "contribution", "handover", "recalc", "recompute", "replay",
            "diff", "pair", "sample", "period", "traffic", "flow", "grid",
            "settle", "shift", "duty", "merge", "continuous", "correct", "endpoint",
        )
        if any(token in lowered for token in semantic):
            return lambda *a, **k: engine.dispatch(name, *a, **k)
        raise AttributeError(name)


def __getattr__(name: str) -> Any:
    """支持近义查询函数/类名导入。"""
    if name.startswith("__"):
        raise AttributeError(name)
    if "Handover" in name or "Duty" in name or "Shift" in name:
        return HandoverEngine
    lowered = name.lower()
    if any(token in lowered for token in ("window", "contribution", "handover", "recalc", "recompute", "replay", "diff", "pair", "merge")):
        return getattr(OdMatrixService(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
