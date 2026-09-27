"""晚点来源追踪（延误归因）：根源识别、传导链回溯、并列保留、输入校验与 API。"""
from datetime import datetime

import pytest

from app import create_app
from planner import (
    TimetableError,
    attribute_delay_propagation,
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


def attribute(timetable, ids, delays=None, clearance=0):
    return attribute_delay_propagation(timetable, ids, delays, clearance)


def entry(result, tid):
    for item in result["trains"]:
        if item["train_id"] == tid:
            return item
    raise AssertionError(f"结果中没有班次 {tid}")


def roots_of(result, tid):
    return entry(result, tid)["roots"]


def chain_steps(root):
    """传导链的 (班次, 停靠序号, 事件, 约束, 时刻) 序列。"""
    return [
        (s["train_id"], s["stop_index"], s["event"], s["constraint"], s["time"])
        for s in root["chain"]
    ]


# ---------- 首发延误根源 ----------

def test_initial_delay_root_and_chain():
    # 单班首发延误 15 分钟：根源即本班首发延误，链沿 运行→停留→运行 到末到
    timetable = tt([train("T1", [
        ("A", None, hm("08:00")),
        ("B", hm("08:30"), hm("08:35")),
        ("C", hm("09:10"), None),
    ])])
    result = attribute(timetable, ["T1"], {"T1": 15}, 10)
    item = entry(result, "T1")
    assert item["final_delay_minutes"] == 15
    assert item["original_final_arrive"] == dt("09:10")
    assert item["final_arrive"] == dt("09:25")
    assert len(item["roots"]) == 1
    root = item["roots"][0]
    assert root["type"] == "initial_delay"
    assert root["train_id"] == "T1"
    assert root["stop_index"] == 0
    assert root["station"] == "A"
    assert root["delay_minutes"] == 15
    assert root["original_depart"] == dt("08:00")
    assert root["adjusted_depart"] == dt("08:15")
    assert chain_steps(root) == [
        ("T1", 0, "depart", "initial_delay", dt("08:15")),
        ("T1", 1, "arrive", "run", dt("08:45")),
        ("T1", 1, "depart", "dwell", dt("08:50")),
        ("T1", 2, "arrive", "run", dt("09:25")),
    ]
    # 链每步都标明站点
    assert [s["station"] for s in root["chain"]] == ["A", "B", "B", "C"]


def test_no_delay_empty_roots():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:50")), ("B", hm("09:20"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], None, 10)
    assert [item["final_delay_minutes"] for item in result["trains"]] == [0, 0]
    assert all(item["roots"] == [] for item in result["trains"])


def test_unaffected_train_has_empty_roots():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:40")), ("B", hm("09:10"), None)]),
        train("T3", [("C", None, hm("08:10")), ("B", hm("08:40"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2", "T3"], {"T1": 5}, 10)
    assert entry(result, "T3")["final_delay_minutes"] == 0
    assert roots_of(result, "T3") == []
    assert [r["type"] for r in roots_of(result, "T2")] == ["initial_delay"]


def test_ineffective_initial_delay_not_root():
    # T2 自己填了 5 分钟首发延误，但区间清空约束更紧（支配），只有 T1 的延误起作用
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:20")), ("B", hm("08:50"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], {"T1": 20, "T2": 5}, 10)
    item = entry(result, "T2")
    assert item["final_delay_minutes"] == 40  # 09:00 发、09:30 到
    assert [(r["type"], r["train_id"]) for r in item["roots"]] == [
        ("initial_delay", "T1"),
    ]


# ---------- 区间冲突根源（原时刻表本就违反清空间隔） ----------

def test_section_conflict_root_original_violation():
    # 无首发延误：T2 原定 08:35 发，前车 T1 08:30 才到，清空 10 分钟本就违反
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:35")), ("B", hm("09:05"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], None, 10)
    assert roots_of(result, "T1") == []
    item = entry(result, "T2")
    assert item["final_delay_minutes"] == 5
    assert len(item["roots"]) == 1
    root = item["roots"][0]
    assert root["type"] == "section_conflict"
    assert root["section"] == ["A", "B"]
    assert root["clearance_minutes"] == 10
    assert root["shortfall_minutes"] == 5  # 08:30 + 10 - 08:35
    assert root["ahead"] == {
        "train_id": "T1", "stop_index": 0, "from": "A", "to": "B",
        "depart": dt("08:00"), "arrive": dt("08:30"),
    }
    assert root["follow"] == {
        "train_id": "T2", "stop_index": 0, "from": "A", "to": "B",
        "original_depart": dt("08:35"), "depart": dt("08:40"),
    }
    assert chain_steps(root) == [
        ("T1", 0, "depart", "section_conflict", dt("08:00")),
        ("T1", 1, "arrive", "run", dt("08:30")),
        ("T2", 0, "depart", "clearance", dt("08:40")),
        ("T2", 1, "arrive", "run", dt("09:10")),
    ]


def test_exact_clearance_no_conflict_root():
    # 恰好满足清空间隔不算违反，不产生晚点与根源
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:40")), ("B", hm("09:10"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], None, 10)
    assert all(item["roots"] == [] for item in result["trains"])


def test_same_train_repeated_passages_conflict_root():
    # 往返班次 T1 的第二次通过（正点）与 T2 形成原时刻表区间冲突
    timetable = tt([
        train("T1", [
            ("A", None, hm("08:00")),
            ("B", hm("08:30"), hm("08:35")),
            ("A", hm("09:05"), None),
        ]),
        train("T2", [("A", None, hm("08:50")), ("B", hm("09:20"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], None, 30)
    assert roots_of(result, "T1") == []
    item = entry(result, "T2")
    assert item["final_delay_minutes"] == 45
    assert len(item["roots"]) == 1
    root = item["roots"][0]
    assert root["type"] == "section_conflict"
    assert root["ahead"]["train_id"] == "T1"
    assert root["ahead"]["stop_index"] == 1  # T1 的第二次通过（B→A）
    assert root["follow"]["train_id"] == "T2"
    assert root["shortfall_minutes"] == 45  # 09:05 + 30 - 08:50
    assert chain_steps(root) == [
        ("T1", 1, "depart", "section_conflict", dt("08:35")),
        ("T1", 2, "arrive", "run", dt("09:05")),
        ("T2", 0, "depart", "clearance", dt("09:35")),
        ("T2", 1, "arrive", "run", dt("10:05")),
    ]


# ---------- 跨班次传导链 ----------

def test_clearance_chain_reaches_initial_delay():
    # T1 首发延误经区间清空约束传给 T2：T2 的根源是 T1 的首发延误（不是区间冲突）
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:40")), ("B", hm("09:10"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], {"T1": 15}, 10)
    item = entry(result, "T2")
    assert item["final_delay_minutes"] == 15
    assert len(item["roots"]) == 1
    root = item["roots"][0]
    assert root["type"] == "initial_delay"
    assert root["train_id"] == "T1"
    assert chain_steps(root) == [
        ("T1", 0, "depart", "initial_delay", dt("08:15")),
        ("T1", 1, "arrive", "run", dt("08:45")),
        ("T2", 0, "depart", "clearance", dt("08:55")),
        ("T2", 1, "arrive", "run", dt("09:25")),
    ]


def test_chain_across_multiple_trains():
    # 延误沿区间顺序链式传播 T1 → T2 → T3：T3 的链穿过 T2 直达 T1 的首发延误
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("A", hm("09:10"), None)]),
        train("T3", [("A", None, hm("09:20")), ("B", hm("09:50"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2", "T3"], {"T1": 20}, 10)
    item = entry(result, "T3")
    assert item["final_delay_minutes"] == 20
    assert len(item["roots"]) == 1
    root = item["roots"][0]
    assert (root["type"], root["train_id"]) == ("initial_delay", "T1")
    assert chain_steps(root) == [
        ("T1", 0, "depart", "initial_delay", dt("08:20")),
        ("T1", 1, "arrive", "run", dt("08:50")),
        ("T2", 0, "depart", "clearance", dt("09:00")),
        ("T2", 1, "arrive", "run", dt("09:30")),
        ("T3", 0, "depart", "clearance", dt("09:40")),
        ("T3", 1, "arrive", "run", dt("10:10")),
    ]


def test_cross_midnight_chain_full_dates():
    timetable = tt([
        train("T1", [("A", None, hm("23:50")), ("B", hm("00:20", DAY2), None)]),
        train("T2", [("A", None, hm("00:15", DAY2)), ("B", hm("00:45", DAY2), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], {"T1": 20}, 10)
    root = roots_of(result, "T2")[0]
    assert chain_steps(root) == [
        ("T1", 0, "depart", "initial_delay", dt("00:10", DAY2)),
        ("T1", 1, "arrive", "run", dt("00:40", DAY2)),
        ("T2", 0, "depart", "clearance", dt("00:50", DAY2)),
        ("T2", 1, "arrive", "run", dt("01:20", DAY2)),
    ]


def test_middle_stop_dwell_extended_chain():
    # T1 在中间站 B 的停留被区间约束拉长：链经 停留 约束连到 T2 的首发延误
    timetable = tt([
        train("T1", [
            ("A", None, hm("08:00")),
            ("B", hm("08:20"), hm("08:25")),
            ("C", hm("08:45"), None),
        ]),
        train("T2", [("B", None, hm("08:10")), ("C", hm("08:30"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], {"T2": 20}, 10)
    item = entry(result, "T1")
    assert item["final_delay_minutes"] == 35
    root = item["roots"][0]
    assert (root["type"], root["train_id"]) == ("initial_delay", "T2")
    assert chain_steps(root) == [
        ("T2", 0, "depart", "initial_delay", dt("08:30")),
        ("T2", 1, "arrive", "run", dt("08:50")),
        ("T1", 1, "depart", "clearance", dt("09:00")),
        ("T1", 2, "arrive", "run", dt("09:20")),
    ]


# ---------- 并列约束保留全部根源 ----------

def test_tied_initial_delay_and_clearance_keep_all_roots():
    # T2 首发下界（08:20+30）与区间清空下界（T1 到达 08:40+10）恰好同为 08:50
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:20")), ("B", hm("08:50"), None)]),
    ])
    result = attribute(timetable, ["T1", "T2"], {"T1": 10, "T2": 30}, 10)
    item = entry(result, "T2")
    assert item["final_delay_minutes"] == 30
    got = [(r["type"], r["train_id"]) for r in item["roots"]]
    assert sorted(got) == [("initial_delay", "T1"), ("initial_delay", "T2")]
    by_train = {r["train_id"]: r for r in item["roots"]}
    assert chain_steps(by_train["T1"])[0] == (
        "T1", 0, "depart", "initial_delay", dt("08:10"))
    assert chain_steps(by_train["T2"]) == [
        ("T2", 0, "depart", "initial_delay", dt("08:50")),
        ("T2", 1, "arrive", "run", dt("09:20")),
    ]


def test_tied_dwell_and_clearance_keep_all_roots():
    # T2 在中间站 B：停留下界（自身到达 09:25+10）与清空下界（T1 到达 09:25+10）并列
    timetable = tt([
        train("T1", [("B", None, hm("08:25")), ("C", hm("08:55"), None)]),
        train("T2", [
            ("A", None, hm("08:10")),
            ("B", hm("08:40"), hm("08:50")),
            ("C", hm("09:20"), None),
        ]),
    ])
    result = attribute(timetable, ["T1", "T2"], {"T1": 30, "T2": 45}, 10)
    item = entry(result, "T2")
    assert item["final_delay_minutes"] == 45
    got = [(r["type"], r["train_id"]) for r in item["roots"]]
    assert sorted(got) == [("initial_delay", "T1"), ("initial_delay", "T2")]
    by_train = {r["train_id"]: r for r in item["roots"]}
    # 经区间清空回溯到 T1 的首发延误
    assert chain_steps(by_train["T1"]) == [
        ("T1", 0, "depart", "initial_delay", dt("08:55")),
        ("T1", 1, "arrive", "run", dt("09:25")),
        ("T2", 1, "depart", "clearance", dt("09:35")),
        ("T2", 2, "arrive", "run", dt("10:05")),
    ]
    # 经站内停留回溯到 T2 自己的首发延误
    assert chain_steps(by_train["T2"]) == [
        ("T2", 0, "depart", "initial_delay", dt("08:55")),
        ("T2", 1, "arrive", "run", dt("09:25")),
        ("T2", 1, "depart", "dwell", dt("09:35")),
        ("T2", 2, "arrive", "run", dt("10:05")),
    ]


def test_tied_section_conflict_and_initial_delay_keep_all_roots():
    # T2 首发延误 5 分钟的下界与区间冲突（前车正点）的下界恰好相同：两个根源都保留
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("08:35")), ("B", hm("09:05"), None)]),
    ])
    # 清空下界 = 08:30 + 10 = 08:40；T2 首发下界 = 08:35 + 5 = 08:40
    result = attribute(timetable, ["T1", "T2"], {"T2": 5}, 10)
    item = entry(result, "T2")
    assert item["final_delay_minutes"] == 5
    got = sorted(
        (r["type"], r.get("train_id")) for r in item["roots"]
    )
    assert got == [("initial_delay", "T2"), ("section_conflict", None)]


# ---------- 结果结构与输入校验 ----------

def test_result_trains_follow_timetable_order():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("09:00")), ("B", hm("09:30"), None)]),
    ])
    result = attribute(timetable, ["T2", "T1"], None, 0)
    assert [item["train_id"] for item in result["trains"]] == ["T1", "T2"]
    assert result["total_stops"] == 4


def base_timetable():
    return tt([train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)])])


@pytest.mark.parametrize("ids", [[], ["T1", "T1"], ["T1", "ZZZ"], ["T1", ""], ["T1", 1]])
def test_bad_train_ids_rejected(ids):
    with pytest.raises(TimetableError):
        attribute(base_timetable(), ids, None, 0)


@pytest.mark.parametrize("minutes", [-1, 121, 2.5, "10", True])
def test_bad_delay_minutes_rejected(minutes):
    with pytest.raises(TimetableError, match="首发延误"):
        attribute(base_timetable(), ["T1"], {"T1": minutes}, 0)


@pytest.mark.parametrize("clearance", [-1, 31, 2.5, "10", True, None])
def test_bad_clearance_rejected(clearance):
    with pytest.raises(TimetableError, match="清空间隔"):
        attribute(base_timetable(), ["T1"], None, clearance)


def test_delay_for_unselected_train_rejected():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("A", None, hm("09:00")), ("B", hm("09:30"), None)]),
    ])
    with pytest.raises(TimetableError, match="不是所选班次"):
        attribute(timetable, ["T1"], {"T2": 5}, 0)


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


