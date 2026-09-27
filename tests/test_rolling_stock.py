"""车底接续规划：接续可行性、最少车底组数、最小等待与 API。"""
import random
import time
from datetime import datetime, timedelta

import pytest

from app import create_app
from planner import (
    TimetableError,
    _min_cost_maximum_matching,
    plan_rolling_stock,
    validate_timetable,
)

STATIONS = [
    {"id": "A", "transfer_minutes": 0},
    {"id": "B", "transfer_minutes": 0},
    {"id": "C", "transfer_minutes": 0},
]


def train(tid, frm, to, depart, arrive, day_from="2026-10-01", day_to=None):
    """构造 frm → to 完整班次（首站发车、末站到达），时间为 "HH:MM"。"""
    day_to = day_to or day_from
    return {"id": tid, "stops": [
        {"station": frm, "arrive": None, "depart": f"{day_from}T{depart}"},
        {"station": to, "arrive": f"{day_to}T{arrive}", "depart": None},
    ]}


def tt(trains):
    return validate_timetable({"stations": STATIONS, "trains": trains})


def chain_ids(result):
    return [[t["train_id"] for t in chain["trains"]] for chain in result["chains"]]


# ---------- 基本规则 ----------

def test_single_train_needs_one_group():
    result = plan_rolling_stock(tt([train("T1", "A", "B", "08:00", "09:00")]), ["T1"], 30)
    assert result["group_count"] == 1
    assert result["total_wait_minutes"] == 0
    assert chain_ids(result) == [["T1"]]
    assert result["chains"][0]["connections"] == []
    t = result["chains"][0]["trains"][0]
    assert (t["origin"], t["destination"]) == ("A", "B")
    assert t["depart"] == datetime(2026, 10, 1, 8, 0)
    assert t["arrive"] == datetime(2026, 10, 1, 9, 0)


def test_continuable_trains_share_one_group():
    # T1 A→B 09:00 到，整备 30 分钟，T2 B→C 10:00 发：可接续
    result = plan_rolling_stock(
        tt([train("T1", "A", "B", "08:00", "09:00"), train("T2", "B", "C", "10:00", "11:00")]),
        ["T1", "T2"], 30,
    )
    assert result["group_count"] == 1
    assert chain_ids(result) == [["T1", "T2"]]
    # 等待 = 后班发车 - 前班到达 = 60 分钟（含 30 分钟整备）
    assert result["total_wait_minutes"] == 60
    (conn,) = result["chains"][0]["connections"]
    assert conn == {"from_train": "T1", "to_train": "T2", "wait_minutes": 60}


def test_turnaround_boundary_is_inclusive():
    timetable = tt([
        train("T1", "A", "B", "08:00", "09:00"),
        train("T2", "B", "C", "09:30", "10:00"),  # 恰好 09:00 + 30
    ])
    assert plan_rolling_stock(timetable, ["T1", "T2"], 30)["group_count"] == 1
    # 整备多 1 分钟则不满足，需 2 组
    assert plan_rolling_stock(timetable, ["T1", "T2"], 31)["group_count"] == 2


def test_turnaround_too_tight_separates_groups():
    result = plan_rolling_stock(
        tt([train("T1", "A", "B", "08:00", "09:00"), train("T2", "B", "C", "09:30", "10:00")]),
        ["T1", "T2"], 40,
    )
    assert result["group_count"] == 2
    assert result["total_wait_minutes"] == 0
    assert sorted(chain_ids(result)) == [["T1"], ["T2"]]


def test_station_mismatch_forbids_deadhead():
    # T1 终到 B，T2 却从 A 发车：不允许空驶，必须另派车底
    result = plan_rolling_stock(
        tt([train("T1", "A", "B", "08:00", "09:00"), train("T2", "A", "C", "10:00", "11:00")]),
        ["T1", "T2"], 30,
    )
    assert result["group_count"] == 2
    assert result["total_wait_minutes"] == 0


