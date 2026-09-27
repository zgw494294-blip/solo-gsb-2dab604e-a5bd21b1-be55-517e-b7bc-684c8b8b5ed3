"""区间延误传播排程：首发延误传播、区间清空约束、逐项最早性、输入校验与 API。"""
from datetime import datetime, timedelta

import pytest

from app import create_app
from planner import (
    TimetableError,
    plan_delay_propagation,
    validate_timetable,
)

DAY = "2026-10-01"
DAY2 = "2026-10-02"

STATIONS = [
    {"id": "A", "transfer_minutes": 0},
    {"id": "B", "transfer_minutes": 0},
    {"id": "C", "transfer_minutes": 0},
]


def hm(value, day=DAY):
    return f"{day}T{value}"


def dt(value, day=DAY):
    return datetime.strptime(f"{day}T{value}", "%Y-%m-%dT%H:%M")


def train(tid, stops_spec):
    """stops_spec：[(站点, 到达或 None, 发车或 None), ...]，时间为完整 ISO 字符串或 None。"""
    return {"id": tid, "stops": [
        {"station": sid, "arrive": arrive, "depart": depart}
        for sid, arrive, depart in stops_spec
    ]}


def tt(trains):
    return validate_timetable({"stations": STATIONS, "trains": trains})


def plan(timetable, ids, delays=None, clearance=0):
    return plan_delay_propagation(timetable, ids, delays, clearance)


def adjusted(result, tid):
    """结果中指定班次的 [(到达, 发车), ...]（datetime 或 None）。"""
    for item in result["trains"]:
        if item["train_id"] == tid:
            return [(s["arrive"], s["depart"]) for s in item["stops"]]
    raise AssertionError(f"结果中没有班次 {tid}")


def final_delay(result, tid):
    for item in result["trains"]:
        if item["train_id"] == tid:
            return item["final_delay_minutes"]
    raise AssertionError(f"结果中没有班次 {tid}")


# ---------- 班内传播 ----------

def test_single_train_uniform_shift():
    # 首发延误 15 分钟：运行时长不变、停留不变，整班到发整体后移 15 分钟
    timetable = tt([train("T1", [
        ("A", None, hm("08:00")),
        ("B", hm("08:30"), hm("08:35")),
        ("C", hm("09:10"), None),
    ])])
    result = plan(timetable, ["T1"], {"T1": 15}, 10)
    assert adjusted(result, "T1") == [
        (None, dt("08:15")),
        (dt("08:45"), dt("08:50")),
        (dt("09:25"), None),
    ]
    assert final_delay(result, "T1") == 15
    assert result["trains"][0]["initial_delay_minutes"] == 15
    assert result["total_stops"] == 3


def test_no_delay_keeps_original_times():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:50")), ("B", hm("09:20"), None)]),
    ])
    result = plan(timetable, ["T1", "T2"], None, 10)  # 间隔 20 分钟 ≥ 清空 10 分钟
    assert adjusted(result, "T1") == [(None, dt("08:00")), (dt("08:30"), None)]
    assert adjusted(result, "T2") == [(None, dt("08:50")), (dt("09:20"), None)]
    assert final_delay(result, "T1") == 0
    assert final_delay(result, "T2") == 0
    # 未提供 initial_delays 时按 0 处理
    assert all(item["initial_delay_minutes"] == 0 for item in result["trains"])


def test_unlisted_train_defaults_to_zero_delay():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("C", None, hm("08:00")), ("B", hm("08:30"), None)]),
    ])
    result = plan(timetable, ["T1", "T2"], {"T1": 5}, 0)
    assert final_delay(result, "T1") == 5
    assert final_delay(result, "T2") == 0  # 未列入 initial_delays，按 0
    assert result["trains"][1]["initial_delay_minutes"] == 0


def test_result_trains_follow_timetable_order():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("09:00")), ("B", hm("09:30"), None)]),
    ])
    result = plan(timetable, ["T2", "T1"], None, 0)
    assert [item["train_id"] for item in result["trains"]] == ["T1", "T2"]


# ---------- 区间清空约束 ----------

def test_section_clearance_pushes_following_train():
    # T1 延误 15 分钟：08:15 发、08:45 到；T2 须等前车到达 + 10 分钟清空
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:40")), ("B", hm("09:10"), None)]),
    ])
    result = plan(timetable, ["T1", "T2"], {"T1": 15}, 10)
    assert adjusted(result, "T1") == [(None, dt("08:15")), (dt("08:45"), None)]
    assert adjusted(result, "T2") == [(None, dt("08:55")), (dt("09:25"), None)]
    assert final_delay(result, "T2") == 15


