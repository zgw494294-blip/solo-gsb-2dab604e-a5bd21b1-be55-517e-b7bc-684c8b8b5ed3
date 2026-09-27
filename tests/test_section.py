"""单线区间冲突排查：区间通过提取、冲突判定、最少暂缓集合与 API。"""
from datetime import datetime, timedelta

import pytest

from app import create_app
from planner import (
    TimetableError,
    analyze_section_conflicts,
    collect_section_passages,
    validate_timetable,
)

STATIONS = [
    {"id": "A", "transfer_minutes": 5},
    {"id": "B", "transfer_minutes": 5},
    {"id": "C", "transfer_minutes": 5},
]


def tt(trains):
    return validate_timetable({"stations": STATIONS, "trains": trains})


def direct_train(tid, frm, to, depart, arrive, day_from="2026-10-01", day_to=None):
    """构造 frm → to 直达班次（首站发车、末站到达），时间为 "HH:MM"。"""
    day_to = day_to or day_from
    return {"id": tid, "stops": [
        {"station": frm, "arrive": None, "depart": f"{day_from}T{depart}"},
        {"station": to, "arrive": f"{day_to}T{arrive}", "depart": None},
    ]}


def shuttle_train(tid):
    """A→B→A→B 往返班次：三次经过 A～B 区间（两个方向）。"""
    return {"id": tid, "stops": [
        {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
        {"station": "B", "arrive": "2026-10-01T08:30", "depart": "2026-10-01T08:40"},
        {"station": "A", "arrive": "2026-10-01T09:10", "depart": "2026-10-01T09:20"},
        {"station": "B", "arrive": "2026-10-01T09:50", "depart": None},
    ]}


def analyze(trains, clearance=0):
    return analyze_section_conflicts(tt(trains), "A", "B", clearance)


def conflict_pairs(result):
    return [
        (c["first"]["train_id"], c["second"]["train_id"])
        for c in result["conflicts"]
    ]


# ---------- 区间通过提取 ----------

def test_passages_both_directions_and_stop_index():
    timetable = tt([
        direct_train("T1", "A", "B", "08:00", "08:40"),
        direct_train("T2", "B", "A", "09:00", "09:40"),
        {"id": "T3", "stops": [  # C→A→B：A→B 通过的起点停靠序号为 1
            {"station": "C", "arrive": None, "depart": "2026-10-01T10:00"},
            {"station": "A", "arrive": "2026-10-01T10:20", "depart": "2026-10-01T10:30"},
            {"station": "B", "arrive": "2026-10-01T11:00", "depart": None},
        ]},
        direct_train("T4", "A", "C", "12:00", "12:40"),  # 不经过 A～B 区间
    ])
    passages = collect_section_passages(timetable, "A", "B")
    assert [(p["train_id"], p["stop_index"], p["from"], p["to"]) for p in passages] == [
        ("T1", 0, "A", "B"),
        ("T2", 0, "B", "A"),
        ("T3", 1, "A", "B"),
    ]
    # 占用为发车至到达，按发车时间排序
    assert passages[0]["start"] == datetime(2026, 10, 1, 8, 0)
    assert passages[0]["end"] == datetime(2026, 10, 1, 8, 40)
    assert passages[2]["start"] == datetime(2026, 10, 1, 10, 30)


def test_passages_same_train_multiple_times():
    passages = collect_section_passages(tt([shuttle_train("T1")]), "A", "B")
    assert [(p["stop_index"], p["from"], p["to"]) for p in passages] == [
        (0, "A", "B"),
        (1, "B", "A"),
        (2, "A", "B"),
    ]


# ---------- 冲突判定 ----------

def test_overlap_is_conflict():
    result = analyze([
        direct_train("T1", "A", "B", "08:00", "08:40"),
        direct_train("T2", "A", "B", "08:10", "08:50"),
    ])
    assert conflict_pairs(result) == [("T1", "T2")]
    # 暂缓任一班次即可，取班次 ID 字典序最小者
    assert result["suspended"] == ["T1"]


def test_gap_shorter_than_clearance_is_conflict():
    # 前车 08:40 驶离，后车 08:45 进入：间隔 5 分钟 < 10 分钟清空间隔
    result = analyze([
        direct_train("T1", "A", "B", "08:00", "08:40"),
        direct_train("T2", "A", "B", "08:45", "09:10"),
    ], clearance=10)
    assert conflict_pairs(result) == [("T1", "T2")]


def test_exact_clearance_is_not_conflict():
    # 恰好满足 10 分钟清空间隔，不算冲突
    result = analyze([
        direct_train("T1", "A", "B", "08:00", "08:40"),
        direct_train("T2", "A", "B", "08:50", "09:10"),
    ], clearance=10)
    assert result["conflicts"] == []
    assert result["suspended"] == []


def test_touching_with_zero_clearance_is_not_conflict():
    result = analyze([
        direct_train("T1", "A", "B", "08:00", "08:40"),
        direct_train("T2", "A", "B", "08:40", "09:10"),
    ])
    assert result["conflicts"] == []


def test_opposite_direction_also_conflicts():
    result = analyze([
        direct_train("T1", "A", "B", "08:00", "08:40"),
        direct_train("T2", "B", "A", "08:30", "09:00"),
    ])
    assert conflict_pairs(result) == [("T1", "T2")]


def test_full_date_comparison_no_false_conflict():
    # 同一钟点、不同日期：按完整日期判断，不算冲突
    result = analyze([
        direct_train("T1", "A", "B", "08:00", "08:40", day_from="2026-10-01"),
        direct_train("T2", "A", "B", "08:05", "08:45", day_from="2026-10-02"),
    ], clearance=30)
    assert result["conflicts"] == []
    assert result["suspended"] == []


def test_cross_midnight_conflict():
    result = analyze([
        direct_train("T1", "A", "B", "23:50", "00:20", day_to="2026-10-02"),
        direct_train("T2", "A", "B", "00:10", "00:50", day_from="2026-10-02"),
    ])
    assert conflict_pairs(result) == [("T1", "T2")]


def test_conflict_details():
    result = analyze([
        {"id": "T1", "stops": [  # C→A→B：A→B 通过起点停靠序号为 1
            {"station": "C", "arrive": None, "depart": "2026-10-01T07:30"},
            {"station": "A", "arrive": "2026-10-01T07:50", "depart": "2026-10-01T08:00"},
            {"station": "B", "arrive": "2026-10-01T08:40", "depart": None},
        ]},
        direct_train("T2", "B", "A", "08:30", "09:00"),
    ])
    (conflict,) = result["conflicts"]
    first, second = conflict["first"], conflict["second"]
    assert (first["train_id"], first["stop_index"]) == ("T1", 1)
    assert (first["from"], first["to"]) == ("A", "B")
    assert first["start"] == datetime(2026, 10, 1, 8, 0)
    assert first["end"] == datetime(2026, 10, 1, 8, 40)
    assert (second["train_id"], second["stop_index"]) == ("T2", 0)
    assert (second["from"], second["to"]) == ("B", "A")


# ---------- 最少暂缓集合 ----------

def test_same_train_multiple_passages_treated_as_whole():
    result = analyze([
        shuttle_train("T1"),                            # 三次通过 A～B 区间
        direct_train("T2", "A", "B", "08:10", "08:40"),  # 与 T1 第一次通过冲突
        direct_train("T3", "B", "A", "08:45", "09:15"),  # 与 T1 第二次通过冲突
    ])
    # 同一班次不与自身冲突；T2、T3 互不冲突
    assert conflict_pairs(result) == [("T1", "T2"), ("T1", "T3")]
    # 暂缓 T1 一个班次即整体解决其全部通过的冲突，优于分别暂缓 T2、T3
    assert result["suspended"] == ["T1"]


def test_minimum_suspension_path():
    # T2 与 T1、T3 分别冲突，T1 与 T3 不冲突：暂缓 T2 一个即可
    result = analyze([
        direct_train("T1", "A", "B", "08:00", "08:20"),
        direct_train("T2", "A", "B", "08:10", "08:50"),
        direct_train("T3", "A", "B", "08:40", "09:10"),
    ])
    assert result["suspended"] == ["T2"]


def test_minimum_suspension_triangle():
    # 三班两两冲突：最少暂缓 2 个，取排序后班次 ID 序列字典序最小者
    result = analyze([
        direct_train("T3", "A", "B", "08:00", "08:30"),
        direct_train("T1", "A", "B", "08:10", "08:40"),
        direct_train("T2", "A", "B", "08:20", "08:50"),
    ])
    assert result["suspended"] == ["T1", "T2"]


def test_minimum_suspension_lexicographic_tie():
    # 两对独立冲突：各取一个班次，取排序后字典序最小的组合
    result = analyze([
        direct_train("T2", "A", "B", "08:00", "08:30"),
        direct_train("T1", "A", "B", "08:10", "08:40"),
        direct_train("T4", "A", "B", "10:00", "10:30"),
        direct_train("T3", "A", "B", "10:10", "10:40"),
    ])
    assert result["suspended"] == ["T1", "T3"]


# ---------- 输入校验与上限 ----------

def test_too_many_passages_rejected():
    trains = []
    base = datetime(2026, 10, 1, 0, 0)
    for i in range(21):  # 21 次通过 > 20 次上限
        t0 = base + timedelta(minutes=30 * i)
        trains.append(direct_train(
            f"T{i:02d}", "A", "B", t0.strftime("%H:%M"),
            (t0 + timedelta(minutes=20)).strftime("%H:%M")))
    with pytest.raises(TimetableError, match="超过 20 次"):
        analyze(trains)


def test_twenty_passages_allowed():
    trains = []
    base = datetime(2026, 10, 1, 0, 0)
    for i in range(20):  # 恰好 20 次通过
        t0 = base + timedelta(minutes=30 * i)
        trains.append(direct_train(
            f"T{i:02d}", "A", "B", t0.strftime("%H:%M"),
            (t0 + timedelta(minutes=20)).strftime("%H:%M")))
    result = analyze(trains)
    assert len(result["passages"]) == 20
    assert result["conflicts"] == []
    assert result["suspended"] == []


def test_unknown_station_rejected():
    timetable = tt([direct_train("T1", "A", "B", "08:00", "08:40")])
    with pytest.raises(TimetableError, match="不存在"):
        analyze_section_conflicts(timetable, "A", "ZZZ", 0)
    with pytest.raises(TimetableError, match="不存在"):
        analyze_section_conflicts(timetable, None, "B", 0)


def test_same_station_rejected():
    timetable = tt([direct_train("T1", "A", "B", "08:00", "08:40")])
    with pytest.raises(TimetableError, match="不能相同"):
        analyze_section_conflicts(timetable, "A", "A", 0)


@pytest.mark.parametrize("clearance", [-1, 31, 2.5, "10", True, None])
def test_bad_clearance_rejected(clearance):
    timetable = tt([direct_train("T1", "A", "B", "08:00", "08:40")])
    with pytest.raises(TimetableError, match="清空间隔"):
        analyze_section_conflicts(timetable, "A", "B", clearance)


def test_not_adjacent_rejected():
    # A、C 间没有直接相邻停靠（T1 经 B 中转）
    timetable = tt([{"id": "T1", "stops": [
        {"station": "A", "arrive": None, "depart": "2026-10-01T08:00"},
        {"station": "B", "arrive": "2026-10-01T08:30", "depart": "2026-10-01T08:40"},
        {"station": "C", "arrive": "2026-10-01T09:10", "depart": None},
    ]}])
    with pytest.raises(TimetableError, match="直接相邻"):
        analyze_section_conflicts(timetable, "A", "C", 0)


# ---------- API ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SAMPLE_TIMETABLE", "")
    application = create_app()
    application.config["TESTING"] = True
    with application.test_client() as client:
        yield client


def api_payload():
    # T1 [08:00,08:40] 与 T2 [08:50,09:30] 间隔恰好 10 分钟；T3 反向且远离
    return {
        "stations": STATIONS,
        "trains": [
            direct_train("T1", "A", "B", "08:00", "08:40"),
            direct_train("T2", "A", "B", "08:50", "09:30"),
            direct_train("T3", "B", "A", "10:30", "11:10"),
        ],
    }


def import_api_sample(client):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]