def test_dates_are_full_datetimes_across_midnight():
    # N1 10-01 23:30 到 B，N2 10-02 00:05 从 B 发：跨午夜按完整日期可接续
    result = plan_rolling_stock(
        tt([
            train("N1", "A", "B", "23:00", "23:30"),
            train("N2", "B", "A", "00:05", "00:35", day_from="2026-10-02"),
        ]),
        ["N1", "N2"], 30,
    )
    assert result["group_count"] == 1
    assert result["total_wait_minutes"] == 35
    # 同钟点不同日期不算：到达 10-02 00:05、发车 10-01（更早）不可接续
    bad = tt([
        train("E1", "A", "B", "23:00", "23:30"),
        train("E2", "B", "A", "23:40", "23:59"),  # 同一天、整备不足
    ])
    assert plan_rolling_stock(bad, ["E1", "E2"], 30)["group_count"] == 2


def test_chains_sorted_by_first_departure():
    result = plan_rolling_stock(
        tt([
            train("T1", "A", "B", "08:00", "09:00"),
            train("T2", "A", "B", "08:30", "09:30"),  # 都终到 B，无后续 B 始发车
        ]),
        ["T2", "T1"], 30,  # 输入乱序不影响
    )
    assert chain_ids(result) == [["T1"], ["T2"]]  # 按首班发车时间排序


# ---------- 先最少组数、再最小等待 ----------

def test_groups_minimized_before_wait():
    # 唯一能把三个班次全部串起来的匹配是 T1→T2→T3（1 组，等待 10+20=30）；
    # 逐边贪便宜不会漏掉这条链，这里直接验证字典序目标下组数优先为 1。
    timetable = tt([
        train("T1", "A", "B", "08:00", "09:00"),
        train("T2", "B", "A", "09:10", "09:20"),
        train("T3", "A", "C", "09:40", "10:30"),
    ])
    result = plan_rolling_stock(timetable, ["T1", "T2", "T3"], 5)
    assert result["group_count"] == 1
    assert chain_ids(result) == [["T1", "T2", "T3"]]
    assert result["total_wait_minutes"] == 10 + 20


def test_min_wait_among_min_groups():
    # 两组接续等待不同：T1→T2=60、T3→T2=20；组数最少为 2，取等待更小的 T3→T2。
    timetable = tt([
        train("T1", "A", "B", "08:00", "09:00"),
        train("T3", "C", "B", "09:00", "09:30"),
        train("T2", "B", "A", "09:40", "10:30"),
        train("T0", "C", "A", "06:00", "08:00"),
    ])
    result = plan_rolling_stock(timetable, ["T1", "T2", "T3", "T0"], 5)
    # 可匹配边：T3→T2（等待10）；T1→T2（等待40）；其他无同站接续
    assert result["group_count"] == 3
    assert result["total_wait_minutes"] == 10
    assert any(ids == ["T3", "T2"] for ids in chain_ids(result))


def test_every_train_executed_once():
    timetable = tt([
        train("T1", "A", "B", "08:00", "09:00"),
        train("T2", "B", "C", "09:30", "10:00"),
        train("T3", "A", "C", "08:10", "11:00"),
        train("T4", "C", "A", "12:00", "13:00"),
    ])
    ids = ["T4", "T2", "T1", "T3"]
    result = plan_rolling_stock(timetable, ids, 20)
    seen = [tid for chain in chain_ids(result) for tid in chain]
    assert sorted(seen) == sorted(ids)
    # 每条链相邻班次确实满足同站与整备约束
    for chain in result["chains"]:
        for prev, nxt in zip(chain["trains"], chain["trains"][1:]):
            assert prev["destination"] == nxt["origin"]
            assert nxt["depart"] >= prev["arrive"] + timedelta(minutes=20)
        assert len(chain["connections"]) == len(chain["trains"]) - 1


# ---------- 随机小规模：与暴力枚举一致 ----------

