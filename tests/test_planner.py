from datetime import datetime

import pytest

from planner import TimetableError, find_itinerary, parse_time, validate_timetable


def tt(stations, trains):
    return validate_timetable({"stations": stations, "trains": trains})


def network():
    """A -> B -> C 网络：T1 全程，T2/T3 从 B 到 C。"""
    return tt(
        [
            {"id": "A", "transfer_minutes": 10},
            {"id": "B", "transfer_minutes": 10},
            {"id": "C", "transfer_minutes": 10},
        ],
        [
            {"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "B", "arrive": "2026-10-01T09:00", "depart": "2026-10-01T09:10"},
                {"station": "C", "arrive": "2026-10-01T10:00", "depart": None},
            ]},
            {"id": "T2", "stops": [
                {"station": "B", "arrive": None, "depart": "2026-10-01T09:15"},
                {"station": "C", "arrive": "2026-10-01T09:45", "depart": None},
            ]},
            {"id": "T3", "stops": [
                {"station": "B", "arrive": None, "depart": "2026-10-01T09:05"},
                {"station": "C", "arrive": "2026-10-01T09:30", "depart": None},
            ]},
        ],
    )


# ---------- 时间解析 ----------

def test_parse_time_ok():
    assert parse_time("2026-10-01T08:00") == datetime(2026, 10, 1, 8, 0)


@pytest.mark.parametrize("bad", [
    "2026-10-01 08:00",   # 缺少 T
    "2026-10-01T08:00:00",  # 多秒
    "2026-13-01T08:00",   # 月份越界
    "2026-02-30T08:00",   # 不存在的日期
    "2026-10-01T25:00",   # 小时越界
    "08:00",
    123,
    None,
])
def test_parse_time_invalid(bad):
    with pytest.raises(TimetableError):
        parse_time(bad)


# ---------- 时刻表校验 ----------

def test_validate_unknown_station_reference():
    with pytest.raises(TimetableError, match="未定义的站点"):
        tt(
            [{"id": "A", "transfer_minutes": 5}],
            [{"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "B", "arrive": "2026-10-01T09:00", "depart": None},
            ]}],
        )


def test_validate_time_sequence_between_stops():
    with pytest.raises(TimetableError, match="时序"):
        tt(
            [{"id": "A", "transfer_minutes": 5}, {"id": "B", "transfer_minutes": 5}],
            [{"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "B", "arrive": "2026-10-01T07:00", "depart": None},
            ]}],
        )


def test_validate_depart_before_arrive_at_same_stop():
    with pytest.raises(TimetableError, match="发车时间早于到达时间"):
        tt(
            [{"id": s, "transfer_minutes": 5} for s in ("A", "B", "C")],
            [{"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "B", "arrive": "2026-10-01T09:00", "depart": "2026-10-01T08:30"},
                {"station": "C", "arrive": "2026-10-01T10:00", "depart": None},
            ]}],
        )


def test_validate_duplicate_station_id():
    with pytest.raises(TimetableError, match="重复"):
        tt(
            [{"id": "A", "transfer_minutes": 5}, {"id": "A", "transfer_minutes": 3}],
            [],
        )


def test_validate_duplicate_train_id():
    with pytest.raises(TimetableError, match="重复"):
        tt(
            [{"id": "A", "transfer_minutes": 5}, {"id": "B", "transfer_minutes": 5}],
            [
                {"id": "T1", "stops": [
                    {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
                    {"station": "B", "arrive": "2026-10-01T09:00", "depart": None},
                ]},
                {"id": "T1", "stops": [
                    {"station": "A", "arrive": None, "depart": "2026-10-01T10:00"},
                    {"station": "B", "arrive": "2026-10-01T11:00", "depart": None},
                ]},
            ],
        )


def test_validate_requires_two_stops():
    with pytest.raises(TimetableError, match="至少"):
        tt(
            [{"id": "A", "transfer_minutes": 5}],
            [{"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
            ]}],
        )


