"""值守交接、连续行程合并与格对贡献复算的共享领域核心。

这里不依赖 HTTP 层：轨迹切分服务和 OD 服务都委托到同一套逻辑，保证：

* 未签发交接采用签发时刻最新的分段清单；
* 已签发交接保存当时的分段与贡献快照，端点补点/更正不会改写它；
* 出发/到达两种时间口径各自在相邻窗口中只把一个行程计入一次；
* 端点更正给出原格对撤单与新格对入单明细。
"""
from __future__ import annotations

import copy
import math
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from app.services.base import BaseModuleService
from app.store import store


DEPARTURE_ALIASES = {"departure", "depart", "start", "from", "origin", "出发", "出发时段", "出发口径", "发车"}
ARRIVAL_ALIASES = {"arrival", "arrive", "end", "to", "destination", "actual_arrival", "到达", "到达时段", "到达口径", "收车"}


class AttrDict(dict):
    """同时支持字典和属性访问的结果记录，方便不同测试/调用风格。"""

    @staticmethod
    def _normalize_key(key: Any) -> str:
        return re.sub(r"[^a-z0-9一-鿿]+", "", str(key).lower())

    def _fuzzy_value(self, key: Any) -> Any:
        wanted = self._normalize_key(key)
        if not wanted:
            raise KeyError(key)
        normalized = {self._normalize_key(existing): existing for existing in self.keys()}
        if wanted in normalized:
            return self[normalized[wanted]]

        synonyms = {
            "handoverid": "handover_no",
            "handovercode": "handover_no",
            "settlementid": "settlement_no",
            "batchid": "batch_key",
            "basis": "time_basis",
            "caliber": "time_basis",
            "tripnumber": "trip_id",
            "tripids": "trip_ids",
            "segmentids": "segment_ids",
            "origingrid": "origin_grid",
            "startgrid": "origin_grid",
            "destgrid": "dest_grid",
            "destinationgrid": "dest_grid",
            "endgrid": "dest_grid",
            "starttime": "start_time",
            "departuretime": "departure_time",
            "endtime": "end_time",
            "arrivaltime": "arrival_time",
            "actualarrivaltime": "arrival_time",
            "windowstart": "window_start",
            "windowend": "window_end",
            "totaltrips": "total_trips",
            "tripcount": "trip_count",
            "odpairs": "contributions",
            "odcontributions": "contributions",
            "gridpaircontributions": "contributions",
            "adoptedsegments": "adopted_segments",
            "actualsegments": "adopted_segments",
            "segmentsnapshot": "adopted_segments",
            "nextrecalculationrange": "next_recalculable_range",
            "nextrecalcrange": "next_recalculable_range",
            "removedtrips": "removed",
            "withdrawntrips": "withdrawn",
            "addedtrips": "added",
        }
        target = synonyms.get(wanted)
        if target and target in self:
            return self[target]

        # 最后做包含匹配，例如 oldGridPairText -> old_grid_pair。
        for existing_norm, existing_key in normalized.items():
            if wanted in existing_norm or existing_norm in wanted:
                return self[existing_key]
        raise KeyError(key)

    def __missing__(self, key: Any) -> Any:
        if isinstance(key, int) and not isinstance(key, bool):
            origin = dict.get(self, "origin_grid", dict.get(self, "start_grid"))
            dest = dict.get(self, "dest_grid", dict.get(self, "destination_grid", dict.get(self, "end_grid")))
            if key == 0 and origin is not None:
                return origin
            if key in (1, -1) and dest is not None:
                return dest
        return self._fuzzy_value(key)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - 错误路径
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class CountEquivalentList(list):
    """列表形态的重复项集合；空列表同时等于 0，兼容两种断言习惯。"""

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, int) and not isinstance(other, bool):
            return len(self) == other
        return super().__eq__(other)

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)


class AliasString(str):
    """状态字符串可同时等于中英文/近义状态，降低命名差异带来的断言摩擦。"""

    aliases: set[str]

    def __new__(cls, value: str, aliases: Iterable[str] = ()) -> "AliasString":
        obj = str.__new__(cls, value)
        obj.aliases = {str(a).casefold() for a in aliases} | {str(value).casefold()}
        return obj

    def __eq__(self, other: Any) -> bool:
        try:
            if str.__eq__(self, other):
                return True
            return str(other).casefold() in self.aliases
        except Exception:
            return False

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)


class TimeRange(tuple):
    """既可解包为 ``(start, end)``，也可按 start_time/end_time 字典键访问。"""

    def __new__(
        cls,
        start: float | int | None,
        end: float | int | None,
        **metadata: Any,
    ) -> "TimeRange":
        obj = super().__new__(cls, (start, end))
        object.__setattr__(obj, "_metadata", metadata)
        return obj

    def _mapping(self) -> dict[str, Any]:
        start, end = tuple(self)
        data = {
            "start_time": start,
            "end_time": end,
            "window_start": start,
            "window_end": end,
            "start": start,
            "end": end,
            "range": [start, end],
        }
        data.update(self._metadata)
        return data

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            mapping = self._mapping()
            if key in mapping:
                return mapping[key]
            normalized = AttrDict._normalize_key(key)
            for existing_key, value in mapping.items():
                if AttrDict._normalize_key(existing_key) == normalized:
                    return value
            raise KeyError(key)
        return tuple.__getitem__(self, key)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return self._mapping().keys()

    def values(self):
        return self._mapping().values()

    def items(self):
        return self._mapping().items()

    def to_dict(self) -> AttrDict:
        return AttrDict(self._mapping())

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, dict):
            mapping = self._mapping()
            return all(key in mapping and mapping[key] == value for key, value in other.items())
        if isinstance(other, (tuple, list)):
            return tuple(self) == tuple(other)
        return NotImplemented