def brute_force_optimal(edges):
    """枚举全部匹配：(最大匹配数, 最小费用)。edges: [(i,j,cost), ...]。"""
    adj = {}
    for k, (i, j, cost) in enumerate(edges):
        adj.setdefault(i, []).append((j, k, cost))
    lefts = sorted(adj)
    best = (-1, 10**9)

    def rec(left_idx, used_right, count, cost):
        nonlocal best
        if count > best[0] or (count == best[0] and cost < best[1]):
            best = (count, cost)
        for p in range(left_idx, len(lefts)):
            for j, _k, c in adj[lefts[p]]:
                if j not in used_right:
                    used_right.add(j)
                    rec(p + 1, used_right, count + 1, cost + c)
                    used_right.remove(j)

    rec(0, set(), 0, 0)
    return best


def test_random_instances_match_brute_force():
    rng = random.Random(20260926)
    for _ in range(40):
        n = rng.randint(1, 7)
        raw_edges = []
        for i in range(n):
            for j in range(n):
                if i != j and rng.random() < 0.4:
                    raw_edges.append((i, j, rng.randint(0, 300)))
        used = _min_cost_maximum_matching(n, raw_edges)
        card = sum(used)
        cost = sum(raw_edges[k][2] for k, u in enumerate(used) if u)
        assert (card, cost) == brute_force_optimal(raw_edges)


# ---------- 60 班次 10 秒内 ----------

def test_sixty_trains_within_ten_seconds():
    stations = [{"id": "A", "transfer_minutes": 0}, {"id": "B", "transfer_minutes": 0}]
    trains = []
    base = datetime(2026, 10, 1, 8, 0)
    for k in range(60):  # A→B、B→A 交替，每班间隔 40 分钟、全程 20 分钟
        frm, to = ("A", "B") if k % 2 == 0 else ("B", "A")
        dep = base + timedelta(minutes=40 * k)
        arr = dep + timedelta(minutes=20)
        trains.append({"id": f"G{k:02d}", "stops": [
            {"station": frm, "arrive": None, "depart": dep.strftime("%Y-%m-%dT%H:%M")},
            {"station": to, "arrive": arr.strftime("%Y-%m-%dT%H:%M"), "depart": None},
        ]})
    ids = [f"G{k:02d}" for k in range(60)]
    started = time.perf_counter()
    result = plan_rolling_stock(validate_timetable({"stations": stations, "trains": trains}),
                                ids, 10)
    elapsed = time.perf_counter() - started
    assert elapsed < 10
    assert result["group_count"] == 1
    assert chain_ids(result) == [ids]  # 全部串成一条链
    assert sum(len(c["trains"]) for c in result["chains"]) == 60


# ---------- 输入校验 ----------

@pytest.mark.parametrize("minutes", [0, 121, -5, "30", 2.5, True, None])
def test_bad_turnaround_rejected(minutes):
    with pytest.raises(TimetableError, match="整备分钟数"):
        plan_rolling_stock(tt([train("T1", "A", "B", "08:00", "09:00")]), ["T1"], minutes)


def test_train_ids_must_be_list():
    with pytest.raises(TimetableError, match="train_ids 必须是数组"):
        plan_rolling_stock(tt([]), "T1", 30)


def test_empty_selection_rejected():
    with pytest.raises(TimetableError, match="1～60"):
        plan_rolling_stock(tt([train("T1", "A", "B", "08:00", "09:00")]), [], 30)


def test_too_many_selected_rejected():
    trains = [train(f"T{k:02d}", "A", "B", "08:00", "09:00") for k in range(61)]
    # 同一时刻的 61 个班次在时刻表校验中允许存在
    timetable = tt(trains)
    with pytest.raises(TimetableError, match="60"):
        plan_rolling_stock(timetable, [f"T{k:02d}" for k in range(61)], 30)