def test_api_section_conflicts_ok(client):
    import_api_sample(client)
    res = client.post("/api/section-conflicts", json={
        "station_a": "A", "station_b": "B", "clearance_minutes": 15,
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"]
    assert data["station_a"] == "A" and data["station_b"] == "B"
    assert data["clearance_minutes"] == 15
    assert data["passage_count"] == 3
    # 间隔 10 分钟 < 15 分钟清空间隔：T1 与 T2 冲突
    (conflict,) = data["conflicts"]
    assert conflict["first"] == {
        "train_id": "T1", "stop_index": 0, "from": "A", "to": "B",
        "direction": "A→B",
        "start": "2026-10-01T08:00", "end": "2026-10-01T08:40",
    }
    assert conflict["second"]["train_id"] == "T2"
    assert conflict["second"]["direction"] == "A→B"
    assert data["suspended"] == ["T1"]


def test_api_section_conflicts_exact_clearance_no_conflict(client):
    import_api_sample(client)
    res = client.post("/api/section-conflicts", json={
        "station_a": "A", "station_b": "B", "clearance_minutes": 10,
    })
    data = res.get_json()
    assert res.status_code == 200
    assert data["ok"]
    assert data["conflicts"] == []   # 恰好满足 10 分钟间隔，不算冲突
    assert data["suspended"] == []


def test_api_analysis_does_not_modify_timetable(client):
    import_api_sample(client)
    before = client.get("/api/timetable").get_json()
    versions_before = client.get("/api/timetable-versions").get_json()
    res = client.post("/api/section-conflicts", json={
        "station_a": "A", "station_b": "B", "clearance_minutes": 15,
    })
    assert res.get_json()["ok"]
    assert client.get("/api/timetable").get_json() == before
    assert client.get("/api/timetable-versions").get_json() == versions_before


def test_api_section_conflicts_validation(client):
    import_api_sample(client)
    # 非 JSON 请求体
    res = client.post("/api/section-conflicts", data="x", content_type="text/plain")
    assert res.status_code == 400
    # 站点不存在
    assert client.post("/api/section-conflicts", json={
        "station_a": "A", "station_b": "ZZZ", "clearance_minutes": 5,
    }).status_code == 400
    # 两站相同
    assert client.post("/api/section-conflicts", json={
        "station_a": "A", "station_b": "A", "clearance_minutes": 5,
    }).status_code == 400
    # 清空间隔越界 / 类型错误
    for bad in (-1, 31, "5", 2.5, True, None):
        res = client.post("/api/section-conflicts", json={
            "station_a": "A", "station_b": "B", "clearance_minutes": bad,
        })
        assert res.status_code == 400
    # 两站在当前时刻表中没有直接相邻停靠
    res = client.post("/api/section-conflicts", json={
        "station_a": "A", "station_b": "C", "clearance_minutes": 5,
    })
    assert res.status_code == 400
    assert "直接相邻" in res.get_json()["error"]


def test_api_section_conflicts_without_timetable(client):
    res = client.post("/api/section-conflicts", json={
        "station_a": "A", "station_b": "B", "clearance_minutes": 5,
    })
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]


def test_api_too_many_passages(client):
    trains = []
    base = datetime(2026, 10, 1, 0, 0)
    for i in range(21):
        t0 = base + timedelta(minutes=30 * i)
        trains.append(direct_train(
            f"T{i:02d}", "A", "B", t0.strftime("%H:%M"),
            (t0 + timedelta(minutes=20)).strftime("%H:%M")))
    assert client.post("/api/import", json={
        "stations": STATIONS, "trains": trains,
    }).get_json()["ok"]
    res = client.post("/api/section-conflicts", json={
        "station_a": "A", "station_b": "B", "clearance_minutes": 5,
    })
    assert res.status_code == 400
    assert "超过 20 次" in res.get_json()["error"]