def test_section_order_fixed_by_original_times():
    # T2 首发延误 60 分钟也不会插到 T1 之前：顺序按原定发车时间固定
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:40")), ("B", hm("09:10"), None)]),
    ])
    result = plan(timetable, ["T1", "T2"], {"T2": 60}, 10)
    assert adjusted(result, "T1") == [(None, dt("08:00")), (dt("08:30"), None)]
    # T2 只受自身首发延误约束（09:40 已满足 T1 到达 08:30 + 10 分钟）
    assert adjusted(result, "T2") == [(None, dt("09:40")), (dt("10:10"), None)]
    assert final_delay(result, "T1") == 0
    assert final_delay(result, "T2") == 60


def test_opposite_directions_share_section():
    # 无向站对：B→A 与 A→B 是同一单线区间
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:35")), ("A", hm("09:05"), None)]),
    ])
    result = plan(timetable, ["T1", "T2"], None, 10)
    # T2 原定 08:35 发，但须等 T1 08:30 到达 + 10 分钟清空
    assert adjusted(result, "T2") == [(None, dt("08:40")), (dt("09:10"), None)]
    assert final_delay(result, "T2") == 5


def test_same_train_repeated_passages_not_self_constrained():
    # 往返班次 T1 三次通过 A～B 区间；同一班次的连续通过不自我约束
    timetable = tt([
        train("T1", [
            ("A", None, hm("08:00")),
            ("B", hm("08:30"), hm("08:35")),
            ("A", hm("09:05"), None),
        ]),
        train("T2", [("A", None, hm("08:50")), ("B", hm("09:20"), None)]),
    ])
    result = plan(timetable, ["T1", "T2"], None, 30)
    # T1 第二次通过（08:35 发）不因自己第一次通过（08:30 到）而推迟
    assert adjusted(result, "T1") == [
        (None, dt("08:00")),
        (dt("08:30"), dt("08:35")),
        (dt("09:05"), None),
    ]
    # T2 顺序上紧跟 T1 的第二次通过：须等 09:05 + 30 分钟
    assert adjusted(result, "T2") == [(None, dt("09:35")), (dt("10:05"), None)]
    assert final_delay(result, "T2") == 45


def test_exact_clearance_allows_touching():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:40")), ("B", hm("09:10"), None)]),
    ])
    # 恰好满足 10 分钟清空（08:30 + 10 = 08:40），不推迟
    result = plan(timetable, ["T1", "T2"], None, 10)
    assert final_delay(result, "T2") == 0
    # 清空为 0 时端点相接即可
    timetable0 = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:30")), ("B", hm("09:00"), None)]),
    ])
    result0 = plan(timetable0, ["T1", "T2"], None, 0)
    assert final_delay(result0, "T2") == 0


def test_section_constraint_extends_middle_stop_dwell():
    # T2 延误后占用 B～C 区间，T1 在中间站 B 的停留被拉长（不少于原停留即可）
    timetable = tt([
        train("T1", [
            ("A", None, hm("08:00")),
            ("B", hm("08:20"), hm("08:25")),
            ("C", hm("08:45"), None),
        ]),
        train("T2", [("B", None, hm("08:10")), ("C", hm("08:30"), None)]),
    ])
    result = plan(timetable, ["T1", "T2"], {"T2": 20}, 10)
    # T2：08:30 发、08:50 到；T1 在 B 的发车须 ≥ 08:50 + 10 = 09:00
    assert adjusted(result, "T2") == [(None, dt("08:30")), (dt("08:50"), None)]
    assert adjusted(result, "T1") == [
        (None, dt("08:00")),
        (dt("08:20"), dt("09:00")),   # 停留由 5 分钟拉长到 40 分钟
        (dt("09:20"), None),
    ]
    assert final_delay(result, "T1") == 35


def test_delay_chains_across_passages():
    # 延误沿区间顺序链式传播：T1 → T2 → T3
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("A", hm("09:10"), None)]),
        train("T3", [("A", None, hm("09:20")), ("B", hm("09:50"), None)]),
    ])
    result = plan(timetable, ["T1", "T2", "T3"], {"T1": 20}, 10)
    # T1：08:20 发、08:50 到；T2 ≥ 09:00 发、09:30 到；T3 ≥ 09:40 发、10:10 到
    assert adjusted(result, "T1") == [(None, dt("08:20")), (dt("08:50"), None)]
    assert adjusted(result, "T2") == [(None, dt("09:00")), (dt("09:30"), None)]
    assert adjusted(result, "T3") == [(None, dt("09:40")), (dt("10:10"), None)]
    assert [final_delay(result, t) for t in ("T1", "T2", "T3")] == [20, 20, 20]


