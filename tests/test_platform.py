from datetime import datetime, timedelta

import pytest

from app import create_app
from planner import (
    TimetableError,
    collect_station_occupations,
    plan_platforms,
    validate_timetable,
)

STATIONS = [
    {"id": "A", "transfer_minutes": 5},
    {"id": "S", "transfer_minutes": 5},
    {"id": "B", "transfer_minutes": 5},
]


def make_train(tid, arrive_s, depart_s, day="2026-10-01"):
    """构造 A → S → B 班次；arrive_s/depart_s 为 "HH:MM"，None 表示首/末站。"""
    stops = []
    if arrive_s is None:  # S 为首站
        stops.append({"station": "S", "arrive": None, "depart": f"{day}T{depart_s}"})
        stops.append({"station": "B", "arrive": f"{day}T23:59", "depart": None})
    elif depart_s is None:  # S 为末站
        stops.append({"station": "A", "arrive": None, "depart": f"{day}T00:01"})
        stops.append({"station": "S", "arrive": f"{day}T{arrive_s}", "depart": None})
    else:
        stops.append({"station": "A", "arrive": None, "depart": f"{day}T00:01"})
        stops.append(
            {"station": "S", "arrive": f"{day}T{arrive_s}", "depart": f"{day}T{depart_s}"}
        )
        stops.append({"station": "B", "arrive": f"{day}T23:59", "depart": None})
    return {"id": tid, "stops": stops}


def tt(trains):
    return validate_timetable({"stations": STATIONS, "trains": trains})


def basic_tt():
    """T1 [08:00,08:20) 与 T2 [08:05,08:25) 重叠；T3 [08:20,08:35) 与 T1 端点相接。"""
    return tt([
        make_train("T1", "08:05", "08:15"),
        make_train("T2", "08:10", "08:20"),
        make_train("T3", "08:25", "08:30"),
    ])


def platforms_of(result):
    return [item["platform"] for item in result]


# ---------- 占用区间 ----------

def test_occupation_window_middle_stop():
    timetable = tt([make_train("T1", "08:05", "08:15")])
    (occ,) = collect_station_occupations(timetable, "S")
    assert occ["start"] == datetime(2026, 10, 1, 8, 0)   # 到达前 5 分钟
    assert occ["end"] == datetime(2026, 10, 1, 8, 20)    # 发车后 5 分钟
    assert (occ["train_id"], occ["stop_index"]) == ("T1", 1)


def test_occupation_window_first_and_last_stop():
    timetable = tt([
        make_train("TF", None, "08:00"),   # S 为首站：取发车时间
        make_train("TL", "10:00", None),   # S 为末站：取到达时间
    ])
    occs = collect_station_occupations(timetable, "S")
    by_id = {o["train_id"]: o for o in occs}
    assert by_id["TF"]["start"] == datetime(2026, 10, 1, 7, 55)
    assert by_id["TF"]["end"] == datetime(2026, 10, 1, 8, 5)
    assert by_id["TL"]["start"] == datetime(2026, 10, 1, 9, 55)
    assert by_id["TL"]["end"] == datetime(2026, 10, 1, 10, 5)


def test_occupation_window_crosses_midnight():
    timetable = tt([make_train("TN", "23:58", None)])
    (occ,) = collect_station_occupations(timetable, "S")
    assert occ["start"] == datetime(2026, 10, 1, 23, 53)
    assert occ["end"] == datetime(2026, 10, 2, 0, 3)  # 跨午夜按实际日期


# ---------- 分配规则 ----------

def test_touching_intervals_share_platform():
    # T1 占用 [08:00,08:20)，T3 占用 [08:20,08:35)：端点相接可共用站台
    result = plan_platforms(basic_tt(), "S", 2)
    assert platforms_of(result) == [1, 2, 1]


def test_single_platform_infeasible_when_overlapping():
    assert plan_platforms(basic_tt(), "S", 1) is None


def test_lexicographically_smallest_platform_sequence():
    # 两个互不重叠的停靠：都分 1 号站台（[1,1] 字典序最小）
    timetable = tt([
        make_train("T1", "08:05", "08:15"),
        make_train("T2", "09:05", "09:15"),
    ])
    assert platforms_of(plan_platforms(timetable, "S", 2)) == [1, 1]
    # 三个互不重叠的停靠、4 个站台：仍然全 1 号
    timetable = tt([
        make_train("T1", "08:05", "08:15"),
        make_train("T2", "09:05", "09:15"),
        make_train("T3", "10:05", "10:15"),
    ])
    assert platforms_of(plan_platforms(timetable, "S", 4)) == [1, 1, 1]