def test_api_delay_attribution_ok(client):
    import_api_sample(client)
    res = client.post("/api/delay-attribution", json={
        "train_ids": ["T2", "T1"],
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
    # T1：自身首发延误 10 分钟，链沿 运行→停留→运行 到末到
    assert t1["final_delay_minutes"] == 10
    assert t1["original_final_arrive"] == "2026-10-01T09:00"
    assert t1["final_arrive"] == "2026-10-01T09:10"
    assert len(t1["roots"]) == 1
    root = t1["roots"][0]
    assert root["type"] == "initial_delay"
    assert root["train_id"] == "T1"
    assert root["delay_minutes"] == 10
    assert root["original_depart"] == "2026-10-01T08:00"
    assert root["adjusted_depart"] == "2026-10-01T08:10"
    assert [
        (s["train_id"], s["stop_index"], s["event"], s["constraint"], s["time"])
        for s in root["chain"]
    ] == [
        ("T1", 0, "depart", "initial_delay", "2026-10-01T08:10"),
        ("T1", 1, "arrive", "run", "2026-10-01T08:40"),
        ("T1", 1, "depart", "dwell", "2026-10-01T08:45"),
        ("T1", 2, "arrive", "run", "2026-10-01T09:10"),
    ]
    assert [s["station"] for s in root["chain"]] == ["A", "B", "B", "C"]

    # T2：末到延误 30 分钟，根源是 T1 的首发延误，链经清空与停留约束传导
    assert t2["final_delay_minutes"] == 30
    assert t2["final_arrive"] == "2026-10-01T09:50"
    assert len(t2["roots"]) == 1
    root = t2["roots"][0]
    assert root["type"] == "initial_delay"
    assert root["train_id"] == "T1"
    assert [
        (s["train_id"], s["stop_index"], s["event"], s["constraint"], s["time"])
        for s in root["chain"]
    ] == [
        ("T1", 0, "depart", "initial_delay", "2026-10-01T08:10"),
        ("T1", 1, "arrive", "run", "2026-10-01T08:40"),
        ("T2", 0, "depart", "clearance", "2026-10-01T08:50"),
        ("T2", 1, "arrive", "run", "2026-10-01T09:20"),
        ("T2", 1, "depart", "dwell", "2026-10-01T09:25"),
        ("T2", 2, "arrive", "run", "2026-10-01T09:50"),
    ]


def test_api_delay_attribution_section_conflict(client):
    import_api_sample(client)
    # 无首发延误：T2 原定 08:20 发于 A，T1 08:30 才到 B——A～B 区间本就违反清空 15
    res = client.post("/api/delay-attribution", json={
        "train_ids": ["T1", "T2"],
        "initial_delays": {},
        "clearance_minutes": 15,
    })
    assert res.status_code == 200
    data = res.get_json()
    t1, t2 = data["trains"]
    assert t1["roots"] == []
    assert t2["final_delay_minutes"] == 25  # 08:45 发、09:45 到
    assert len(t2["roots"]) == 1
    root = t2["roots"][0]
    assert root["type"] == "section_conflict"
    assert root["section"] == ["A", "B"]
    assert root["clearance_minutes"] == 15
    assert root["shortfall_minutes"] == 25  # 08:30 + 15 - 08:20
    assert root["ahead"] == {
        "train_id": "T1", "stop_index": 0, "from": "A", "to": "B",
        "depart": "2026-10-01T08:00", "arrive": "2026-10-01T08:30",
    }
    assert root["follow"] == {
        "train_id": "T2", "stop_index": 0, "from": "A", "to": "B",
        "original_depart": "2026-10-01T08:20", "depart": "2026-10-01T08:45",
    }
    assert [
        (s["train_id"], s["stop_index"], s["event"], s["constraint"], s["time"])
        for s in root["chain"]
    ] == [
        ("T1", 0, "depart", "section_conflict", "2026-10-01T08:00"),
        ("T1", 1, "arrive", "run", "2026-10-01T08:30"),
        ("T2", 0, "depart", "clearance", "2026-10-01T08:45"),
        ("T2", 1, "arrive", "run", "2026-10-01T09:15"),
        ("T2", 1, "depart", "dwell", "2026-10-01T09:20"),
        ("T2", 2, "arrive", "run", "2026-10-01T09:45"),
    ]


def test_api_delay_attribution_without_timetable(client):
    res = client.post("/api/delay-attribution", json={
        "train_ids": ["T1"], "initial_delays": {}, "clearance_minutes": 0,
    })
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]


def test_api_delay_attribution_validation(client):
    import_api_sample(client)
    # 非 JSON 请求体
    res = client.post("/api/delay-attribution", data="x", content_type="text/plain")
    assert res.status_code == 400
    # 班次数量越界 / 班次不存在 / 重复
    assert client.post("/api/delay-attribution", json={
        "train_ids": [], "clearance_minutes": 0,
    }).status_code == 400
    assert client.post("/api/delay-attribution", json={
        "train_ids": ["T1", "ZZZ"], "clearance_minutes": 0,
    }).status_code == 400
    assert client.post("/api/delay-attribution", json={
        "train_ids": ["T1", "T1"], "clearance_minutes": 0,
    }).status_code == 400
    # 首发延误越界 / 类型错误 / 班次未选择
    for bad_delays in ({"T1": -1}, {"T1": 121}, {"T1": "10"}, {"T2": 5}, [1]):
        res = client.post("/api/delay-attribution", json={
            "train_ids": ["T1"], "initial_delays": bad_delays,
            "clearance_minutes": 0,
        })
        assert res.status_code == 400
    # 清空间隔越界 / 类型错误
    for bad_clearance in (-1, 31, "5", 2.5, True, None):
        res = client.post("/api/delay-attribution", json={
            "train_ids": ["T1"], "clearance_minutes": bad_clearance,
        })
        assert res.status_code == 400


def test_api_delay_attribution_does_not_modify_timetable(client):
    import_api_sample(client)
    before = client.get("/api/timetable").get_json()
    versions_before = client.get("/api/timetable-versions").get_json()
    res = client.post("/api/delay-attribution", json={
        "train_ids": ["T1", "T2"],
        "initial_delays": {"T1": 30},
        "clearance_minutes": 15,
    })
    assert res.get_json()["ok"]
    assert client.get("/api/timetable").get_json() == before
    assert client.get("/api/timetable-versions").get_json() == versions_before
