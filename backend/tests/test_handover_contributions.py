"""值守交接、窗口格对贡献、连续分段合并与端点更正一致性测试。"""
from __future__ import annotations

from datetime import datetime

from app.services.duty_handover import DutyHandoverService
from app.services.od_matrix import OdMatrixService
from app.services.track_segment import TrackSegmentService


def _ts(hour: int, minute: int) -> float:
    return datetime(2026, 9, 24, hour, minute).timestamp()


def _two_trips() -> list[dict]:
    return [
        {
            "segment_id": "trip-a",
            "terminal": "A",
            "start_time": _ts(9, 40),
            "end_time": _ts(9, 48),
            "origin_grid": "origin-a",
            "end_grid": "grid-a",
        },
        {
            "segment_id": "trip-b",
            "terminal": "B",
            "start_time": _ts(9, 50),
            "end_time": _ts(9, 58),
            "registered_end_time": _ts(9, 58),
            "origin_grid": "origin-b",
            "end_grid": "grid-old",
        },
    ]


def test_departure_and_arrival_windows_count_crossing_trip_once(tmp_path):
    od = OdMatrixService(tmp_path)
    segments = _two_trips()
    segments[1]["end_time"] = _ts(10, 3)
    segments[1]["arrival_time"] = _ts(10, 3)
    segments[1]["dest_grid"] = "grid-new"

    departure = od.query_adjacent_windows(
        segments, hour=10, reference=_ts(10, 0), time_basis="departure"
    )
    arrival = od.query_adjacent_windows(
        segments, hour=10, reference=_ts(10, 0), time_basis="arrival"
    )

    assert [w["total_trips"] for w in departure["windows"]] == [2, 0]
    assert [w["total_trips"] for w in arrival["windows"]] == [1, 1]
    assert departure["total_trips"] == 2
    assert arrival["total_trips"] == 2
    assert departure["duplicate_count"] == 0
    assert arrival["duplicate_trip_ids"] == []


def test_issued_handover_replays_original_pair_after_endpoint_correction(tmp_path):
    track = TrackSegmentService(tmp_path)
    od = OdMatrixService(tmp_path)
    handover = DutyHandoverService(tmp_path)
    segments = _two_trips()
    batch_key = "HANDOVER-SCENARIO"
    track._overwrite_jsonl(track._data_dir("track_segments") / f"{batch_key}_segments.jsonl", segments)

    handover.issue_handover("HJ-OLD", batch_key=batch_key, time_basis="departure")

    pending = track.submit_endpoint_correction(
        batch_key=batch_key,
        segment_id="trip-b",
        end_time=_ts(10, 3),
        end_grid="grid-new",
    )
    assert pending["status"] == "processing"

    processing_replay = od.recalculate_handover("HJ-OLD")
    assert any(c["dest_grid"] == "grid-old" for c in processing_replay["contributions"])

    diff = track.apply_endpoint_correction(pending["correction_id"])
    assert diff["removed"][0]["trip_id"] == "trip-b"
    assert diff["removed"][0]["dest_grid"] == "grid-old"
    assert diff["added"][0]["trip_id"] == "trip-b"
    assert diff["added"][0]["dest_grid"] == "grid-new"
    assert diff["next_recalculable_range"]["bucket_starts"]

    old_replay = handover.recalculate_handover("HJ-OLD")
    assert any(c["dest_grid"] == "grid-old" for c in old_replay["contributions"])
    assert not any(c["dest_grid"] == "grid-new" for c in old_replay["contributions"])

    current_departure = od.query_contributions(
        batch_key=batch_key, hour=9, reference=_ts(9, 0), time_basis="departure"
    )
    assert any(c["dest_grid"] == "grid-new" for c in current_departure["contributions"])


def test_merge_continuous_segments_returns_actual_trip_endpoint_grids():
    track = TrackSegmentService()
    trips = track.merge_continuous_segments([
        {"segment_id": 1, "terminal": "T", "start_grid": "o", "end_grid": "middle"},
        {"segment_id": 2, "terminal": "T", "start_grid": "middle", "end_grid": "d"},
    ])
    grids = track.get_merged_segment_grids([
        {"segment_id": 1, "terminal": "T", "start_grid": "o", "end_grid": "middle"},
        {"segment_id": 2, "terminal": "T", "start_grid": "middle", "end_grid": "d"},
    ])

    assert len(trips) == 1
    assert trips[0]["origin_grid"] == "o"
    assert trips[0]["destination_grid"] == "d"
    assert grids[0]["grid_pair"] == "o->d"