def test_result_sorted_by_start_then_train_then_index():
    # 同一时刻两个班次到达：开始时间相同，按班次 ID 排序
    timetable = tt([
        make_train("TB", "08:05", "08:15"),
        make_train("TA", "08:05", "08:15"),
    ])
    result = plan_platforms(timetable, "S", 2)
    assert [(r["train_id"], r["stop_index"]) for r in result] == [("TA", 1), ("TB", 1)]
    assert platforms_of(result) == [1, 2]


def test_lock_is_reserved():
    # 预锁 T1 到 2 号站台：T2 与 T1 重叠只能去 1 号；
    # T3 [08:20,08:35) 与 T2 [08:05,08:25) 重叠、与 T1 [08:00,08:20) 端点相接，故也分 2 号
    result = plan_platforms(
        basic_tt(), "S", 2, locks=[{"train_id": "T1", "stop_index": 1, "platform": 2}]
    )
    assert platforms_of(result) == [2, 1, 2]


def test_lock_forces_backtracking():
    # T1、T2 不重叠，但 T1 锁 2 号站台后 T1=2、T2=1
    timetable = tt([
        make_train("T1", "08:05", "08:15"),
        make_train("T2", "09:05", "09:15"),
    ])
    result = plan_platforms(
        timetable, "S", 2, locks=[{"train_id": "T1", "stop_index": 1, "platform": 2}]
    )
    assert platforms_of(result) == [2, 1]


def test_conflicting_locks_infeasible():
    # 两个重叠停靠锁同一站台 → 无解
    assert plan_platforms(
        basic_tt(),
        "S",
        2,
        locks=[
            {"train_id": "T1", "stop_index": 1, "platform": 1},
            {"train_id": "T2", "stop_index": 1, "platform": 1},
        ],
    ) is None


# ---------- 规模上限内的性能（不得回溯爆炸） ----------

def explosive_trains():
    """20 次停靠：前 15 次彼此分离，最后 5 次同时占用（需 5 个站台）。"""
    trains = []
    base = datetime(2026, 10, 1, 8, 0)
    for i in range(15):  # 间隔 30 分钟，占用窗口互不接触
        t0 = base + timedelta(minutes=30 * i)
        trains.append(make_train(
            f"D{i:02d}", t0.strftime("%H:%M"),
            (t0 + timedelta(minutes=5)).strftime("%H:%M")))
    for i in range(5):  # 同一时刻到达，占用完全重叠
        trains.append(make_train(f"X{i:02d}", "20:00", "20:05"))
    return trains


def explosive_tt():
    return tt(explosive_trains())


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_explosive_infeasible_returns_promptly(count):
    # 前段 15 次分离停靠的等价分配不得被逐一枚举：任何 1～4 个站台都立即判无解
    assert plan_platforms(explosive_tt(), "S", count) is None


def test_explosive_infeasible_with_valid_lock():
    # 有效预锁不改变占用区间，同样不得触发回溯爆炸
    assert plan_platforms(
        explosive_tt(), "S", 4,
        locks=[{"train_id": "D03", "stop_index": 1, "platform": 2}],
    ) is None
    # 预锁落在同时占用的停靠上也一样
    assert plan_platforms(
        explosive_tt(), "S", 4,
        locks=[{"train_id": "X02", "stop_index": 1, "platform": 1}],
    ) is None