def test_validate_bad_time_format_in_stop():
    with pytest.raises(TimetableError, match="YYYY-MM-DDTHH:MM"):
        tt(
            [{"id": "A", "transfer_minutes": 5}, {"id": "B", "transfer_minutes": 5}],
            [{"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01 08:00"},
                {"station": "B", "arrive": "2026-10-01T09:00", "depart": None},
            ]}],
        )


# ---------- 行程搜索 ----------

def test_transfer_beats_later_direct_and_buffer_enforced():
    # T1 到 B 为 09:00，B 站换乘需 10 分钟：T3(09:05) 赶不上，T2(09:15) 可以
    result = find_itinerary(network(), "A", "C", parse_time("2026-10-01T08:00"))
    assert result is not None
    assert result["train_sequence"] == ["T1", "T2"]
    assert result["transfers"] == 1
    assert result["arrive"] == datetime(2026, 10, 1, 9, 45)
    assert [s["from"] for s in result["segments"]] == ["A", "B"]
    assert [s["to"] for s in result["segments"]] == ["B", "C"]


def test_same_train_through_stops_needs_no_transfer():
    # B -> C 直达：T3 09:05 发车 09:30 到，无需换乘
    result = find_itinerary(network(), "B", "C", parse_time("2026-10-01T08:00"))
    assert result["train_sequence"] == ["T3"]
    assert result["transfers"] == 0
    assert len(result["segments"]) == 1


def test_first_departure_not_before_earliest():
    # T1 08:00 从 A 发车，早于 08:30 的约束，A 没有其他班次
    assert find_itinerary(network(), "A", "C", parse_time("2026-10-01T08:30")) is None
    # 08:00 整可以乘坐
    assert find_itinerary(network(), "A", "C", parse_time("2026-10-01T08:00")) is not None


def test_cross_midnight_by_date():
    timetable = tt(
        [{"id": s, "transfer_minutes": 5} for s in ("A", "B", "C")],
        [
            {"id": "N1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T23:30"},
                {"station": "B", "arrive": "2026-10-01T23:55", "depart": None},
            ]},
            {"id": "N2", "stops": [
                {"station": "B", "arrive": None, "depart": "2026-10-02T00:05"},
                {"station": "C", "arrive": "2026-10-02T01:30", "depart": None},
            ]},
        ],
    )
    # 23:55 + 5 分钟换乘 = 00:00 <= 00:05，跨午夜换乘可行
    result = find_itinerary(timetable, "A", "C", parse_time("2026-10-01T23:00"))
    assert result is not None
    assert result["train_sequence"] == ["N1", "N2"]
    assert result["arrive"] == datetime(2026, 10, 2, 1, 30)


def test_tie_prefers_fewer_transfers():
    timetable = tt(
        [{"id": s, "transfer_minutes": 5} for s in ("A", "M", "B")],
        [
            {"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "B", "arrive": "2026-10-01T12:00", "depart": None},
            ]},
            {"id": "T2", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "M", "arrive": "2026-10-01T10:00", "depart": None},
            ]},
            {"id": "T3", "stops": [
                {"station": "M", "arrive": None, "depart": "2026-10-01T10:10"},
                {"station": "B", "arrive": "2026-10-01T12:00", "depart": None},
            ]},
        ],
    )
    # 直达与换乘同为 12:00 到达，选换乘少的直达
    result = find_itinerary(timetable, "A", "B", parse_time("2026-10-01T07:00"))
    assert result["train_sequence"] == ["T1"]
    assert result["transfers"] == 0


def test_tie_prefers_lexicographically_smaller_train_sequence():
    timetable = tt(
        [{"id": s, "transfer_minutes": 5} for s in ("A", "M", "B")],
        [
            {"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "M", "arrive": "2026-10-01T09:00", "depart": None},
            ]},
            {"id": "G9", "stops": [
                {"station": "M", "arrive": None, "depart": "2026-10-01T09:30"},
                {"station": "B", "arrive": "2026-10-01T11:00", "depart": None},
            ]},
            {"id": "G10", "stops": [
                {"station": "M", "arrive": None, "depart": "2026-10-01T09:30"},
                {"station": "B", "arrive": "2026-10-01T11:00", "depart": None},
            ]},
        ],
    )
    # G9 与 G10 同时到发，"G10" < "G9"（字典序），选 G10
    result = find_itinerary(timetable, "A", "B", parse_time("2026-10-01T07:00"))
    assert result["train_sequence"] == ["T1", "G10"]