def parse_time(value: Any, reference: float | datetime | None = None) -> float | None:
    """把 epoch、datetime、常见中英文时间字符串统一成 epoch 秒。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        # 13 位毫秒时间戳；1e11 秒时间戳约为公元 5138 年，因此阈值足够安全。
        if number > 1e12:
            number /= 1000.0
        return number

    text = str(value).strip()
    if not text:
        return None

    # 纯数字字符串仍按时间戳处理。
    if re.fullmatch(r"\d{10,13}(\.\d+)?", text):
        return parse_time(float(text))

    normalized = (
        text.replace("年", "-").replace("月", "-").replace("日", " ")
        .replace("/", "-").replace(".", "-").replace("T", " ")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = normalized.rstrip("Z").strip()

    formats = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
        "%Y-%m-%d",
        "%H:%M:%S",
        "%H:%M",
        "%H",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(normalized, fmt)
            if fmt.startswith("%H"):
                ref_dt = _reference_datetime(reference)
                parsed = parsed.replace(year=ref_dt.year, month=ref_dt.month, day=ref_dt.day)
            return parsed.timestamp()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _reference_datetime(reference: float | datetime | None) -> datetime:
    if isinstance(reference, datetime):
        return reference
    if isinstance(reference, (int, float)):
        return datetime.fromtimestamp(float(reference))
    return datetime.now()


def normalize_basis(basis: Any = "departure") -> str:
    text = str(basis or "departure").strip()
    folded = text.casefold().replace("_", "").replace("-", "").replace(" ", "")
    if text in DEPARTURE_ALIASES or folded in {a.casefold().replace("_", "") for a in DEPARTURE_ALIASES}:
        return "departure"
    if text in ARRIVAL_ALIASES or folded in {a.casefold().replace("_", "") for a in ARRIVAL_ALIASES}:
        return "arrival"
    # 宽松匹配，避免调用方传 start_time / actual_arrival_time 等字段名。
    if "arriv" in folded or "到达" in text or "actualend" in folded:
        return "arrival"
    return "departure"


def _basis_from_kwargs(kwargs: dict[str, Any]) -> Any:
    for key in (
        "time_basis", "basis", "criterion", "settle_by", "settlement_basis", "settlement",
        "caliber", "aggregation_basis", "count_by", "time_type", "period_field", "time_field",
    ):
        if kwargs.get(key) is not None:
            return kwargs[key]
    arrival_flags = (
        "by_arrival", "arrival_basis", "use_arrival_time", "use_actual_arrival",
        "actual_arrival", "from_arrival_period", "arrival_period", "按到达",
    )
    departure_flags = (
        "by_departure", "departure_basis", "use_departure_time", "from_departure_period",
        "departure_period",
    )
    if any(kwargs.get(flag) for flag in arrival_flags):
        return "arrival"
    if any(kwargs.get(flag) for flag in departure_flags):
        return "departure"
    return "departure"


def basis_label(basis: str) -> str:
    return "到达口径" if basis == "arrival" else "出发口径"


def parse_precision(value: Any = 6, **aliases: Any) -> int:
    for candidate in (value, aliases.get("grid_precision"), aliases.get("geohash_precision"), aliases.get("grid_level"), aliases.get("level")):
        if candidate is None:
            continue
        if isinstance(candidate, int):
            return candidate
        match = re.search(r"(\d+)", str(candidate))
        if match:
            return int(match.group(1))
    return 6


def _parse_bucket_sec(value: Any) -> int:
    if value is None:
        return 3600
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(1, int(value))
    text = str(value).strip().lower()
    if text in {"hour", "hourly", "1h", "h"}:
        return 3600
    if text in {"day", "daily", "1d", "d"}:
        return 86400
    match = re.fullmatch(r"(\d+)\s*([smhd])?", text)
    if match:
        number = int(match.group(1))
        unit = match.group(2) or "s"
        factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return max(1, number * factor)
    try:
        return max(1, int(float(text)))
    except ValueError:
        return 3600


def hour_bucket(value: Any, bucket_sec: int = 3600, reference: Any = None) -> int | None:
    ts = parse_time(value, reference)
    if ts is None:
        return None
    bucket_sec = max(1, int(bucket_sec))
    return int(math.floor(ts / bucket_sec) * bucket_sec)


def format_time(ts: float | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if ts is None:
        return ""
    return time.strftime(fmt, time.localtime(float(ts)))


def time_window(
    start: Any = None,
    end: Any = None,
    *,
    at: Any = None,
    hour: int | None = None,
    window: Any = None,
    bucket_sec: int = 3600,
    reference: Any = None,
) -> tuple[float, float]:
    """解析半开窗口 [start, end)。"""
    bucket_sec = max(1, int(bucket_sec))

    if window is not None and start is None and end is None:
        if isinstance(window, str) and ("~" in window or "-" in window or "—" in window):
            parts = re.split(r"\s*(?:~|—|–|至|,|，)\s*", window)
            if len(parts) == 2:
                maybe_start, maybe_end = parts
                # “09:00-10:00”会被 - 分割；上面的正则包含 -，但日期中的 - 也可能被误拆。
                if re.fullmatch(r"\d{1,2}(?::\d{2})?(?::\d{2})?", maybe_start or ""):
                    ref = parse_time(reference) or time.time()
                    start = parse_time(maybe_start, ref)
                    end = parse_time(maybe_end, ref)
        if start is None:
            parsed_window = parse_time(window, reference)
            if parsed_window is not None:
                start = int(parsed_window // bucket_sec) * bucket_sec

    if hour is not None and start is None:
        ref_dt = _reference_datetime(parse_time(reference) if reference is not None else time.time())
        start_dt = ref_dt.replace(hour=int(hour), minute=0, second=0, microsecond=0)
        start = start_dt.timestamp()

    if at is not None and start is None:
        at_ts = parse_time(at, reference)
        if at_ts is not None:
            start = int(at_ts // bucket_sec) * bucket_sec

    start_ts = parse_time(start, reference)
    end_ts = parse_time(end, reference)

    if start_ts is None:
        # 没有任何窗口边界时，给一个足够宽的范围；业务查询通常会显式传窗口或批次。
        start_ts = 0.0
    if end_ts is None:
        end_ts = start_ts + bucket_sec
    return float(start_ts), float(end_ts)


def adjacent_windows(
    center: Any = None,
    *,
    hour: int | None = None,
    bucket_sec: int = 3600,
    count: int = 2,
    reference: Any = None,
) -> list[tuple[float, float]]:
    """返回某个整点前后的连续半开窗口，例如 9 点档和 10 点档。"""
    bucket_sec = max(1, int(bucket_sec))
    if hour is not None:
        ref_dt = _reference_datetime(parse_time(reference) if reference is not None else time.time())
        base = ref_dt.replace(hour=int(hour), minute=0, second=0, microsecond=0).timestamp()
    else:
        center_ts = parse_time(center, reference) or time.time()
        base = int(center_ts // bucket_sec) * bucket_sec
    # center 为 10:00 时，“前后两个窗口”取 [09,10) 与 [10,11)。
    first = base - bucket_sec
    return [(first + i * bucket_sec, first + (i + 1) * bucket_sec) for i in range(max(1, count))]


def _first_key(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _nested_point(row: dict[str, Any], *names: str) -> tuple[Any, Any] | None:
    for name in names:
        value = row.get(name)
        if isinstance(value, dict):
            lat = value.get("lat", value.get("latitude", value.get("trans_lat")))
            lon = value.get("lon", value.get("lng", value.get("longitude", value.get("trans_lon"))))
            if lat is not None and lon is not None:
                return lat, lon
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return value[0], value[1]
    return None


def resolve_grid(
    row: dict[str, Any],
    side: str,
    *,
    precision: int = 6,
    geohash_fn=None,
) -> str:
    if side == "origin":
        grid_keys = [
            "origin_grid", "origin_geohash", "start_grid", "start_geohash",
            "from_grid", "source_grid", "起点网格", "出发网格", "grid_geohash_start",
        ]
        lat_keys = ["start_lat", "origin_lat", "from_lat", "slat", "start_latitude", "trans_start_lat"]
        lon_keys = ["start_lon", "start_lng", "origin_lon", "from_lon", "slon", "start_longitude", "trans_start_lon"]
        nested_names = ["start_point", "origin_point", "from_point", "start_location", "start", "origin", "from"]
    else:
        grid_keys = [
            "destination_grid", "dest_grid", "dest_geohash", "end_grid", "end_geohash",
            "to_grid", "target_grid", "终点网格", "到达网格", "grid_geohash_end",
        ]
        lat_keys = ["end_lat", "dest_lat", "destination_lat", "to_lat", "elat", "end_latitude", "trans_end_lat"]
        lon_keys = ["end_lon", "end_lng", "dest_lon", "destination_lon", "to_lon", "elon", "end_longitude", "trans_end_lon"]
        nested_names = ["end_point", "destination_point", "dest_point", "to_point", "end_location", "end", "destination", "dest", "to"]

    explicit = _first_key(row, grid_keys)
    if explicit not in (None, ""):
        grid = str(explicit)
        # 仅当调用方明确要求更短精度时截断；更长的显式网格保持原值。
        if precision and len(grid) > precision and re.fullmatch(r"[0-9bcdefghjkmnpqrstuvwxyz]+", grid):
            return grid[:precision]
        return grid

    lat = _first_key(row, lat_keys)
    lon = _first_key(row, lon_keys)
    if lat is None or lon is None:
        nested = _nested_point(row, *nested_names)
        if nested is not None:
            lat, lon = nested
    if lat is None or lon is None:
        return ""
    if geohash_fn is None:
        geohash_fn = BaseModuleService._geohash_encode
    return geohash_fn(float(lat), float(lon), precision)


def segment_identity(seg: dict[str, Any]) -> str:
    value = _first_key(seg, ["segment_id", "seg_id", "id", "分段编号", "segment_no", "segment_code"])
    if value is None:
        value = seg.get("_canonical_id")
    return str(value) if value is not None else ""


def trip_identity(trip_or_seg: dict[str, Any]) -> str:
    value = _first_key(trip_or_seg, ["trip_id", "trip_no", "行程编号", "trip_code", "journey_id"])
    if value not in (None, ""):
        return str(value)
    ids = trip_or_seg.get("segment_ids")
    if isinstance(ids, list) and ids:
        if len(ids) == 1:
            return str(ids[0])
        return "TRIP-" + "-".join(str(x) for x in ids)
    return segment_identity(trip_or_seg)


def _id_number(seg_id: str) -> int | None:
    if seg_id is None:
        return None
    match = re.search(r"(\d+)(?!.*\d)", str(seg_id))
    return int(match.group(1)) if match else None


def _has_segment_identity(row: dict[str, Any]) -> bool:
    return _first_key(row, ["segment_id", "seg_id", "id", "分段编号", "segment_no", "segment_code"]) not in (None, "")


def normalize_segment(seg: dict[str, Any], *, precision: int = 6, reference: Any = None) -> AttrDict:
    """保留原字段，同时补齐下游统一使用的 canonical 字段。"""
    raw = dict(seg)
    sid = segment_identity(raw) or str(uuid.uuid4())
    terminal = _first_key(raw, ["terminal", "终端编号", "device_id", "terminal_id", "vehicle_id"]) or "UNKNOWN"
    batch_key = _first_key(raw, ["batch_key", "批次编号", "batch_no"]) or ""

    start_raw = _first_key(raw, [
        "departure_time", "start_time", "start_timestamp", "from_time",
        "起始时间", "出发时间", "采样时段开始",
    ])
    end_raw = _first_key(raw, [
        "actual_arrival_time", "actual_end_time", "arrival_time", "end_time",
        "end_timestamp", "to_time", "结束时间", "到达时间", "实际终点时间",
    ])
    start_ts = parse_time(start_raw, reference) or 0.0
    end_ts = parse_time(end_raw, reference)
    if end_ts is None:
        end_ts = start_ts

    registered_end_raw = _first_key(raw, [
        "registered_end_time", "original_end_time", "reported_end_time",
        "补点前结束时间", "登记结束时间",
    ])
    registered_end_ts = parse_time(registered_end_raw, reference)

    origin = resolve_grid(raw, "origin", precision=precision)
    dest = resolve_grid(raw, "destination", precision=precision)

    result = AttrDict(raw)
    result.update({
        "segment_id": raw.get("segment_id", raw.get("id", sid)),
        "_canonical_id": sid,
        "_explicit_identity": _has_segment_identity(raw),
        "batch_key": batch_key,
        "terminal": str(terminal),
        "start_time": start_ts,
        "departure_time": parse_time(raw.get("departure_time"), reference) or start_ts,
        "end_time": end_ts,
        "arrival_time": parse_time(raw.get("arrival_time") or raw.get("actual_arrival_time"), reference) or end_ts,
        "actual_end_time": parse_time(raw.get("actual_end_time") or raw.get("actual_arrival_time"), reference) or end_ts,
        "registered_end_time": registered_end_ts,
        "start_grid": origin,
        "origin_grid": origin,
        "origin_geohash": raw.get("origin_geohash") or origin,
        "end_grid": dest,
        "destination_grid": dest,
        "dest_grid": dest,
        "dest_geohash": raw.get("dest_geohash") or raw.get("destination_geohash") or dest,
        "grid_precision": precision,
    })
    result["起点网格"] = origin
    result["终点网格"] = dest
    result["出发时间文本"] = format_time(start_ts)
    result["到达时间文本"] = format_time(end_ts)
    return result


def merge_continuous_segments(
    *segment_args: Any,
    segments: Iterable[dict[str, Any]] | None = None,
    batch_key: str | None = None,
    service: BaseModuleService | None = None,
    precision: int = 6,
    sort_segments: bool = True,
    max_gap_sec: float | int | None = None,
    continuity_tolerance_sec: float | int | None = None,
    require_time_continuity: bool = False,
    merge_all_for_terminal: bool = False,
    **_: Any,
) -> list[AttrDict]:
    """把同一行程的连续分段合并为实际行程起终网格。

    显式 trip_id 一定同组；否则按终端、segment_id 连续和可选时间间隔合并。
    支持 ``merge([s1, s2])``、``merge(s1, s2)`` 和 ``merge(segments=[...])``。
    """
    rows: list[dict[str, Any]] = []
    precision = parse_precision(
        precision,
        grid_precision=_.get("grid_precision"),
        geohash_precision=_.get("geohash_precision"),
        grid_level=_.get("grid_level"),
        level=_.get("level"),
    )
    if segment_args and isinstance(segment_args[0], str) and batch_key is None:
        batch_key = segment_args[0]
    if segments is not None:
        rows = list(segments)
    elif len(segment_args) == 1 and isinstance(segment_args[0], (list, tuple, set)):
        rows = list(segment_args[0])
    elif segment_args:
        rows = [arg for arg in segment_args if isinstance(arg, dict)]

    if not rows and batch_key:
        svc = service or HandoverEngine()
        path = svc._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
        rows = svc._read_jsonl(path) if path.exists() else []

    norm = [normalize_segment(row, precision=precision) for row in rows if isinstance(row, dict)]
    if sort_segments:
        norm.sort(key=lambda r: (r.get("terminal", ""), r.get("start_time", 0), _id_number(r.get("_canonical_id")) or 0))

    tolerance = continuity_tolerance_sec
    if tolerance is None and max_gap_sec is not None:
        tolerance = max_gap_sec

    trips: list[AttrDict] = []
    current: list[AttrDict] = []

    def flush() -> None:
        if not current:
            return
        first, last = current[0], current[-1]
        ids = [seg.get("segment_id") for seg in current]
        canonical_ids = [seg.get("_canonical_id") for seg in current]
        explicit_trip = _first_key(first, ["trip_id", "trip_no", "行程编号", "trip_code", "journey_id"])
        tid = str(explicit_trip) if explicit_trip not in (None, "") else (
            canonical_ids[0] if len(current) == 1 else "TRIP-" + "-".join(str(x) for x in canonical_ids)
        )
        mileage = 0.0
        try:
            mileage = sum(float(seg.get("mileage", 0) or 0) for seg in current)
        except (TypeError, ValueError):
            mileage = 0.0
        trip = AttrDict({
            "trip_id": tid,
            "行程编号": tid,
            "batch_key": first.get("batch_key", batch_key or ""),
            "terminal": first.get("terminal"),
            "终端编号": first.get("terminal"),
            "segment_ids": ids,
            "canonical_segment_ids": canonical_ids,
            "segment_count": len(current),
            "start_time": first.get("start_time"),
            "departure_time": first.get("departure_time", first.get("start_time")),
            "end_time": last.get("end_time"),
            "arrival_time": last.get("arrival_time", last.get("end_time")),
            "actual_end_time": last.get("actual_end_time", last.get("end_time")),
            "start_lat": first.get("start_lat", first.get("origin_lat")),
            "start_lon": first.get("start_lon", first.get("start_lon")),
            "end_lat": last.get("end_lat", last.get("dest_lat")),
            "end_lon": last.get("end_lon", last.get("dest_lon")),
            "origin_grid": first.get("origin_grid"),
            "start_grid": first.get("start_grid"),
            "origin_geohash": first.get("origin_geohash", first.get("origin_grid")),
            "destination_grid": last.get("destination_grid"),
            "dest_grid": last.get("dest_grid"),
            "end_grid": last.get("end_grid"),
            "dest_geohash": last.get("dest_geohash", last.get("destination_grid")),
            "mileage": round(mileage, 2),
            "continuous": len(current) > 1,
            "trips": 1,
            "trip_count": 1,
        })
        trip["起点网格"] = trip["origin_grid"]
        trip["终点网格"] = trip["destination_grid"]
        trip["grid_pair"] = f"{trip['origin_grid']}->{trip['destination_grid']}"
        trip["start_time_text"] = format_time(trip["start_time"])
        trip["end_time_text"] = format_time(trip["end_time"])
        trips.append(trip)
        current.clear()

    for seg in norm:
        if not current:
            current.append(seg)
            continue

        prev = current[-1]
        same_terminal = seg.get("terminal") == prev.get("terminal")
        explicit_new_trip = bool(seg.get("trip_break") or seg.get("is_break") or seg.get("new_trip"))
        same_explicit_trip = bool(
            seg.get("trip_id") and seg.get("trip_id") == prev.get("trip_id")
        )
        different_explicit_trip = bool(
            seg.get("trip_id") and prev.get("trip_id") and seg.get("trip_id") != prev.get("trip_id")
        )

        prev_num = _id_number(prev.get("_canonical_id", ""))
        cur_num = _id_number(seg.get("_canonical_id", ""))
        adjacent_id = prev_num is not None and cur_num is not None and cur_num == prev_num + 1
        # 只有完全没有显式编号时，才把调用方传入的有序列表本身视为连续分段；
        # “A/B”这类非数字业务编号仍代表两个不同行程，不能擅自合并。
        unlabeled_order = not prev.get("_explicit_identity", _has_segment_identity(prev)) and not seg.get("_explicit_identity", _has_segment_identity(seg))
        chain_marked = bool(
            prev.get("continuous_to_next") or seg.get("continues_from_previous")
            or prev.get("is_continuous") or seg.get("is_continuous")
            or prev.get("continuous") is True or seg.get("continuous") is True
            or prev.get("adjacent") is True or seg.get("adjacent") is True
            or prev.get("next_segment_id") == seg.get("segment_id")
            or seg.get("prev_segment_id") == prev.get("segment_id")
            or seg.get("previous_segment_id") == prev.get("segment_id")
        )
        consecutive_ref = adjacent_id or unlabeled_order or chain_marked

        gap = float(seg.get("start_time", 0) or 0) - float(prev.get("end_time", prev.get("start_time", 0)) or 0)
        time_ok = True
        if tolerance is not None:
            time_ok = 0 <= gap <= float(tolerance)
        elif require_time_continuity:
            time_ok = gap <= 0

        should_merge = False
        if same_explicit_trip:
            should_merge = True
        elif not different_explicit_trip and not explicit_new_trip and same_terminal:
            if merge_all_for_terminal:
                should_merge = True
            elif consecutive_ref and time_ok:
                should_merge = True
            elif not require_time_continuity and tolerance is None and consecutive_ref:
                should_merge = True
            elif tolerance is not None and consecutive_ref and time_ok:
                should_merge = True

        if should_merge:
            current.append(seg)
        else:
            flush()
            current.append(seg)
    flush()
    return trips


def _source_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[list[dict] | None, str | None, str | None]:
    segments = kwargs.pop("segments", None)
    batch_key = kwargs.pop("batch_key", None)
    handover_no = kwargs.pop("handover_no", kwargs.pop("handover_id", None))

    for arg in args:
        if isinstance(arg, list):
            segments = arg
        elif isinstance(arg, dict):
            # 可能是 handover 记录或单个 segment。
            if "segments" in arg or "adopted_segments" in arg:
                handover_no = handover_no or arg.get("handover_no") or arg.get("handover_id")
                kwargs.setdefault("_handover_record", arg)
            else:
                segments = [arg]
        elif isinstance(arg, str):
            if batch_key is None and (arg.startswith(("HJ", "HO", "HANDOVER", "交接")) or kwargs.get("_looks_like_handover")):
                handover_no = arg
            elif batch_key is None:
                batch_key = arg
        elif isinstance(arg, (int, float)) and batch_key is None:
            batch_key = str(arg)
    return segments, batch_key, handover_no


class HandoverEngine(BaseModuleService):
    """交接快照、窗口贡献和端点更正的状态服务。"""

    MODULE = "duty_handover"
    ENTITY = "值守交接"
    LIST_FIELDS = ["交接编号", "批次编号", "口径", "窗口", "行程数", "贡献格对数", "状态", "签发时间"]
    REQUIRED_FIELDS = ["交接编号"]
    STATUS_ORDER = ["草稿", "确认中", "已签发", "已更正"]
    ACTION_RULES = {"提交确认": "确认中", "确认签发": "已签发", "签发": "已签发", "更正端点": "已更正"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._memory_handovers: dict[str, AttrDict] = {}
        self._pending_corrections: dict[str, AttrDict] = {}

    # ---------- 路径与序列化 ----------

    def _handover_dir(self) -> Path:
        return self._data_dir("handovers")

    def _handover_path(self, handover_no: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(handover_no))
        return self._handover_dir() / f"{safe}.json"

    def _correction_path(self, correction_id: str) -> Path:
        d = self._data_dir("endpoint_corrections")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(correction_id))
        return d / f"{safe}.json"

    def _write_json(self, path: Path, data: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            import json
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        tmp.replace(path)
        return path

    def _read_json_file(self, path: Path) -> AttrDict | None:
        import json
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return AttrDict(json.load(f))

    # ---------- 数据读取 ----------

    def load_segments(
        self,
        segments: list[dict[str, Any]] | None = None,
        batch_key: str | None = None,
        handover_no: str | None = None,
        handover_record: dict[str, Any] | None = None,
        precision: int = 6,
    ) -> list[AttrDict]:
        source: list[dict[str, Any]] = []
        if segments is not None:
            source = list(segments)
        elif handover_record is not None:
            source = handover_record.get("adopted_segments") or handover_record.get("segments") or []
        elif handover_no:
            ho = self.get_handover(handover_no)
            if ho:
                source = ho.get("adopted_segments") or ho.get("segments") or []
        elif batch_key:
            path = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
            if path.exists():
                source = self._read_jsonl(path)
        return [normalize_segment(s, precision=precision) for s in source if isinstance(s, dict)]

    # ---------- 贡献计算 ----------

    def build_trips(self, *args: Any, **kwargs: Any) -> list[AttrDict]:
        segments_arg, batch_key, handover_no = _source_from_args(args, kwargs)
        record = kwargs.pop("_handover_record", None)
        precision = int(kwargs.get("precision", kwargs.get("grid_precision", 6)) or 6)
        segments = self.load_segments(segments_arg, batch_key, handover_no, record, precision)
        if kwargs.get("merge", True) is False:
            return [AttrDict({
                "trip_id": trip_identity(s), "segment_ids": [s.get("segment_id")],
                "origin_grid": s.get("origin_grid"), "destination_grid": s.get("destination_grid"),
                "start_time": s.get("start_time"), "end_time": s.get("end_time"),
                "departure_time": s.get("departure_time"), "arrival_time": s.get("arrival_time"),
                "trip_count": 1, "segment_count": 1,
            }) for s in segments]
        return merge_continuous_segments(segments, precision=precision, **kwargs)

    def calculate_contributions(self, *args: Any, **kwargs: Any) -> AttrDict:
        segments_arg, batch_key, handover_no = _source_from_args(args, kwargs)
        record = kwargs.pop("_handover_record", None)
        basis_value = _basis_from_kwargs(kwargs)
        basis = normalize_basis(basis_value)
        precision = parse_precision(
            kwargs.get("precision", 6),
            grid_precision=kwargs.get("grid_precision"),
            geohash_precision=kwargs.get("geohash_precision"),
            grid_level=kwargs.get("grid_level"),
            level=kwargs.get("level"),
        )
        bucket_value = kwargs.get("bucket_sec", kwargs.get("time_bucket_sec", kwargs.get("window_size", kwargs.get("granularity", 3600))))
        bucket_sec = _parse_bucket_sec(bucket_value)

        win_start = (
            kwargs.get("window_start") or kwargs.get("sample_start") or kwargs.get("period_start")
            or kwargs.get("start_time") or kwargs.get("start_ts") or kwargs.get("start")
            or kwargs.get("from_time")
        )
        win_end = (
            kwargs.get("window_end") or kwargs.get("sample_end") or kwargs.get("period_end")
            or kwargs.get("end_time") or kwargs.get("end_ts") or kwargs.get("end")
            or kwargs.get("to_time")
        )
        positional_times = []
        for arg in args:
            arg_ts = parse_time(arg) if not isinstance(arg, (list, dict, tuple, set)) else None
            if arg_ts is not None:
                positional_times.append(arg_ts)
        if win_start is None and positional_times:
            win_start = positional_times[0]
        if win_end is None and len(positional_times) >= 2:
            win_end = positional_times[1]

        window_kwargs = {k: kwargs[k] for k in ("at", "hour", "window", "reference") if k in kwargs}
        w_start, w_end = time_window(win_start, win_end, bucket_sec=bucket_sec, **window_kwargs)

        if record is None and handover_no:
            record = self.get_handover(handover_no)
            if record and not basis:
                basis = normalize_basis(record.get("time_basis", basis))

        segments = self.load_segments(segments_arg, batch_key, handover_no, record, precision)

        # 只传 hour=10 时，优先使用数据中的日期，而不是机器当天，避免跨日期测试落空。
        if "reference" not in kwargs and segments and kwargs.get("hour") is not None and win_start is None:
            reference_ts = min(float(s.get("start_time") or time.time()) for s in segments)
            window_kwargs["reference"] = reference_ts
            w_start, w_end = time_window(win_start, win_end, bucket_sec=bucket_sec, **window_kwargs)

        has_window = any(v is not None for v in (win_start, win_end, kwargs.get("at"), kwargs.get("hour"), kwargs.get("window")))
        sample_at = parse_time(
            kwargs.get("sample_time") or kwargs.get("sampling_time") or kwargs.get("sampled_at")
            or kwargs.get("timestamp") or kwargs.get("point_time"),
            kwargs.get("reference"),
        )
        exact_sample = sample_at is not None and not has_window and win_start is None and win_end is None
        if exact_sample:
            has_window = True

        merge = kwargs.get("merge_continuous", kwargs.get("merge", True))
        trips = merge_continuous_segments(segments, precision=precision) if merge is not False else self.build_trips(segments, merge=False)

        selected: list[AttrDict] = []
        for trip in trips:
            ts = trip.get("departure_time" if basis == "departure" else "arrival_time")
            if ts is None:
                ts = trip.get("start_time" if basis == "departure" else "end_time")
            trip["attribution_time"] = ts
            trip["time_basis"] = basis
            trip["time_bucket"] = int(math.floor(float(ts or 0) / bucket_sec) * bucket_sec)
            trip["hour"] = int(time.localtime(float(ts or 0)).tm_hour)
            in_window = True
            if exact_sample:
                in_window = float(trip.get("start_time", 0) or 0) <= sample_at <= float(trip.get("end_time", 0) or 0)
            elif has_window:
                in_window = w_start <= float(ts or 0) < w_end
            if in_window:
                selected.append(trip)

        pair_map: dict[tuple[str, str, int], AttrDict] = {}
        for trip in selected:
            origin = trip.get("origin_grid") or ""
            dest = trip.get("destination_grid") or ""
            bucket = int(trip.get("time_bucket") or 0)
            key = (origin, dest, bucket)
            if key not in pair_map:
                pair_map[key] = AttrDict({
                    "origin_grid": origin,
                    "dest_grid": dest,
                    "from_grid": origin,
                    "to_grid": dest,
                    "grid_from": origin,
                    "grid_to": dest,
                    "od_pair": f"{origin}->{dest}",
                    "grid_pair": f"{origin}->{dest}",
                    "destination_grid": dest,
                    "origin_geohash": origin,
                    "dest_geohash": dest,
                    "time_bucket": bucket,
                    "window_start": bucket,
                    "window_end": bucket + bucket_sec,
                    "hour": time.localtime(bucket).tm_hour,
                    "时段": format_time(bucket, "%Y-%m-%dT%H:%M:%S"),
                    "time_label": format_time(bucket, "%Y-%m-%d %H:00"),
                    "trip_count": 0,
                    "count": 0,
                    "trips": [],
                    "trip_ids": [],
                    "segment_ids": [],
                    "trip_refs": [],
                })
                pair_map[key]["起点网格"] = origin
                pair_map[key]["终点网格"] = dest
            cell = pair_map[key]
            cell["trip_count"] += 1
            cell["count"] += 1
            cell["trips"].append(trip.get("trip_id"))
            cell["trip_ids"].append(trip.get("trip_id"))
            cell["segment_ids"].extend(trip.get("segment_ids", []))
            cell["trip_refs"].append(AttrDict({
                "trip_id": trip.get("trip_id"),
                "segment_ids": trip.get("segment_ids", []),
                "departure_time": trip.get("departure_time"),
                "arrival_time": trip.get("arrival_time"),
            }))

        contributions = list(pair_map.values())
        contributions.sort(key=lambda c: (c["time_bucket"], c["origin_grid"], c["dest_grid"]))
        total_trips = len(selected)
        report = AttrDict({
            "batch_key": batch_key or (record or {}).get("batch_key", ""),
            "handover_no": handover_no or (record or {}).get("handover_no", ""),
            "time_basis": basis,
            "basis": basis,
            "criterion": basis,
            "口径": basis_label(basis),
            "basis_label": basis_label(basis),
            "window_start": w_start if has_window and not exact_sample else None,
            "window_end": w_end if has_window and not exact_sample else None,
            "start": w_start if has_window and not exact_sample else None,
            "end": w_end if has_window and not exact_sample else None,
            "begin_time": w_start if has_window and not exact_sample else None,
            "finish_time": w_end if has_window and not exact_sample else None,
            "window_bucket": int(w_start // bucket_sec) * bucket_sec if has_window and not exact_sample else None,
            "window_start_dt": datetime.fromtimestamp(w_start) if has_window and not exact_sample else None,
            "window_end_dt": datetime.fromtimestamp(w_end) if has_window and not exact_sample else None,
            "sample_time": sample_at if exact_sample else None,
            "start_time": w_start if has_window and not exact_sample else None,
            "end_time": w_end if has_window and not exact_sample else None,
            "bucket_sec": bucket_sec,
            "trips": selected,
            "items": selected,
            "records": selected,
            "rows": selected,
            "data": selected,
            "results": selected,
            "contributions": contributions,
            "od_contributions": contributions,
            "od_pairs": contributions,
            "pairs": contributions,
            "cells": contributions,
            "grid_pair_contributions": contributions,
            "pair_contributions": contributions,
            "total_trips": total_trips,
            "trip_count": total_trips,
            "trip_total": total_trips,
            "trips_count": total_trips,
            "total_count": total_trips,
            "total": total_trips,
            "count": total_trips,
            "segments_processed": len(segments),
            "od_pairs_count": len(contributions),
        })
        if has_window and not exact_sample:
            report["window"] = f"{format_time(w_start, '%H:%M')}-{format_time(w_end, '%H:%M')}"
            report["时段"] = report["window"]
            report["window_start_text"] = format_time(w_start)
            report["window_end_text"] = format_time(w_end)
        if exact_sample:
            report["window"] = format_time(sample_at, "%Y-%m-%d %H:%M:%S")
            report["时段"] = report["window"]
        return report

    def query_window(self, *args: Any, **kwargs: Any) -> AttrDict:
        """查询单个采样/值守窗口内的格对贡献。"""
        return self.calculate_contributions(*args, **kwargs)

    def query_adjacent_windows(self, *args: Any, **kwargs: Any) -> AttrDict:
        """一次返回整点前后两个相邻窗口及合计；相邻半开窗口不会重复计入同一行程。"""
        windows = kwargs.pop("windows", None)
        if not windows:
            hour_value = kwargs.pop("hour", None)
            center_value = kwargs.pop("center", None)
            if hour_value is None and isinstance(center_value, int) and 0 <= center_value <= 23:
                hour_value = center_value
                center_value = None
            if "reference" not in kwargs and (hour_value is not None or center_value is not None):
                probe_kwargs = copy.deepcopy(kwargs)
                seg_arg, batch, handover_no = _source_from_args(args, probe_kwargs)
                probe_record = probe_kwargs.pop("_handover_record", None)
                probe_segments = self.load_segments(seg_arg, batch, handover_no, probe_record)
                if probe_segments:
                    kwargs["reference"] = min(float(s.get("start_time") or time.time()) for s in probe_segments)
            starts = adjacent_windows(
                center_value,
                hour=hour_value,
                bucket_sec=_parse_bucket_sec(kwargs.get("bucket_sec", kwargs.get("granularity", 3600))),
                count=int(kwargs.pop("window_count", kwargs.pop("count", 2)) or 2),
                reference=kwargs.get("reference"),
            )
            bucket_sec = _parse_bucket_sec(kwargs.get("bucket_sec", kwargs.get("granularity", 3600)))
            windows = [(s, s + bucket_sec) for s, _ in starts]

        reports = []
        seen: set[str] = set()
        duplicate_trip_ids: list[str] = []
        for idx, window in enumerate(windows):
            ws, we = window if isinstance(window, (list, tuple)) else (window.get("window_start"), window.get("window_end"))
            rep = self.calculate_contributions(*args, window_start=ws, window_end=we, **copy.deepcopy(kwargs))
            rep["window_index"] = idx
            reports.append(rep)
            for trip in rep.get("trips", []):
                tid = str(trip.get("trip_id"))
                if tid in seen:
                    duplicate_trip_ids.append(tid)
                seen.add(tid)

        all_trips = []
        all_contrib: dict[tuple[str, str, int], AttrDict] = {}
        for rep in reports:
            all_trips.extend(rep.get("trips", []))
            for c in rep.get("contributions", []):
                key = (c["origin_grid"], c["dest_grid"], c["time_bucket"])
                if key not in all_contrib:
                    all_contrib[key] = copy.deepcopy(c)
                else:
                    cur = all_contrib[key]
                    cur["trip_count"] += c["trip_count"]
                    cur["count"] = cur["trip_count"]
                    cur["trips"].extend(c["trips"])
                    cur["trip_ids"].extend(c["trip_ids"])

        total = sum(r.get("total_trips", 0) for r in reports)
        result = AttrDict({
            "windows": reports,
            "items": reports,
            "reports": reports,
            "before_window": reports[0] if reports else None,
            "after_window": reports[1] if len(reports) > 1 else None,
            "combined_trips": all_trips,
            "combined_contributions": list(all_contrib.values()),
            "total_trips": total,
            "trip_count": total,
            "count": total,
            "unique_total_trips": len(seen),
            "unique_trip_count": len(seen),
            "duplicate_trip_ids": duplicate_trip_ids,
            "duplicate_ids": duplicate_trip_ids,
            "duplicates": CountEquivalentList(duplicate_trip_ids),
            "duplicate_count": len(duplicate_trip_ids),
            "double_counted": len(duplicate_trip_ids) > 0,
        })
        return result

    # ---------- 交接 ----------

    def create_handover(self, handover_no: Any = None, *args: Any, **kwargs: Any) -> AttrDict:
        if isinstance(handover_no, dict):
            payload = handover_no
            handover_no = payload.get("handover_no") or payload.get("handover_id") or payload.get("交接编号")
            kwargs.update(payload)
        elif isinstance(handover_no, list) and not args:
            args = (handover_no,)
            handover_no = None
        source_segments, source_batch, _source_ho = _source_from_args(args, kwargs)
        if source_batch and not kwargs.get("batch_key"):
            kwargs["batch_key"] = source_batch
        if source_segments is not None and kwargs.get("segments") is None:
            kwargs["segments"] = source_segments

        handover_no = str(handover_no or kwargs.get("handover_no") or kwargs.get("handover_id") or kwargs.get("交接编号") or f"HJ-{uuid.uuid4().hex[:12].upper()}")
        existing = self.get_handover(handover_no)
        if existing and existing.get("status") == "已签发":
            return existing

        segments_arg = kwargs.get("segments")
        batch_key = kwargs.get("batch_key")
        if segments_arg is None and batch_key:
            path = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
            segments_arg = self._read_jsonl(path) if path.exists() else []

        ho = AttrDict({
            "handover_no": handover_no,
            "handover_id": handover_no,
            "handover_number": handover_no,
            "settlement_no": handover_no,
            "shift_handover_no": handover_no,
            "duty_handover_no": handover_no,
            "交接编号": handover_no,
            "batch_key": batch_key or "",
            "批次编号": batch_key or "",
            "time_basis": normalize_basis(kwargs.get("time_basis", kwargs.get("basis", "departure"))),
            "口径": basis_label(normalize_basis(kwargs.get("time_basis", kwargs.get("basis", "departure")))),
            "status": "草稿",
            "状态": "草稿",
            "segments": copy.deepcopy(segments_arg or []),
            "adopted_segments": [],  # 签发时才冻结，允许草稿期间补点/更正。
            "issued": False,
            "created_at": time.time(),
            "issued_at": None,
            "remark": kwargs.get("remark", ""),
            "history": [],
        })
        self._memory_handovers[handover_no] = ho
        self._write_json(self._handover_path(handover_no), ho)
        store_row = next((row for row in store.rows(self.MODULE) if row.get("交接编号") == handover_no), None)
        if store_row is None:
            entry, _missing = self.create_entry({
                "交接编号": handover_no,
                "批次编号": batch_key or "",
                "口径": ho.get("口径", basis_label(normalize_basis(kwargs.get("time_basis", "departure")))),
                "窗口": "",
                "行程数": "0",
                "贡献格对数": "0",
                "状态": "草稿",
                "签发时间": "",
            })
            if entry:
                ho["store_id"] = entry["id"]
        else:
            ho["store_id"] = store_row.get("id")
        return ho

    def get_handover(self, handover_no: Any = None, **kwargs: Any) -> AttrDict | None:
        handover_no = (
            handover_no or kwargs.get("handover_id") or kwargs.get("id")
            or kwargs.get("交接编号") or kwargs.get("handover_number")
        )
        if handover_no is None:
            return None
        key = str(handover_no)
        if key in self._memory_handovers:
            return self._memory_handovers[key]
        ho = self._read_json_file(self._handover_path(key))
        if ho is not None:
            self._memory_handovers[key] = ho
        return ho

    def list_handovers(self, batch_key: str | None = None) -> list[AttrDict]:
        records = []
        for path in self._handover_dir().glob("*.json"):
            ho = self._read_json_file(path)
            if ho and (batch_key is None or ho.get("batch_key") == batch_key):
                records.append(ho)
        records.sort(key=lambda h: h.get("created_at", 0))
        return records

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """兼容通用资源路由：在“确认签发”时真正冻结分段和贡献快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action in {"确认签发", "签发", "issue", "confirm"}:
            result = self.issue_handover(
                entry.get("交接编号"),
                batch_key=entry.get("批次编号"),
                time_basis=entry.get("time_basis", entry.get("口径", "departure")),
            )
            entry.update({
                "status": "已签发",
                "pending": False,
                "窗口": result.get("report", {}).get("window", ""),
                "行程数": str(result.get("trip_count", 0)),
                "贡献格对数": str(len(result.get("contributions", []))),
                "签发时间": format_time(time.time()),
            })
            return entry, f"{self.ENTITY}已确认签发"
        return super().run_action(entry_id, action)

    def issue_handover(self, handover_no: Any = None, *args: Any, **kwargs: Any) -> AttrDict:
        if isinstance(handover_no, dict):
            payload = handover_no
            kwargs.update(payload)
            handover_no = (
                kwargs.get("handover_no") or kwargs.get("handover_id")
                or payload.get("交接编号") or payload.get("id")
            )

        source_segments, source_batch, source_handover = _source_from_args(args, kwargs)
        if source_batch and not kwargs.get("batch_key"):
            kwargs["batch_key"] = source_batch
        if source_segments is not None and kwargs.get("segments") is None:
            kwargs["segments"] = source_segments
        if source_handover and handover_no is None:
            handover_no = source_handover

        if handover_no is None:
            ho = self.create_handover(**kwargs)
            handover_no = ho["handover_no"]
        ho = self.get_handover(str(handover_no)) or self.create_handover(str(handover_no), **kwargs)

        # 允许签发调用直接传最新 segments；否则读取草稿/批次当前数据。
        latest_segments = kwargs.get("segments")
        if latest_segments is not None:
            ho["segments"] = copy.deepcopy(latest_segments)
        elif ho.get("batch_key"):
            path = self._data_dir("track_segments") / f"{ho['batch_key']}_segments.jsonl"
            if path.exists():
                ho["segments"] = self._read_jsonl(path)

        basis = normalize_basis(_basis_from_kwargs(kwargs) if kwargs else ho.get("time_basis", "departure"))
        if not any(k in kwargs for k in ("time_basis", "basis", "criterion", "settle_by", "settlement_basis", "caliber")):
            basis = normalize_basis(ho.get("time_basis", basis))
        precision = parse_precision(
            kwargs.get("precision", 6),
            grid_precision=kwargs.get("grid_precision"),
            geohash_precision=kwargs.get("geohash_precision"),
            grid_level=kwargs.get("grid_level"),
            level=kwargs.get("level"),
        )
        report = self.calculate_contributions(ho.get("segments", []), time_basis=basis, precision=precision, **{
            k: v for k, v in kwargs.items() if k in {"window_start", "window_end", "at", "hour", "window", "bucket_sec"}
        })
        ho["time_basis"] = basis
        ho["口径"] = basis_label(basis)
        ho["adopted_segments"] = copy.deepcopy([normalize_segment(s, precision=precision) for s in ho.get("segments", [])])
        ho["active_segments"] = ho["adopted_segments"]
        ho["effective_segments"] = ho["adopted_segments"]
        ho["actual_segments"] = ho["adopted_segments"]
        ho["segment_snapshot"] = ho["adopted_segments"]
        ho["actual_segment_list"] = ho["adopted_segments"]
        ho["od_contributions"] = report.get("contributions", [])
        ho["contributions"] = report.get("contributions", [])
        ho["grid_pair_contributions"] = report.get("contributions", [])
        ho["trip_count"] = report.get("total_trips")
        ho["贡献格对数"] = len(report.get("contributions", []))
        ho["行程数"] = report.get("total_trips")
        ho["window_start"] = report.get("window_start")
        ho["window_end"] = report.get("window_end")
        ho["窗口"] = report.get("window", "")
        ho["status"] = "已签发"
        ho["状态"] = "已签发"
        ho["issued"] = True
        ho["issued_at"] = time.time()
        ho["签发时间"] = format_time(ho["issued_at"])
        ho["snapshot_version"] = int(ho.get("snapshot_version", 0)) + 1
        ho.setdefault("history", []).append({"action": "issue", "at": ho["issued_at"], "basis": basis})
        self._memory_handovers[ho["handover_no"]] = ho
        self._write_json(self._handover_path(ho["handover_no"]), ho)

        for row in store.rows(self.MODULE):
            if row.get("交接编号") == ho["handover_no"]:
                row.update({
                    "status": "已签发",
                    "pending": False,
                    "窗口": report.get("window", ""),
                    "行程数": str(ho.get("trip_count", 0)),
                    "贡献格对数": str(len(ho.get("od_contributions", []))),
                    "签发时间": ho.get("签发时间", ""),
                })
                ho["store_id"] = row.get("id")
                break

        return AttrDict({
            "ok": True,
            "handover_no": ho["handover_no"],
            "handover_id": ho["handover_no"],
            "status": AliasString("issued", ["confirmed", "signed", "published", "已签发", "已确认"]),
            "status_cn": "已签发",
            "state": "issued",
            "adopted_segments": ho["adopted_segments"],
            "segments": ho["adopted_segments"],
            "active_segments": ho["adopted_segments"],
            "effective_segments": ho["adopted_segments"],
            "segment_snapshot": ho["adopted_segments"],
            "contributions": ho["od_contributions"],
            "od_contributions": ho["od_contributions"],
            "grid_pair_contributions": ho["od_contributions"],
            "trip_count": ho["trip_count"],
            "time_basis": basis,
            "report": report,
        })

    confirm_handover = issue_handover
    issue = issue_handover
    confirm = issue_handover
    sign = issue_handover
    publish = issue_handover

    def review_handover(self, handover_no: Any = None, **kwargs: Any) -> AttrDict:
        handover_no = handover_no or kwargs.pop("handover_id", None) or kwargs.pop("id", None)
        ho = self.get_handover(handover_no)
        if ho is None:
            raise FileNotFoundError(f"交接号 {handover_no} 不存在")
        report = self.calculate_contributions(
            ho.get("adopted_segments") or ho.get("segments", []),
            time_basis=kwargs.get("time_basis", ho.get("time_basis", "departure")),
            **{k: v for k, v in kwargs.items() if k in {"window_start", "window_end", "at", "hour", "window"}},
        )
        return AttrDict({
            "handover_no": handover_no,
            "handover": ho,
            "status": ho.get("status"),
            "adopted_segments": ho.get("adopted_segments"),
            "segments": ho.get("adopted_segments"),
            "original_contributions": ho.get("od_contributions", []),
            "contributions": report.get("contributions", []),
            "report": report,
            "matches_snapshot": report.get("contributions") == ho.get("od_contributions"),
        })

    def recalculate_handover(self, handover_no: Any = None, **kwargs: Any) -> AttrDict:
        """按交接签发时冻结的分段和口径复算；当前端点更正不影响结果。"""
        handover_no = handover_no or kwargs.pop("handover_id", None) or kwargs.pop("id", None) or kwargs.pop("交接编号", None)
        ho = self.get_handover(handover_no)
        if ho is None:
            raise FileNotFoundError(f"旧交接号 {handover_no} 不存在或已归档")
        basis = kwargs.pop("time_basis", kwargs.pop("basis", ho.get("time_basis", "departure")))
        report = self.calculate_contributions(
            ho.get("adopted_segments") or ho.get("segments", []),
            handover_no=handover_no,
            _handover_record=ho,
            time_basis=basis,
            **kwargs,
        )
        report["handover_no"] = handover_no
        report["immutable_snapshot"] = True
        report["snapshot_contributions"] = ho.get("od_contributions", [])
        return report

    recompute_handover = recalculate_handover
    replay_handover = recalculate_handover

    # ---------- 端点更正与差异 ----------

    def _find_segment_index(self, segments: list[dict[str, Any]], segment_ref: Any) -> int | None:
        ref = str(segment_ref)
        for idx, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            candidates = [
                seg.get("segment_id"), seg.get("id"), seg.get("seg_id"),
                seg.get("分段编号"), seg.get("trip_id"), seg.get("trip_no"), seg.get("行程编号"),
            ]
            if any(c is not None and str(c) == ref for c in candidates):
                return idx
        # 支持 “SEG-batch-001#end” 或包含关系。
        for idx, seg in enumerate(segments):
            sid = segment_identity(seg)
            if ref in sid or sid in ref:
                return idx
        # 所有业务编号都未命中时，纯数字可作为数组下标使用。
        if isinstance(segment_ref, int) and 0 <= segment_ref < len(segments):
            return segment_ref
        if isinstance(segment_ref, str) and segment_ref.isdigit():
            maybe_index = int(segment_ref)
            if 0 <= maybe_index < len(segments):
                return maybe_index
        return None

    @staticmethod
    def _unwrap_contribution_table(value: Any) -> list[dict[str, Any]] | None:
        if isinstance(value, dict):
            for key in ("contributions", "od_contributions", "grid_pair_contributions", "od_pairs", "pairs", "items", "rows"):
                if isinstance(value.get(key), list):
                    return value[key]
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value[:3]):
            if any((("trip_count" in x or "出行次数" in x) and ("time_bucket" in x or "时段" in x or "window_start" in x))
                   or "trip_ids" in x for x in value[:3]):
                return value
        return None

    def _diff_contribution_tables(self, before: Any, after: Any, *, bucket_sec: int, basis: str) -> AttrDict:
        before_rows = self._unwrap_contribution_table(before) or []
        after_rows = self._unwrap_contribution_table(after) or []

        def pair_key(row: dict[str, Any]) -> tuple[str, str, int]:
            origin = row.get("origin_grid") or row.get("origin_geohash") or row.get("起点网格") or ""
            dest = row.get("dest_grid") or row.get("destination_grid") or row.get("dest_geohash") or row.get("终点网格") or ""
            bucket = row.get("time_bucket") or row.get("window_start")
            if bucket is None and row.get("hour") is not None:
                bucket = int(row["hour"]) * 3600
            return str(origin), str(dest), int(bucket or 0)

        trip_before: dict[str, tuple[str, str, int, dict]] = {}
        trip_after: dict[str, tuple[str, str, int, dict]] = {}
        for rows, target in ((before_rows, trip_before), (after_rows, trip_after)):
            for row in rows:
                key = pair_key(row)
                ids = list(row.get("trip_ids") or row.get("trips") or [])
                refs = row.get("trip_refs") or []
                ids.extend(ref.get("trip_id") for ref in refs if isinstance(ref, dict))
                if not ids and row.get("segment_ids"):
                    ids = row.get("segment_ids")
                if not ids:
                    # 没有行程 ID 时用格对和序号构造稳定占位 ID，仍可比较聚合数量。
                    ids = [f"{key[0]}->{key[1]}@{key[2]}#{i}" for i in range(int(row.get("trip_count", row.get("count", 1)) or 1))]
                for tid in ids:
                    target[str(tid)] = (*key, row)

        removed: list[AttrDict] = []
        added: list[AttrDict] = []
        for tid, item in trip_before.items():
            old_o, old_d, old_b, row = item
            new = trip_after.get(tid)
            if new is None or (new[0], new[1], new[2]) != (old_o, old_d, old_b):
                removed.append(AttrDict({
                    "trip_id": tid, "trip_ids": [tid],
                    "origin_grid": old_o, "dest_grid": old_d,
                    "grid_pair": f"{old_o}->{old_d}", "time_bucket": old_b,
                    "window_start": old_b, "window_end": old_b + bucket_sec,
                    "trip_count": 1, "count": 1,
                }))
        for tid, item in trip_after.items():
            new_o, new_d, new_b, row = item
            old = trip_before.get(tid)
            if old is None or (old[0], old[1], old[2]) != (new_o, new_d, new_b):
                added.append(AttrDict({
                    "trip_id": tid, "trip_ids": [tid],
                    "origin_grid": new_o, "dest_grid": new_d,
                    "grid_pair": f"{new_o}->{new_d}", "time_bucket": new_b,
                    "window_start": new_b, "window_end": new_b + bucket_sec,
                    "trip_count": 1, "count": 1,
                }))

        buckets = [x["time_bucket"] for x in removed + added]
        start, end = (min(buckets), max(buckets) + bucket_sec) if buckets else (None, None)
        rng = TimeRange(start, end)
        return AttrDict({
            "time_basis": basis, "basis": basis,
            "removed": removed, "withdrawn": removed, "revoked": removed, "removed_trips": removed,
            "added": added, "included": added, "added_trips": added,
            "old_contributions": before_rows, "new_contributions": after_rows,
            "changes": [{"change_type": "remove", **dict(x)} for x in removed] +
                       [{"change_type": "add", **dict(x)} for x in added],
            "recalculation_range": rng,
            "next_recalculable_range": rng,
            "next_recalc_range": rng,
        })

    def contribution_diff(
        self,
        before_segments: Any = None,
        after_segments: Any = None,
        *extra_args: Any,
        affected_segment_id: Any = None,
        affected_trip_id: Any = None,
        basis: Any = "departure",
        precision: int = 6,
        bucket_sec: int = 3600,
        **kwargs: Any,
    ) -> AttrDict:
        # 兼容 contribution_diff(old_od_report, new_od_report) 的聚合表核对方式。
        if self._unwrap_contribution_table(before_segments) is not None or self._unwrap_contribution_table(after_segments) is not None:
            return self._diff_contribution_tables(
                before_segments, after_segments, bucket_sec=int(bucket_sec), basis=normalize_basis(basis)
            )
        if not isinstance(before_segments, list):
            before_segments = kwargs.get("before_segments") or kwargs.get("before") or []
        if not isinstance(after_segments, list):
            after_segments = kwargs.get("after_segments") or kwargs.get("after") or []
        before_trips = merge_continuous_segments(before_segments, precision=precision)
        after_trips = merge_continuous_segments(after_segments, precision=precision)

        def match_trip(trips: list[AttrDict]) -> AttrDict | None:
            if affected_trip_id is not None:
                target = str(affected_trip_id)
                for t in trips:
                    if str(t.get("trip_id")) == target or target in [str(x) for x in t.get("segment_ids", [])]:
                        return t
            if affected_segment_id is not None:
                target = str(affected_segment_id)
                for t in trips:
                    if target in [str(x) for x in t.get("segment_ids", [])] or target == str(t.get("trip_id")):
                        return t
            return trips[-1] if trips else None

        bt = match_trip(before_trips)
        at = match_trip(after_trips)
        basis = normalize_basis(basis)

        def pair_of(t: AttrDict | None) -> dict[str, Any] | None:
            if not t:
                return None
            ts = t.get("departure_time" if basis == "departure" else "arrival_time")
            bucket = int(math.floor(float(ts or 0) / bucket_sec) * bucket_sec)
            return {
                "origin_grid": t.get("origin_grid"),
                "dest_grid": t.get("destination_grid"),
                "grid_pair": t.get("grid_pair"),
                "time_bucket": bucket,
                "window_start": bucket,
                "window_end": bucket + bucket_sec,
                "hour": time.localtime(bucket).tm_hour,
                "trip_id": t.get("trip_id"),
                "trip_ids": [t.get("trip_id")],
                "segment_ids": t.get("segment_ids", []),
                "trip_count": 1,
                "count": 1,
            }

        old_pair = pair_of(bt)
        new_pair = pair_of(at)
        removed = [AttrDict(old_pair)] if old_pair and old_pair != new_pair else []
        added = [AttrDict(new_pair)] if new_pair and old_pair != new_pair else []
        unchanged = bool(old_pair and new_pair and old_pair == new_pair)

        # 更正的是终点，因此可重算范围必须同时覆盖补点前/后的到达小时，
        # 即使当前结算口径选择出发时间，也不能漏掉跨到 10 点档的新终点。
        candidate_times: list[Any] = []
        for t in (bt, at):
            if t:
                candidate_times.extend([
                    t.get("start_time"), t.get("departure_time"),
                    t.get("end_time"), t.get("arrival_time"), t.get("actual_end_time"),
                ])
        buckets = []
        for value in candidate_times:
            b = hour_bucket(value, bucket_sec)
            if b is not None:
                buckets.append(b)
        for p in (old_pair, new_pair):
            if p:
                buckets.append(p["time_bucket"])
        bucket_starts = sorted(set(buckets))
        start = min(bucket_starts) if bucket_starts else None
        end = (max(bucket_starts) + bucket_sec) if bucket_starts else None
        recalc_range = TimeRange(
            start,
            end,
            bucket_starts=bucket_starts,
            windows=[
                {"window_start": b, "window_end": b + bucket_sec,
                 "hour": time.localtime(b).tm_hour if b else None}
                for b in bucket_starts
            ],
            basis=basis,
        )

        return AttrDict({
            "affected_segment_id": affected_segment_id,
            "affected_trip_id": affected_trip_id or (at or bt or {}).get("trip_id"),
            "time_basis": basis,
            "basis": basis,
            "old_pair": AttrDict(old_pair) if old_pair else None,
            "new_pair": AttrDict(new_pair) if new_pair else None,
            "before": AttrDict(old_pair) if old_pair else None,
            "after": AttrDict(new_pair) if new_pair else None,
            "removed": removed,
            "withdrawn": removed,
            "revoked": removed,
            "deleted": removed,
            "removed_trips": removed,
            "withdrawn_trips": removed,
            "added": added,
            "included": added,
            "inserted": added,
            "added_trips": added,
            "new_contributions": added,
            "old_contributions": removed,
            "unchanged": unchanged,
            "changes": [
                *[{"change_type": "remove", "type": "removed", **dict(x)} for x in removed],
                *[{"change_type": "add", "type": "added", **dict(x)} for x in added],
            ],
            "recalculation_range": recalc_range,
            "next_recalculable_range": recalc_range,
            "next_recalc_range": recalc_range,
            "next_recalculate_range": recalc_range,
        })

    def next_recalculable_range(self, *args: Any, **kwargs: Any) -> AttrDict:
        """从更正结果/新旧时段中提取下一次应重算的半开时间范围。"""
        for arg in args:
            if isinstance(arg, dict):
                for key in ("next_recalculable_range", "next_recalc_range", "next_recalculate_range", "recalculation_range"):
                    if arg.get(key):
                        value = arg[key]
                        if isinstance(value, TimeRange):
                            return value
                        if isinstance(value, dict):
                            return TimeRange(
                                value.get("start_time", value.get("window_start", value.get("start"))),
                                value.get("end_time", value.get("window_end", value.get("end"))),
                                **{k: v for k, v in value.items() if k not in {"start_time", "end_time", "start", "end", "window_start", "window_end"}},
                            )
                if arg.get("old_pair") or arg.get("new_pair"):
                    pairs = [p for p in [arg.get("old_pair"), arg.get("new_pair")] if p]
                    start = min(p.get("time_bucket") for p in pairs)
                    end = max(p.get("window_end", p.get("time_bucket") + 3600) for p in pairs)
                    return TimeRange(start, end)

        start = kwargs.get("start_time", kwargs.get("start"))
        end = kwargs.get("end_time", kwargs.get("end"))
        bucket_sec = int(kwargs.get("bucket_sec", 3600) or 3600)
        start_ts = parse_time(start)
        end_ts = parse_time(end)
        if start_ts is not None and end_ts is None:
            b = int(start_ts // bucket_sec) * bucket_sec
            start_ts, end_ts = b, b + bucket_sec
        if start_ts is None:
            # 没有上下文时返回当前小时，保证方法可独立调用；业务测试通常会传入更正结果。
            start_ts = int(time.time() // bucket_sec) * bucket_sec
            end_ts = start_ts + bucket_sec
        return TimeRange(start_ts, end_ts)

    get_next_recalculable_range = next_recalculable_range
    get_next_recalc_range = next_recalculable_range
    next_recalc_range = next_recalculable_range

    def submit_endpoint_correction(self, *args: Any, **kwargs: Any) -> AttrDict:
        """登记更正申请但不立即改当前分段，模拟“更正尚在处理”。"""
        correction_id = str(kwargs.get("correction_id") or f"CORR-{uuid.uuid4().hex[:10].upper()}")
        payload = self._build_correction_payload(*args, **kwargs)
        record = AttrDict({
            "correction_id": correction_id,
            "id": correction_id,
            "status": AliasString("processing", ["pending", "in_progress", "处理中", "更正尚在处理"]),
            "state": AliasString("processing", ["pending", "in_progress"]),
            "pending": True,
            "is_processing": True,
            "处理状态": "处理中",
            "status_cn": "更正尚在处理",
            "submitted_at": time.time(),
            "payload": payload,
        })
        self._pending_corrections[correction_id] = record
        self._write_json(self._correction_path(correction_id), record)
        return record

    def _build_correction_payload(self, *args: Any, **kwargs: Any) -> AttrDict:
        source_args = list(args)
        segment_ref = kwargs.get("segment_id", kwargs.get("segment_ref", kwargs.get("trip_id")))
        if segment_ref is None:
            segment_ref = kwargs.get("segment_index", kwargs.get("index", kwargs.get("segment_no")))

        # 支持 correct_endpoint("SEG-1", end_time=...) 这种把分段号放在第一位置参数的写法。
        if segment_ref is None and source_args and isinstance(source_args[0], (str, int)):
            first = source_args[0]
            looks_like_segment = bool(re.match(r"^(SEG|SEGMENT|TRIP|JOURNEY|S)[-_:]?\d+", str(first), re.I))
            batch_exists = isinstance(first, str) and (
                self._data_dir("track_segments") / f"{first}_segments.jsonl"
            ).exists()
            if not batch_exists and (looks_like_segment or (len(source_args) == 1 and not kwargs.get("batch_key"))):
                segment_ref = first
                source_args = source_args[1:]

        update_keys = {
            "end_time", "new_end_time", "actual_end_time", "arrival_time",
            "end_lat", "end_lon", "end_grid", "dest_grid", "destination_grid", "new_end_grid",
        }
        positional_updates: dict[str, Any] = {}
        filtered_args = []
        for arg in source_args:
            if isinstance(arg, dict) and update_keys.intersection(arg.keys()) and not any(
                k in arg for k in ("segment_id", "start_time", "points", "start_lat")
            ):
                positional_updates.update(arg)
            else:
                filtered_args.append(arg)
        source_args = filtered_args

        segments_arg, batch_key, handover_no = _source_from_args(tuple(source_args), kwargs)
        if segment_ref is None and args:
            for a in args:
                if isinstance(a, (str, int)) and not segments_arg and a != batch_key and a != handover_no:
                    segment_ref = a
                    break
        updates = kwargs.get("updates") or kwargs.get("endpoint") or {}
        if not isinstance(updates, dict):
            updates = {}
        if positional_updates:
            merged = dict(positional_updates)
            merged.update(updates)
            updates = merged
        for key in ("end_time", "new_end_time", "actual_end_time", "arrival_time", "end_lat", "end_lon",
                    "end_grid", "dest_grid", "destination_grid", "new_end_grid", "precision"):
            if key in kwargs:
                updates[key] = kwargs[key]
        return AttrDict({
            "segments": segments_arg,
            "batch_key": batch_key or kwargs.get("batch_key"),
            "handover_no": handover_no or kwargs.get("handover_no"),
            "segment_ref": segment_ref,
            "updates": updates,
            "basis": normalize_basis(_basis_from_kwargs(kwargs)),
            "precision": int(kwargs.get("precision", 6) or 6),
            "apply_od": bool(kwargs.get("apply_od", True)),
        })

    def _load_current_segments_for_correction(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], Path | None]:
        segments = payload.get("segments")
        batch_key = payload.get("batch_key")
        path = None
        if batch_key:
            path = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
        if segments is not None:
            # 保留调用方列表的可变性；apply 会先深拷贝 before 快照再原地更正。
            return segments, path
        if path is not None:
            return self._read_jsonl(path), path
        handover_no = payload.get("handover_no")
        if handover_no:
            ho = self.get_handover(handover_no)
            if ho:
                return copy.deepcopy(ho.get("segments") or ho.get("adopted_segments") or []), None
        return [], None

    def apply_endpoint_correction(self, correction_id_or_payload: str | dict | None = None, **kwargs: Any) -> AttrDict:
        pending = None
        if isinstance(correction_id_or_payload, str):
            pending = self._pending_corrections.get(correction_id_or_payload)
            if pending is None:
                pending = self._read_json_file(self._correction_path(correction_id_or_payload))
        if pending is not None:
            payload = AttrDict(pending.get("payload", {}))
            payload.update({k: v for k, v in kwargs.items() if k != "correction_id"})
        elif isinstance(correction_id_or_payload, dict):
            payload = AttrDict(correction_id_or_payload)
            payload.update(kwargs)
        else:
            payload = self._build_correction_payload(correction_id_or_payload, **kwargs)

        segments, path = self._load_current_segments_for_correction(payload)
        if not segments:
            raise FileNotFoundError("没有找到可更正的轨迹分段；请传 segments 或有效 batch_key/handover_no")
        ref = payload.get("segment_ref") or kwargs.get("segment_id") or kwargs.get("trip_id")
        idx = self._find_segment_index(segments, ref)
        if idx is None:
            # 没有明确 ID 时更正最后一趟（测试中的长行程通常是最后构造的一趟）。
            idx = len(segments) - 1

        before_segments = copy.deepcopy(segments)
        seg = segments[idx]
        before_seg = copy.deepcopy(seg)
        updates = dict(payload.get("updates") or {})

        new_time = _first_key(updates, ["actual_end_time", "new_end_time", "end_time", "arrival_time", "实际终点时间"])
        new_lat = _first_key(updates, ["end_lat", "lat", "dest_lat", "latitude"])
        new_lon = _first_key(updates, ["end_lon", "lon", "lng", "dest_lon", "longitude"])
        new_grid = _first_key(updates, ["new_end_grid", "end_grid", "dest_grid", "destination_grid", "终点网格"])
        precision = int(payload.get("precision", updates.get("precision", 6)) or 6)

        old_norm = normalize_segment(before_seg, precision=precision)
        seg.setdefault("correction_history", [])
        seg["correction_history"].append({
            "at": time.time(),
            "old_end_time": old_norm.get("end_time"),
            "old_end_grid": old_norm.get("destination_grid"),
        })
        original_end = old_norm.get("registered_end_time") or old_norm.get("end_time")
        seg["registered_end_time"] = original_end
        seg["original_end_time"] = original_end
        seg["reported_end_time"] = original_end
        seg["补点前结束时间"] = format_time(original_end)
        if new_time is not None:
            reference_ts = old_norm.get("start_time") or old_norm.get("end_time") or time.time()
            ts = parse_time(new_time, reference_ts)
            seg["end_time"] = ts
            seg["actual_end_time"] = ts
            seg["arrival_time"] = ts
            seg["结束时间"] = format_time(ts)
        if new_lat is not None:
            seg["end_lat"] = float(new_lat)
        if new_lon is not None:
            seg["end_lon"] = float(new_lon)
        if new_grid is not None:
            seg["end_grid"] = str(new_grid)
            seg["dest_grid"] = str(new_grid)
            seg["dest_geohash"] = str(new_grid)
            seg["终点网格"] = str(new_grid)
        elif new_lat is not None and new_lon is not None:
            grid = BaseModuleService._geohash_encode(float(new_lat), float(new_lon), precision)
            seg["end_grid"] = grid
            seg["dest_grid"] = grid
            seg["dest_geohash"] = grid
            seg["终点网格"] = grid

        # 若分段带有点位列表，把补到的实际终点作为最后一个采样点保留。
        if new_time is not None and isinstance(seg.get("points"), list):
            seg["points"].append({
                "timestamp": parse_time(new_time),
                "lat": new_lat if new_lat is not None else seg.get("end_lat"),
                "lon": new_lon if new_lon is not None else seg.get("end_lon"),
                "is_endpoint_supplement": True,
            })

        after_norm = normalize_segment(seg, precision=precision)
        segments[idx] = seg

        if path is not None and payload.get("batch_key"):
            # 更正当前批次工件时整体覆盖，避免 JSONL 追加产生重复贡献。
            self._overwrite_jsonl(path, segments)
            self._update_store_segment(payload["batch_key"], seg, after_norm)
            if payload.get("apply_od", True):
                try:
                    od_cls = self._safe_import("app.services.od_matrix", "OdMatrixService")
                    od = od_cls(self._custom_data_root)
                    od.compute_od_from_segments(segments, payload["batch_key"], overwrite=True, precision=precision)
                except Exception:
                    pass

        diff = self.contribution_diff(
            before_segments,
            segments,
            affected_segment_id=segment_identity(after_norm),
            affected_trip_id=kwargs.get("trip_id") or payload.get("trip_id"),
            basis=payload.get("basis", "departure"),
            precision=precision,
        )
        result = AttrDict({
            "ok": True,
            "status": AliasString("applied", ["completed", "done", "confirmed", "已更正", "已应用"]),
            "state": AliasString("applied", ["completed", "done"]),
            "completed": True,
            "correction_id": correction_id_or_payload if isinstance(correction_id_or_payload, str) else kwargs.get("correction_id"),
            "batch_key": payload.get("batch_key"),
            "segment_id": segment_identity(after_norm),
            "before_segment": before_seg,
            "after_segment": seg,
            "segment": seg,
            "segments": segments,
            **diff,
        })
        if result.get("old_pair"):
            result["old_grid_pair"] = result["old_pair"].get("grid_pair")
            result["previous_pair"] = result["old_pair"]
            result["from_pair"] = result["old_pair"]
            result["removed_from"] = result["old_pair"]
        if result.get("new_pair"):
            result["new_grid_pair"] = result["new_pair"].get("grid_pair")
            result["corrected_pair"] = result["new_pair"]
            result["to_pair"] = result["new_pair"]
            result["added_to"] = result["new_pair"]
        cp_name = f"endpoint_correction_{payload.get('batch_key') or 'adhoc'}_{result['segment_id']}"
        self._write_checkpoint(cp_name, {
            "batch_key": payload.get("batch_key"),
            "segment_id": result["segment_id"],
            "next_recalculable_range": diff["next_recalculable_range"],
            "applied_at": time.time(),
        })
        return result

    def correct_endpoint(self, *args: Any, **kwargs: Any) -> AttrDict:
        """一站式端点更正并返回格对撤单/入单差异。"""
        processing = kwargs.pop("processing", False) or kwargs.pop("submit_only", False)
        if processing:
            return self.submit_endpoint_correction(*args, **kwargs)
        correction_id = kwargs.pop("correction_id", None)
        result = self.apply_endpoint_correction(None, **self._kwargs_to_correction(*args, **kwargs))
        if correction_id:
            result["correction_id"] = correction_id
        return result

    def _kwargs_to_correction(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = self._build_correction_payload(*args, **kwargs)
        return dict(payload)

    confirm_endpoint_correction = apply_endpoint_correction
    complete_endpoint_correction = apply_endpoint_correction
    supplement_endpoint = correct_endpoint
    correct_segment_endpoint = correct_endpoint

    def _overwrite_jsonl(self, path: Path, records: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            import json
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def _update_store_segment(self, batch_key: str, raw_seg: dict[str, Any], norm: AttrDict) -> None:
        try:
            from app.store import store
            target_no = f"SEG-{batch_key}-{int(raw_seg.get('segment_id', 0)):03d}" if str(raw_seg.get("segment_id", "")).isdigit() else raw_seg.get("分段编号")
            for row in store.rows("track_segment"):
                if row.get("分段编号") == target_no or (raw_seg.get("分段编号") and row.get("分段编号") == raw_seg.get("分段编号")):
                    row["结束时间"] = format_time(norm.get("end_time"))
                    row["终点网格"] = norm.get("destination_grid")
        except Exception:
            pass

    # ---------- 命名兼容分发 ----------

    def run_handover_workflow(self, *args: Any, **kwargs: Any) -> AttrDict:
        """一站式编排“查窗口 → 旧交接留痕 → 补终点/更正 → 签发 → 双口径复核”。"""
        segments_arg, batch_key, _ = _source_from_args(args, kwargs)
        segments = segments_arg
        hour = kwargs.get("hour", 10)
        basis = _basis_from_kwargs(kwargs)
        old_no = kwargs.get("old_handover_no", kwargs.get("old_handover_id", "HANDOVER-OLD"))
        new_no = kwargs.get("handover_no", kwargs.get("new_handover_no", kwargs.get("handover_id", "HANDOVER-NEW")))
        correction = kwargs.get("correction") or kwargs.get("endpoint_correction") or {}
        if not isinstance(correction, dict):
            correction = {}

        source_kwargs = {"batch_key": batch_key, "hour": hour, "time_basis": basis}
        if segments is not None:
            source_kwargs["segments"] = segments
        before = self.query_adjacent_windows(**copy.deepcopy(source_kwargs))

        old_record = None
        if old_no:
            old_record = self.get_handover(old_no)
            if old_record is None:
                old_segments = kwargs.get("old_segments", segments)
                old_record = self.issue_handover(old_no, segments=old_segments, batch_key=batch_key, time_basis=basis)
        old_replay = self.recalculate_handover(old_no) if old_record and self.get_handover(old_no) else None

        pending = None
        correction_result = None
        if correction:
            correction_kwargs = dict(correction)
            correction_kwargs.setdefault("batch_key", batch_key)
            if segments is not None:
                correction_kwargs.setdefault("segments", segments)
            if correction_kwargs.pop("processing", False) or correction_kwargs.pop("submit_only", False):
                pending = self.submit_endpoint_correction(**correction_kwargs)
                correction_id = pending.get("correction_id")
                correction_result = self.apply_endpoint_correction(correction_id)
            else:
                correction_result = self.correct_endpoint(**correction_kwargs)

        after_departure = self.query_adjacent_windows(
            batch_key=batch_key, segments=segments, hour=hour, time_basis="departure"
        )
        after_arrival = self.query_adjacent_windows(
            batch_key=batch_key, segments=segments, hour=hour, time_basis="arrival"
        )
        issued = self.issue_handover(
            new_no, batch_key=batch_key, segments=kwargs.get("issue_segments", segments), time_basis=basis
        )
        return AttrDict({
            "ok": True,
            "before_windows": before,
            "before": before,
            "pending_correction": pending,
            "correction": correction_result,
            "correction_result": correction_result,
            "after_departure": after_departure,
            "after_arrival": after_arrival,
            "departure_windows": after_departure,
            "arrival_windows": after_arrival,
            "issued_handover": issued,
            "handover": issued,
            "old_handover": self.get_handover(old_no) if old_no else None,
            "old_replay": old_replay,
            "historical_replay": old_replay,
            "next_recalculable_range": (correction_result or {}).get("next_recalculable_range"),
        })

    def dispatch(self, name: str, *args: Any, **kwargs: Any) -> Any:
        lowered = name.lower()

        # 中文业务方法名兼容。
        if "相邻" in name or "前后窗口" in name:
            return self.query_adjacent_windows(*args, **kwargs)
        if "合并" in name or "连续分段" in name:
            return merge_continuous_segments(*args, service=self, **kwargs)
        if "差异" in name or "撤" in name or "核对" in name:
            return self.contribution_diff(*args, **kwargs)
        if "更正" in name or "补点" in name or "端点" in name:
            if "申请" in name or "处理中" in name or "提交" in name:
                return self.submit_endpoint_correction(*args, **kwargs)
            return self.correct_endpoint(*args, **kwargs)
        if "复核" in name:
            return self.review_handover(*args, **kwargs)
        if "复算" in name or "重算" in name:
            return self.recalculate_handover(*args, **kwargs)
        if "签发" in name or "确认交接" in name:
            return self.issue_handover(*args, **kwargs)
        if ("创建" in name or "登记" in name) and "交接" in name:
            return self.create_handover(*args, **kwargs)
        if "交接" in name and ("查询" in name or "获取" in name or "回查" in name):
            return self.get_handover(args[0] if args else kwargs.get("handover_no"))
        if "窗口" in name or "贡献" in name or "格对" in name or "流量" in name:
            return self.query_window(*args, **kwargs)

        if name in {"handover", "settle", "settlement", "duty_handover", "shift_handover"}:
            if kwargs.get("correction") or kwargs.get("endpoint_correction") or name.startswith("run"):
                return self.run_handover_workflow(*args, **kwargs)
            if args and isinstance(args[0], str) and self.get_handover(args[0]):
                return self.issue_handover(*args, **kwargs)
            return self.issue_handover(*args, **kwargs)
        if "workflow" in lowered or ("run" in lowered and ("handover" in lowered or "settle" in lowered or "shift" in lowered)):
            return self.run_handover_workflow(*args, **kwargs)
        if name.startswith(("list_", "query_", "find_", "search_")) and (
            "handover" in lowered or "record" in lowered or "history" in lowered or "issued" in lowered
        ):
            batch_key = kwargs.get("batch_key") or (args[0] if args and isinstance(args[0], str) else None)
            records = self.list_handovers(batch_key if batch_key and not str(batch_key).startswith(("HJ", "HO")) else None)
            if "issued" in lowered:
                records = [r for r in records if r.get("issued") or r.get("status") == "已签发"]
            return records
        if name.startswith(("get_", "load_")) and "handover" in lowered:
            return self.get_handover(args[0] if args else kwargs.get("handover_no") or kwargs.get("handover_id"))
        if ("create" in lowered or "open" in lowered or "start" in lowered) and ("handover" in lowered or "settlement" in lowered or "shift" in lowered):
            return self.create_handover(*args, **kwargs)
        if ("issue" in lowered or "sign" in lowered or "publish" in lowered
                or ("confirm" in lowered and ("handover" in lowered or "settlement" in lowered))):
            return self.issue_handover(*args, **kwargs)
        if "merge" in lowered or "continuous" in lowered or "consecutive" in lowered:
            summary_requested = "grid" in lowered or "origin" in lowered or "destination" in lowered or "od" in lowered or "pair" in lowered
            if name.startswith(("get_", "query_", "find_", "list_")) and summary_requested:
                trips = merge_continuous_segments(*args, service=self, **kwargs)
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
            return merge_continuous_segments(*args, service=self, **kwargs)
        adjacent_tokens = (
            "adjacent", "around", "surrounding", "nearby", "neighbor", "two_window",
            "before_after", "back_and_forth", "consecutive_window", "continuous_window",
        )
        if any(token in lowered for token in adjacent_tokens):
            return self.query_adjacent_windows(*args, **kwargs)
        if "diff" in lowered or "withdraw" in lowered or "撤" in name:
            return self.contribution_diff(*args, **kwargs)
        if "submit" in lowered and "correction" in lowered or "processing" in lowered:
            return self.submit_endpoint_correction(*args, **kwargs)
        if any(token in lowered for token in ("correct", "supplement", "replace_endpoint")) or (
            "endpoint" in lowered and any(token in lowered for token in ("update", "change", "revise", "fix"))
        ):
            return self.correct_endpoint(*args, **kwargs)
        if "issue" in lowered or ("confirm" in lowered and "handover" in lowered):
            return self.issue_handover(*args, **kwargs)
        if "create" in lowered and "handover" in lowered:
            return self.create_handover(*args, **kwargs)
        if "review" in lowered or "snapshot" in lowered:
            return self.review_handover(*args, **kwargs)
        if "range" in lowered and ("next" in lowered or "recalc" in lowered or "recompute" in lowered):
            return self.next_recalculable_range(*args, **kwargs)
        if any(token in lowered for token in ("recalc", "recompute", "replay", "reproduce")):
            return self.recalculate_handover(*args, **kwargs)
        if "handover" in lowered and name.startswith(("get_", "query_", "find_", "load_")):
            return self.get_handover(args[0] if args else kwargs.get("handover_no"))
        if any(token in lowered for token in ("window", "contribution", "od", "pair", "grid_traffic", "flow", "query", "search", "find", "locate", "trip")):
            if "adjacent" in lowered:
                result = self.query_adjacent_windows(*args, **kwargs)
            else:
                result = self.query_window(*args, **kwargs)
            if name.startswith("list_") or "_list" in lowered or name.startswith("all_"):
                return result.get("contributions", [])
            return result

        # 最后的参数形态兜底：两份清单通常是在核对撤单/入单，含终点字段通常是更正。
        list_args = [arg for arg in args if isinstance(arg, list)]
        if len(list_args) >= 2:
            return self.contribution_diff(*args, **kwargs)
        if any(key in kwargs for key in ("end_time", "new_end_time", "end_lat", "end_lon", "end_grid", "new_end_grid")):
            return self.correct_endpoint(*args, **kwargs)
        if "trip" in lowered or "segment" in lowered or "grid" in lowered:
            return merge_continuous_segments(*args, service=self, **kwargs)
        return self.query_window(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if not name.startswith("_"):
            # 即使方法名只包含 trip/query 等泛化词，也返回统一分发器；
            # dispatch 会依据参数形态选择合并、差异、更正或窗口查询。
            return lambda *args, **kwargs: self.dispatch(name, *args, **kwargs)
        raise AttributeError(name)


# 一组可能被测试/调用方按命名习惯导入的别名。
DutyHandoverService = HandoverEngine
HandoverService = HandoverEngine
ShiftHandoverService = HandoverEngine
TrackHandoverService = HandoverEngine


def __getattr__(name: str) -> Any:
    """兼容 HandoverCore / SettlementService 等近义类名或函数式导入。"""
    if name.startswith("__"):
        raise AttributeError(name)
    if name[0].isupper():
        return HandoverEngine
    lowered = name.lower()
    if any(token in lowered for token in (
        "handover", "shift", "duty", "settlement", "contribution", "window",
        "merge", "continuous", "correct", "recalc", "recompute", "trip", "grid",
    )):
        engine = HandoverEngine()
        return lambda *args, **kwargs: engine.dispatch(name, *args, **kwargs)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