def test_duplicate_selection_rejected():
    with pytest.raises(TimetableError, match="重复"):
        plan_rolling_stock(
            tt([train("T1", "A", "B", "08:00", "09:00")]), ["T1", "T1"], 30
        )


def test_unknown_train_rejected():
    with pytest.raises(TimetableError, match="不存在"):
        plan_rolling_stock(
            tt([train("T1", "A", "B", "08:00", "09:00")]), ["T1", "NOPE"], 30
        )


def test_non_string_train_id_rejected():
    with pytest.raises(TimetableError, match="班次 ID"):
        plan_rolling_stock(
            tt([train("T1", "A", "B", "08:00", "09:00")]), ["T1", 2], 30
        )


# ---------- API ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SAMPLE_TIMETABLE", "")
    application = create_app()
    application.config["TESTING"] = True
    with application.test_client() as client:
        yield client


def payload():
    return {
        "stations": STATIONS,
        "trains": [
            train("T1", "A", "B", "08:00", "09:00"),
            train("T2", "B", "C", "09:40", "10:30"),
            train("T3", "A", "C", "08:30", "11:00"),
        ],
    }


def test_api_plan_ok(client):
    assert client.post("/api/import", json=payload()).get_json()["ok"]
    res = client.post("/api/rolling-stock-plan",
                      json={"train_ids": ["T1", "T2", "T3"], "turnaround_minutes": 30})
    data = res.get_json()
    assert res.status_code == 200
    assert data["ok"]
    assert data["train_count"] == 3
    assert data["turnaround_minutes"] == 30
    assert data["group_count"] == 2
    # T1→T2 可接续（等待 40），T3 单独一组
    assert data["total_wait_minutes"] == 40
    ids = [[t["train_id"] for t in c["trains"]] for c in data["chains"]]
    assert sorted(ids) == [["T1", "T2"], ["T3"]]
    chain = next(c for c in data["chains"] if c["trains"][0]["train_id"] == "T1")
    assert chain["trains"][0]["depart"] == "2026-10-01T08:00"
    assert chain["trains"][-1]["arrive"] == "2026-10-01T10:30"
    assert chain["connections"] == [
        {"from_train": "T1", "to_train": "T2", "wait_minutes": 40}
    ]


def test_api_chain_trains_carry_endpoints_and_times(client):
    assert client.post("/api/import", json=payload()).get_json()["ok"]
    res = client.post("/api/rolling-stock-plan",
                      json={"train_ids": ["T1"], "turnaround_minutes": 30})
    t = res.get_json()["chains"][0]["trains"][0]
    assert t == {
        "train_id": "T1", "origin": "A", "destination": "B",
        "depart": "2026-10-01T08:00", "arrive": "2026-10-01T09:00",
    }


@pytest.mark.parametrize("body", [
    {"train_ids": [], "turnaround_minutes": 30},
    {"train_ids": ["T1", "T1"], "turnaround_minutes": 30},
    {"train_ids": ["NOPE"], "turnaround_minutes": 30},
    {"train_ids": ["T1"], "turnaround_minutes": 0},
    {"train_ids": ["T1"], "turnaround_minutes": 121},
    {"train_ids": ["T1"], "turnaround_minutes": "30"},
    {"train_ids": "T1", "turnaround_minutes": 30},
    {"turnaround_minutes": 30},
    {"train_ids": ["T1"]},
])
def test_api_bad_input_returns_400(client, body):
    assert client.post("/api/import", json=payload()).get_json()["ok"]
    res = client.post("/api/rolling-stock-plan", json=body)
    assert res.status_code == 400
    assert not res.get_json()["ok"]


def test_api_non_json_body_400(client):
    assert client.post("/api/import", json=payload()).get_json()["ok"]
    res = client.post("/api/rolling-stock-plan", data="x", content_type="text/plain")
    assert res.status_code == 400


def test_api_without_timetable_400(client):
    res = client.post("/api/rolling-stock-plan",
                      json={"train_ids": ["T1"], "turnaround_minutes": 30})
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]