def test_lock_blocked_tail_returns_promptly():
    # 团数未超站台数（预检放行），但预锁链挡住长尾 → 无解只能靠搜索发现，
    # 前 15 次分离停靠的等价分配同样只展开一次
    trains = []
    base = datetime(2026, 10, 1, 8, 0)
    for i in range(15):
        t0 = base + timedelta(minutes=30 * i)
        trains.append(make_train(
            f"D{i:02d}", t0.strftime("%H:%M"),
            (t0 + timedelta(minutes=5)).strftime("%H:%M")))
    # 预锁链：占用 [20:00,20:10)…[20:30,20:40)，依次锁 1～4 号站台
    for p in (1, 2, 3, 4):
        moment = f"20:{5 + 10 * (p - 1):02d}"
        trains.append(make_train(f"L{p}", moment, moment))
    trains.append(make_train("B1", "20:10", "20:30"))  # 长尾与四条链全部重叠
    locks = [{"train_id": f"L{p}", "stop_index": 1, "platform": p} for p in (1, 2, 3, 4)]
    assert plan_platforms(tt(trains), "S", 4, locks=locks) is None
    # 去掉长尾则有解：预锁保留，前段字典序最小（全 1 号站台）
    result = plan_platforms(tt(trains[:-1]), "S", 4, locks=locks)
    assert platforms_of(result) == [1] * 15 + [1, 2, 3, 4]


def test_twenty_separated_stops_feasible_promptly():
    # 20 次彼此分离的停靠、4 个站台：全部 1 号站台，迅速返回
    trains = []
    base = datetime(2026, 10, 1, 8, 0)
    for i in range(20):
        t0 = base + timedelta(minutes=30 * i)
        trains.append(make_train(
            f"T{i:02d}", t0.strftime("%H:%M"),
            (t0 + timedelta(minutes=5)).strftime("%H:%M")))
    result = plan_platforms(tt(trains), "S", 4)
    assert platforms_of(result) == [1] * 20


# ---------- 输入校验 ----------

def test_unknown_station_rejected():
    with pytest.raises(TimetableError, match="不存在"):
        plan_platforms(basic_tt(), "NOPE", 2)


@pytest.mark.parametrize("count", [0, 5, -1, "2", 2.5, True, None])
def test_bad_platform_count_rejected(count):
    with pytest.raises(TimetableError, match="站台数量"):
        plan_platforms(basic_tt(), "S", count)


def test_too_many_stops_rejected():
    base = datetime(2026, 10, 1, 8, 0)
    fmt = "%Y-%m-%dT%H:%M"
    trains = []
    for i in range(21):  # 21 次停靠 > 20 次上限
        t0 = base + timedelta(minutes=30 * i)
        trains.append({"id": f"T{i:02d}", "stops": [
            {"station": "A", "arrive": None, "depart": t0.strftime(fmt)},
            {"station": "S",
             "arrive": (t0 + timedelta(minutes=10)).strftime(fmt),
             "depart": (t0 + timedelta(minutes=15)).strftime(fmt)},
            {"station": "B", "arrive": (t0 + timedelta(minutes=25)).strftime(fmt),
             "depart": None},
        ]})
    with pytest.raises(TimetableError, match="超过 20 次"):
        plan_platforms(tt(trains), "S", 4)


def test_lock_unknown_train():
    with pytest.raises(TimetableError, match="班次 .* 不存在"):
        plan_platforms(basic_tt(), "S", 2,
                       locks=[{"train_id": "NOPE", "stop_index": 0, "platform": 1}])


def test_lock_stop_index_out_of_range():
    with pytest.raises(TimetableError, match="停靠序号"):
        plan_platforms(basic_tt(), "S", 2,
                       locks=[{"train_id": "T1", "stop_index": 3, "platform": 1}])
    with pytest.raises(TimetableError, match="停靠序号"):
        plan_platforms(basic_tt(), "S", 2,
                       locks=[{"train_id": "T1", "stop_index": -1, "platform": 1}])


def test_lock_platform_out_of_range():
    with pytest.raises(TimetableError, match="站台编号"):
        plan_platforms(basic_tt(), "S", 2,
                       locks=[{"train_id": "T1", "stop_index": 1, "platform": 3}])
    with pytest.raises(TimetableError, match="站台编号"):
        plan_platforms(basic_tt(), "S", 2,
                       locks=[{"train_id": "T1", "stop_index": 1, "platform": 0}])


def test_lock_stop_not_at_station():
    # T1 的第 0 次停靠在 A 站，不在 S 站
    with pytest.raises(TimetableError, match="不在站点"):
        plan_platforms(basic_tt(), "S", 2,
                       locks=[{"train_id": "T1", "stop_index": 0, "platform": 1}])