def test_cross_midnight_full_dates():
    timetable = tt([
        train("T1", [("A", None, hm("23:50")), ("B", hm("00:20", DAY2), None)]),
        train("T2", [("A", None, hm("00:15", DAY2)), ("B", hm("00:45", DAY2), None)]),
    ])
    result = plan(timetable, ["T1", "T2"], {"T1": 20}, 10)
    # T1：10-02 00:10 发、00:40 到；T2 须等 00:40 + 10 分钟
    assert adjusted(result, "T1") == [
        (None, dt("00:10", DAY2)), (dt("00:40", DAY2), None),
    ]
    assert adjusted(result, "T2") == [
        (None, dt("00:50", DAY2)), (dt("01:20", DAY2), None),
    ]
    assert final_delay(result, "T2") == 35


def test_itemwise_earliest_no_unnecessary_delay():
    # 逐项最早：未受约束的班次与停靠保持原时刻，不额外推迟
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:40")), ("B", hm("09:10"), None)]),
        train("T3", [("C", None, hm("08:10")), ("B", hm("08:40"), None)]),
    ])
    result = plan(timetable, ["T1", "T2", "T3"], {"T1": 5}, 10)
    # T1 延误 5 分钟（08:35 到）；T2 须 ≥ 08:45 发（原 08:40）；T3 与 A～B 区间无关
    assert adjusted(result, "T2") == [(None, dt("08:45")), (dt("09:15"), None)]
    assert adjusted(result, "T3") == [(None, dt("08:10")), (dt("08:40"), None)]
    assert final_delay(result, "T3") == 0


# ---------- 输入校验 ----------

def base_timetable():
    return tt([train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)])])


def test_train_ids_must_be_list():
    with pytest.raises(TimetableError, match="train_ids 必须是数组"):
        plan(base_timetable(), "T1", None, 0)


@pytest.mark.parametrize("ids", [[], ["T1", "T1"], ["T1", "ZZZ"], ["T1", ""], ["T1", 1]])
def test_bad_train_ids_rejected(ids):
    with pytest.raises(TimetableError):
        plan(base_timetable(), ids, None, 0)


def test_too_many_trains_rejected():
    ids = [f"T{i:02d}" for i in range(41)]
    timetable = tt([
        train(tid, [("A", None, hm("08:00")), ("B", hm("08:30"), None)]) for tid in ids
    ])
    with pytest.raises(TimetableError, match="1～40"):
        plan(timetable, ids, None, 0)


def test_initial_delays_must_be_dict():
    with pytest.raises(TimetableError, match="initial_delays"):
        plan(base_timetable(), ["T1"], [("T1", 5)], 0)


@pytest.mark.parametrize("minutes", [-1, 121, 2.5, "10", True])
def test_bad_delay_minutes_rejected(minutes):
    with pytest.raises(TimetableError, match="首发延误"):
        plan(base_timetable(), ["T1"], {"T1": minutes}, 0)


def test_delay_for_unselected_train_rejected():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("09:00")), ("B", hm("09:30"), None)]),
    ])
    with pytest.raises(TimetableError, match="不是所选班次"):
        plan(timetable, ["T1"], {"T2": 5}, 0)
    with pytest.raises(TimetableError, match="不是所选班次"):
        plan(timetable, ["T1"], {"ZZZ": 5}, 0)


@pytest.mark.parametrize("clearance", [-1, 31, 2.5, "10", True, None])
def test_bad_clearance_rejected(clearance):
    with pytest.raises(TimetableError, match="清空间隔"):
        plan(base_timetable(), ["T1"], None, clearance)


def test_total_stops_limit():
    # 40 班 × 6 停靠 = 240 > 200 上限
    ids = [f"T{i:02d}" for i in range(40)]
    timetable = tt([
        train(tid, [
            ("A", None, hm("08:00")),
            ("B", hm("08:20"), hm("08:25")),
            ("C", hm("08:45"), hm("08:50")),
            ("A", hm("09:10"), hm("09:15")),
            ("B", hm("09:35"), hm("09:40")),
            ("C", hm("10:00"), None),
        ]) for tid in ids
    ])
    with pytest.raises(TimetableError, match="超过 200"):
        plan(timetable, ids, None, 0)