def test_no_route_returns_none():
    assert find_itinerary(network(), "C", "A", parse_time("2026-10-01T00:00")) is None


def test_same_origin_and_destination_rejected():
    with pytest.raises(TimetableError):
        find_itinerary(network(), "A", "A", parse_time("2026-10-01T08:00"))


# ---------- 班次回访同一站点 ----------

def revisit_network():
    """T 回访站点 B（B→D→B）；Q 从 D 到 E。"""
    return tt(
        [
            {"id": "B", "transfer_minutes": 0},
            {"id": "D", "transfer_minutes": 2},
            {"id": "E", "transfer_minutes": 0},
        ],
        [
            {"id": "T", "stops": [
                {"station": "B", "arrive": None, "depart": "2026-10-01T09:05"},
                {"station": "D", "arrive": "2026-10-01T10:00", "depart": "2026-10-01T10:05"},
                {"station": "B", "arrive": "2026-10-01T11:00", "depart": None},
            ]},
            {"id": "Q", "stops": [
                {"station": "D", "arrive": None, "depart": "2026-10-01T10:03"},
                {"station": "E", "arrive": "2026-10-01T10:30", "depart": None},
            ]},
        ],
    )


def test_revisited_station_no_time_travel():
    # T 的 D→B（10:05→11:00）不能接上它早已发出的 B→D（09:05→10:00）再换 Q；
    # Q 10:03 从 D 发车早于 10:05，故 D→E 无可乘行程
    assert find_itinerary(revisit_network(), "D", "E", parse_time("2026-10-01T10:05")) is None


def test_revisited_station_forward_ride_and_transfer_ok():
    # 合法行程不受影响：T 从 B 到 D（09:05→10:00），在 D 换 Q（10:03→10:30）
    result = find_itinerary(revisit_network(), "B", "E", parse_time("2026-10-01T09:00"))
    assert result is not None
    assert result["train_sequence"] == ["T", "Q"]
    assert result["transfers"] == 1
    assert result["arrive"] == datetime(2026, 10, 1, 10, 30)
    assert [(s["from"], s["to"]) for s in result["segments"]] == [("B", "D"), ("D", "E")]
    # 各段到发连续且可乘：10:00 到 D，10:03 发车（D 站换乘 2 分钟）
    assert result["segments"][0]["depart"] == datetime(2026, 10, 1, 9, 5)
    assert result["segments"][1]["depart"] == datetime(2026, 10, 1, 10, 3)


def test_same_train_revisit_continues_forward():
    # T 回访 B 后继续到 C：D→C 可同班次乘坐，沿后续停靠点前进
    timetable = tt(
        [{"id": s, "transfer_minutes": 0} for s in ("B", "C", "D")],
        [
            {"id": "T", "stops": [
                {"station": "B", "arrive": None, "depart": "2026-10-01T09:05"},
                {"station": "D", "arrive": "2026-10-01T10:00", "depart": "2026-10-01T10:05"},
                {"station": "B", "arrive": "2026-10-01T11:00", "depart": "2026-10-01T11:10"},
                {"station": "C", "arrive": "2026-10-01T12:00", "depart": None},
            ]},
        ],
    )
    result = find_itinerary(timetable, "D", "C", parse_time("2026-10-01T10:00"))
    assert result is not None
    assert result["train_sequence"] == ["T"]
    assert result["transfers"] == 0
    assert result["arrive"] == datetime(2026, 10, 1, 12, 0)
    assert len(result["segments"]) == 1
    # 09:05 的 B→D 已发出，只能等 T 回访 B 时（11:10）上车
    result = find_itinerary(timetable, "B", "C", parse_time("2026-10-01T09:06"))
    assert result is not None
    assert result["depart"] == datetime(2026, 10, 1, 11, 10)
    assert result["arrive"] == datetime(2026, 10, 1, 12, 0)