def test_lock_duplicated():
    with pytest.raises(TimetableError, match="重复预锁"):
        plan_platforms(basic_tt(), "S", 2, locks=[
            {"train_id": "T1", "stop_index": 1, "platform": 1},
            {"train_id": "T1", "stop_index": 1, "platform": 2},
        ])


def test_locks_must_be_list():
    with pytest.raises(TimetableError, match="locks 必须是数组"):
        plan_platforms(basic_tt(), "S", 2, locks={"train_id": "T1"})


# ---------- API ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SAMPLE_TIMETABLE", "")
    application = create_app()
    application.config["TESTING"] = True
    with application.test_client() as client:
        yield client


def import_basic(client):
    payload = validate_payload()
    assert client.post("/api/import", json=payload).get_json()["ok"]


def validate_payload():
    return {
        "stations": STATIONS,
        "trains": [
            {"id": "T1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T00:01"},
                {"station": "S", "arrive": "2026-10-01T08:05", "depart": "2026-10-01T08:15"},
                {"station": "B", "arrive": "2026-10-01T23:59", "depart": None},
            ]},
            {"id": "T2", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T00:01"},
                {"station": "S", "arrive": "2026-10-01T08:10", "depart": "2026-10-01T08:20"},
                {"station": "B", "arrive": "2026-10-01T23:59", "depart": None},
            ]},
        ],
    }


def test_api_platform_plan_ok(client):
    import_basic(client)
    res = client.post("/api/platform-plan", json={"station_id": "S", "platform_count": 2})
    data = res.get_json()
    assert res.status_code == 200
    assert data["ok"] and data["feasible"]
    assert data["station"] == "S" and data["platform_count"] == 2
    assert [(a["train_id"], a["stop_index"], a["platform"]) for a in data["assignments"]] == [
        ("T1", 1, 1), ("T2", 1, 2),
    ]
    assert data["assignments"][0]["start"] == "2026-10-01T08:00"
    assert data["assignments"][0]["end"] == "2026-10-01T08:20"
    assert data["assignments"][1]["start"] == "2026-10-01T08:05"
    assert data["assignments"][1]["end"] == "2026-10-01T08:25"


def test_api_platform_plan_with_lock(client):
    import_basic(client)
    res = client.post("/api/platform-plan", json={
        "station_id": "S",
        "platform_count": 2,
        "locks": [{"train_id": "T1", "stop_index": 1, "platform": 2}],
    })
    data = res.get_json()
    assert data["ok"] and data["feasible"]
    assert [a["platform"] for a in data["assignments"]] == [2, 1]


def test_api_platform_plan_infeasible(client):
    import_basic(client)
    res = client.post("/api/platform-plan", json={"station_id": "S", "platform_count": 1})
    data = res.get_json()
    assert res.status_code == 200
    assert data["ok"] and not data["feasible"]
    assert "无解" in data["message"]


def test_api_platform_plan_explosive_infeasible(client):
    # 20 次停靠（前 15 分离 + 后 5 同时占用）、4 个站台：须快速返回明确无解提示
    assert client.post("/api/import", json={
        "stations": STATIONS, "trains": explosive_trains(),
    }).get_json()["ok"]
    res = client.post("/api/platform-plan", json={"station_id": "S", "platform_count": 4})
    data = res.get_json()
    assert res.status_code == 200
    assert data["ok"] and not data["feasible"]
    assert "无解" in data["message"]


def test_api_platform_plan_invalid_lock(client):
    import_basic(client)
    res = client.post("/api/platform-plan", json={
        "station_id": "S",
        "platform_count": 2,
        "locks": [{"train_id": "T1", "stop_index": 9, "platform": 1}],
    })
    assert res.status_code == 400
    assert not res.get_json()["ok"]


def test_api_platform_plan_validation(client):
    import_basic(client)
    # 站点不存在
    assert client.post("/api/platform-plan", json={
        "station_id": "ZZZ", "platform_count": 2,
    }).status_code == 400
    # 站台数量越界
    assert client.post("/api/platform-plan", json={
        "station_id": "S", "platform_count": 5,
    }).status_code == 400
    # 非 JSON 请求体
    res = client.post("/api/platform-plan", data="x", content_type="text/plain")
    assert res.status_code == 400


def test_api_platform_plan_without_timetable(client):
    res = client.post("/api/platform-plan", json={"station_id": "S", "platform_count": 2})
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]