def test_exactly_200_stops_allowed():
    # 40 班 × 5 停靠 = 200，恰好达到上限；各班错开 2 小时，互不干扰
    ids = [f"T{i:02d}" for i in range(40)]
    base = datetime(2026, 10, 1, 8, 0)
    trains = []
    for i, tid in enumerate(ids):
        t0 = base + timedelta(hours=2 * i)

        def at(minutes, t0=t0):
            return (t0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")

        trains.append(train(tid, [
            ("A", None, at(0)),
            ("B", at(20), at(25)),
            ("C", at(45), at(50)),
            ("A", at(70), at(75)),
            ("B", at(95), None),
        ]))
    result = plan(tt(trains), ids, None, 0)
    assert result["total_stops"] == 200
    assert len(result["trains"]) == 40
    assert all(item["final_delay_minutes"] == 0 for item in result["trains"])


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
    # T1：A 08:00 → B 08:30/08:35 → C 09:00；T2：A 08:20 → B 08:50/08:55 → C 09:20
    return {
        "stations": STATIONS,
        "trains": [
            train("T1", [
                ("A", None, hm("08:00")),
                ("B", hm("08:30"), hm("08:35")),
                ("C", hm("09:00"), None),
            ]),
            train("T2", [
                ("A", None, hm("08:20")),
                ("B", hm("08:50"), hm("08:55")),
                ("C", hm("09:20"), None),
            ]),
        ],
    }


def import_api_sample(client):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]


def test_api_delay_plan_ok(client):
    import_api_sample(client)
    res = client.post("/api/delay-plan", json={
        "train_ids": ["T2", "T1"],  # 顺序无关：结果按时刻表顺序
        "initial_delays": {"T1": 10},
        "clearance_minutes": 10,
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"]
    assert data["clearance_minutes"] == 10
    assert data["train_count"] == 2
    assert data["total_stops"] == 6
    assert [t["train_id"] for t in data["trains"]] == ["T1", "T2"]

    t1, t2 = data["trains"]
    assert t1["initial_delay_minutes"] == 10
    assert t1["final_delay_minutes"] == 10
    assert t1["stops"] == [
        {"station": "A", "arrive": None, "depart": "2026-10-01T08:10",
         "original_arrive": None, "original_depart": "2026-10-01T08:00"},
        {"station": "B", "arrive": "2026-10-01T08:40", "depart": "2026-10-01T08:45",
         "original_arrive": "2026-10-01T08:30", "original_depart": "2026-10-01T08:35"},
        {"station": "C", "arrive": "2026-10-01T09:10", "depart": None,
         "original_arrive": "2026-10-01T09:00", "original_depart": None},
    ]
    # T2 被 T1 带动：A～B 区间等 08:40+10，B 站停留被拉长，末到延误 30 分钟
    assert t2["initial_delay_minutes"] == 0
    assert t2["final_delay_minutes"] == 30
    assert [(s["arrive"], s["depart"]) for s in t2["stops"]] == [
        (None, "2026-10-01T08:50"),
        ("2026-10-01T09:20", "2026-10-01T09:25"),
        ("2026-10-01T09:50", None),
    ]


def test_api_delay_plan_without_timetable(client):
    res = client.post("/api/delay-plan", json={
        "train_ids": ["T1"], "initial_delays": {}, "clearance_minutes": 0,
    })
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]


def test_api_delay_plan_validation(client):
    import_api_sample(client)
    # 非 JSON 请求体
    res = client.post("/api/delay-plan", data="x", content_type="text/plain")
    assert res.status_code == 400
    # 班次数量越界 / 班次不存在 / 重复
    assert client.post("/api/delay-plan", json={
        "train_ids": [], "clearance_minutes": 0,
    }).status_code == 400
    assert client.post("/api/delay-plan", json={
        "train_ids": ["T1", "ZZZ"], "clearance_minutes": 0,
    }).status_code == 400
    assert client.post("/api/delay-plan", json={
        "train_ids": ["T1", "T1"], "clearance_minutes": 0,
    }).status_code == 400
    # 首发延误越界 / 类型错误 / 班次未选择
    for bad_delays in ({"T1": -1}, {"T1": 121}, {"T1": "10"}, {"T2": 5}, [1]):
        res = client.post("/api/delay-plan", json={
            "train_ids": ["T1"], "initial_delays": bad_delays,
            "clearance_minutes": 0,
        })
        assert res.status_code == 400
    # 清空间隔越界 / 类型错误
    for bad_clearance in (-1, 31, "5", 2.5, True, None):
        res = client.post("/api/delay-plan", json={
            "train_ids": ["T1"], "clearance_minutes": bad_clearance,
        })
        assert res.status_code == 400


def test_api_delay_plan_does_not_modify_timetable(client):
    import_api_sample(client)
    before = client.get("/api/timetable").get_json()
    versions_before = client.get("/api/timetable-versions").get_json()
    res = client.post("/api/delay-plan", json={
        "train_ids": ["T1", "T2"],
        "initial_delays": {"T1": 30},
        "clearance_minutes": 15,
    })
    assert res.get_json()["ok"]
    assert client.get("/api/timetable").get_json() == before
    assert client.get("/api/timetable-versions").get_json() == versions_before
