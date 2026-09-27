"""限座旅客疏运测算：席位容量、席位复用、换乘时间、时间窗边界、输入校验与 API。"""
import collections
import random
from datetime import datetime, timedelta

import pytest

from app import create_app
from planner import (
    TimetableError,
    parse_time,
    plan_passenger_flow,
    validate_timetable,
)

DAY = "2026-10-01"


def dt(value, day=DAY):
    return datetime.strptime(f"{day}T{value}", "%Y-%m-%dT%H:%M")


def station(sid, transfer=0):
    return {"id": sid, "name": sid, "transfer_minutes": transfer}


def train(tid, stops_spec):
    """stops_spec：[(站点, 到达或 None, 发车或 None), ...]，时间为 "HH:MM"。"""
    stops = []
    for sid, arrive, depart in stops_spec:
        stops.append({
            "station": sid,
            "arrive": f"{DAY}T{arrive}" if arrive else None,
            "depart": f"{DAY}T{depart}" if depart else None,
        })
    return {"id": tid, "stops": stops}


def tt(stations_spec, trains_spec):
    return validate_timetable({"stations": stations_spec, "trains": trains_spec})


def seg_ids(item):
    return [
        (s["train_id"], s["board_index"], s["alight_index"], s["from"], s["to"])
        for s in item["segments"]
    ]


def hm(moment):
    return moment.strftime("%H:%M")


def verify_solution(result, timetable, origin, destination, earliest, deadline, capacities):
    """独立校验返回方案满足全部业务规则，且每段人数不超过席位。"""
    stations = timetable["stations"]
    trains = {t["id"]: t for t in timetable["trains"]}
    assert result["total"] == sum(i["people"] for i in result["itineraries"])
    section_load = {}
    for item in result["itineraries"]:
        assert isinstance(item["people"], int) and item["people"] > 0
        segs = item["segments"]
        assert segs[0]["from"] == origin
        assert segs[-1]["to"] == destination
        assert item["depart"] == segs[0]["depart"]
        assert item["arrive"] == segs[-1]["arrive"]
        assert item["transfers"] == len(segs) - 1
        assert item["depart"] >= earliest
        assert item["arrive"] <= deadline
        for idx, seg in enumerate(segs):
            t = trains[seg["train_id"]]
            b, a = seg["board_index"], seg["alight_index"]
            assert 0 <= b < a < len(t["stops"])
            assert seg["from"] == t["stops"][b]["station"]
            assert seg["to"] == t["stops"][a]["station"]
            assert seg["depart"] == t["stops"][b]["depart"]
            assert seg["arrive"] == t["stops"][a]["arrive"]
            for k in range(b, a):
                key = (seg["train_id"], k)
                section_load[key] = section_load.get(key, 0) + item["people"]
            if idx:
                prev = segs[idx - 1]
                assert prev["to"] == seg["from"]
                if prev["train_id"] == seg["train_id"]:
                    # 同班次只能沿后续停靠点前进
                    assert seg["board_index"] == prev["alight_index"]
                else:
                    # 换车须留足本站换乘时间（端点相接允许）
                    ready = prev["arrive"] + timedelta(
                        minutes=stations[seg["from"]]["transfer_minutes"]
                    )
                    assert seg["depart"] >= ready
    for (tid, k), load in section_load.items():
        assert load <= capacities[tid], (tid, k, load)


# ---------- 基本容量与多行程 ----------

def test_single_direct_train_uses_full_capacity():
    timetable = tt(
        [station("A"), station("B"), station("C")],
        [train("T1", [("A", None, "08:00"), ("B", "09:00", "09:05"), ("C", "10:00", None)])],
    )
    result = plan_passenger_flow(
        timetable, "A", "C", dt("08:00"), dt("11:00"), {"T1": 100}
    )
    assert result["total"] == 100
    (item,) = result["itineraries"]
    assert item["people"] == 100
    assert item["transfers"] == 0
    assert seg_ids(item) == [("T1", 0, 2, "A", "C")]
    assert item["depart"] == dt("08:00")
    assert item["arrive"] == dt("10:00")
    verify_solution(result, timetable, "A", "C", dt("08:00"), dt("11:00"), {"T1": 100})


def test_parallel_trains_split_into_itineraries():
    timetable = tt(
        [station("A"), station("B")],
        [
            train("T1", [("A", None, "08:00"), ("B", "09:00", None)]),
            train("T2", [("A", None, "08:30"), ("B", "09:30", None)]),
        ],
    )
    result = plan_passenger_flow(
        timetable, "A", "B", dt("08:00"), dt("10:00"), {"T1": 30, "T2": 70}
    )
    assert result["total"] == 100
    by_train = {}
    for item in result["itineraries"]:
        (seg,) = item["segments"]
        by_train[seg["train_id"]] = item["people"]
    assert by_train == {"T1": 30, "T2": 70}


def test_section_capacity_is_bottleneck():
    # A→B 有 200 个席位可到 B，B→C 只有 T2 的 40 个席位可接续
    timetable = tt(
        [station("A"), station("B", 10), station("C")],
        [
            train("T1", [("A", None, "08:00"), ("B", "09:00", None)]),
            train("T3", [("A", None, "08:10"), ("B", "09:00", None)]),
            train("T2", [("B", None, "09:20"), ("C", "10:00", None)]),
        ],
    )
    result = plan_passenger_flow(
        timetable, "A", "C", dt("08:00"), dt("11:00"), {"T1": 100, "T2": 40, "T3": 100}
    )
    assert result["total"] == 40
    for item in result["itineraries"]:
        assert len(item["segments"]) == 2  # 全部需要在 B 换乘
    verify_solution(result, timetable, "A", "C", dt("08:00"), dt("11:00"),
                    {"T1": 100, "T2": 40, "T3": 100})


# ---------- 席位复用与换乘 ----------

def test_seats_reused_by_boarding_at_intermediate_stop():
    # earliest = 08:30：T1 在 A 站 08:00 发车早于时间窗，旅客无法从 A 乘 T1，
    # 但 T1 的 B→C 区段（09:05 发、10:10 到）仍在窗内，可在中途站 B 上车复用。
    # T2（08:30 发，08:50 到 B，+10 换乘分钟 = 09:00 ≤ 09:05）与
    # T5（08:35 发，08:55 到 B，09:05 恰好端点相接）共 20 人到 B；
    # B→C 通道：T1 的 B→C 区段（10 座）+ T3（09:30 发，10 座），合计 20 座，
    # 故 20 人全部送达，且 T1 的 B→C 区段只能由 T2/T5 的旅客乘坐。
    timetable = tt(
        [station("A"), station("B", 10), station("C")],
        [
            train("T1", [("A", None, "08:00"), ("B", "09:00", "09:05"), ("C", "10:10", None)]),
            train("T2", [("A", None, "08:30"), ("B", "08:50", None)]),
            train("T5", [("A", None, "08:35"), ("B", "08:55", None)]),
            train("T3", [("B", None, "09:30"), ("C", "10:00", None)]),
        ],
    )
    caps = {"T1": 10, "T2": 10, "T5": 10, "T3": 10}
    result = plan_passenger_flow(timetable, "A", "C", dt("08:30"), dt("10:10"), caps)
    assert result["total"] == 20
    verify_solution(result, timetable, "A", "C", dt("08:30"), dt("10:10"), caps)

    # 逐区段统计负载：T1 的 A→B 区段无人乘坐（被 earliest 截断），
    # B→C 区段满载 10 人——中途站上车的旅客复用了该车席位
    load = {}
    for item in result["itineraries"]:
        for seg in item["segments"]:
            for k in range(seg["board_index"], seg["alight_index"]):
                load[(seg["train_id"], k)] = (
                    load.get((seg["train_id"], k), 0) + item["people"]
                )
    assert load.get(("T1", 0), 0) == 0
    assert load[("T1", 1)] == 10
    assert load[("T3", 0)] == 10
    # 必有 10 人在 B（停靠序号 1）登上 T1
    reusers = [
        item for item in result["itineraries"]
        if any(s["train_id"] == "T1" and s["board_index"] == 1 for s in item["segments"])
    ]
    assert sum(item["people"] for item in reusers) == 10


def test_transfer_minutes_enforced_boundary_inclusive():
    def timetable_with_t2_depart(depart):
        return tt(
            [station("A"), station("B", 10), station("C")],
            [
                train("T1", [("A", None, "08:00"), ("B", "09:00", None)]),
                train("T2", [("B", None, depart), ("C", "10:00", None)]),
            ],
        )
    caps = {"T1": 30, "T2": 100}
    # 09:10 = 09:00 + 10 分钟，恰好满足：30 人全部接续
    ok = plan_passenger_flow(
        timetable_with_t2_depart("09:10"), "A", "C", dt("08:00"), dt("11:00"), caps
    )
    assert ok["total"] == 30
    # 09:09 差 1 分钟：接不上，无人可送达
    tight = plan_passenger_flow(
        timetable_with_t2_depart("09:09"), "A", "C", dt("08:00"), dt("11:00"), caps
    )
    assert tight["total"] == 0
    assert tight["itineraries"] == []


def test_transfer_needed_but_staying_on_same_train_needs_no_time():
    # B 换乘时间 120 分钟，也不影响 T1 原车经停 B 直达 C
    timetable = tt(
        [station("A"), station("B", 120), station("C")],
        [train("T1", [("A", None, "08:00"), ("B", "09:00", "09:02"), ("C", "10:00", None)])],
    )
    result = plan_passenger_flow(
        timetable, "A", "C", dt("08:00"), dt("11:00"), {"T1": 50}
    )
    assert result["total"] == 50
    (item,) = result["itineraries"]
    assert item["transfers"] == 0
    assert seg_ids(item) == [("T1", 0, 2, "A", "C")]


# ---------- 时间窗边界 ----------

def test_earliest_boundary_is_inclusive():
    timetable = tt(
        [station("A"), station("B")],
        [train("T1", [("A", None, "08:00"), ("B", "09:00", None)])],
    )
    caps = {"T1": 10}
    assert plan_passenger_flow(timetable, "A", "B", dt("08:00"), dt("10:00"), caps)["total"] == 10
    assert plan_passenger_flow(timetable, "A", "B", dt("08:01"), dt("10:00"), caps)["total"] == 0


def test_deadline_boundary_is_inclusive():
    timetable = tt(
        [station("A"), station("B")],
        [train("T1", [("A", None, "08:00"), ("B", "09:00", None)])],
    )
    caps = {"T1": 10}
    assert plan_passenger_flow(timetable, "A", "B", dt("08:00"), dt("09:00"), caps)["total"] == 10
    assert plan_passenger_flow(timetable, "A", "B", dt("08:00"), dt("08:59"), caps)["total"] == 0


def test_full_datetimes_across_midnight():
    timetable = tt(
        [station("A"), station("B")],
        [
            {"id": "N1", "stops": [
                {"station": "A", "arrive": None, "depart": "2026-10-01T23:30"},
                {"station": "B", "arrive": "2026-10-02T00:30", "depart": None},
            ]},
        ],
    )
    caps = {"N1": 10}
    assert plan_passenger_flow(
        timetable, "A", "B",
        datetime(2026, 10, 1, 23, 0), datetime(2026, 10, 2, 1, 0), caps,
    )["total"] == 10
    # 最晚到达 10-02 00:00，00:30 到达已超窗
    assert plan_passenger_flow(
        timetable, "A", "B",
        datetime(2026, 10, 1, 23, 0), datetime(2026, 10, 2, 0, 0), caps,
    )["total"] == 0


def test_no_route_returns_zero():
    timetable = tt(
        [station("A"), station("B"), station("C")],
        [train("T2", [("B", None, "09:00"), ("C", "10:00", None)])],
    )
    result = plan_passenger_flow(
        timetable, "A", "C", dt("08:00"), dt("11:00"), {"T2": 100}
    )
    assert result == {"total": 0, "itineraries": []}


def test_revisited_station_only_forward():
    # T 回访 B：B→D→B；只能沿后续停靠前进，不能借前序停靠形成倒流
    timetable = tt(
        [station("B"), station("D", 2)],
        [train("T", [
            ("B", None, "09:05"),
            ("D", "10:00", "10:05"),
            ("B", "11:00", None),
        ])],
    )
    caps = {"T": 7}
    # D→B 乘坐后段（10:05→11:00）
    result = plan_passenger_flow(timetable, "D", "B", dt("09:00"), dt("12:00"), caps)
    assert result["total"] == 7
    assert seg_ids(result["itineraries"][0]) == [("T", 1, 2, "D", "B")]
    # B→D 乘坐前段（09:05→10:00）
    result2 = plan_passenger_flow(timetable, "B", "D", dt("09:00"), dt("12:00"), caps)
    assert result2["total"] == 7
    assert seg_ids(result2["itineraries"][0]) == [("T", 0, 1, "B", "D")]


def test_revisited_origin_seats_reused_by_later_batch():
    # T 回访起终站：A→B→A→B，09:00、11:05 从 A 发车，10:00、12:00 到 B。
    # 两段乘车互不重叠，后一批旅客复用前一批腾出的席位：11 + 11 = 22 人。
    # 源点放人、候车链、上车、同车经停等非席位边不得把总人数卡在席位总和上。
    stations_spec = [station("A"), station("B")]
    trains_spec = [train("T", [
        ("A", None, "09:00"),
        ("B", "10:00", "10:30"),
        ("A", "11:00", "11:05"),
        ("B", "12:00", None),
    ])]
    timetable = tt(stations_spec, trains_spec)
    caps = {"T": 11}
    result = plan_passenger_flow(timetable, "A", "B", dt("08:00"), dt("13:00"), caps)
    assert result["total"] == 22
    assert len(result["itineraries"]) == 2
    first, second = result["itineraries"]  # 按出发时刻排序
    assert first["people"] == 11
    assert seg_ids(first) == [("T", 0, 1, "A", "B")]
    assert first["depart"] == dt("09:00") and first["arrive"] == dt("10:00")
    assert first["transfers"] == 0
    assert second["people"] == 11
    assert seg_ids(second) == [("T", 2, 3, "A", "B")]
    assert second["depart"] == dt("11:05") and second["arrive"] == dt("12:00")
    assert second["transfers"] == 0
    verify_solution(result, timetable, "A", "B", dt("08:00"), dt("13:00"), caps)
    # 与参考实现对照：其非席位边上界同样不按席位总和截断
    assert _reference_max_flow(
        stations_spec, trains_spec, "A", "B", dt("08:00"), dt("13:00"), caps
    ) == 22


# ---------- 随机小规模：独立 Edmonds-Karp 参考实现 + 方案可行性 ----------

def _reference_max_flow(stations_spec, trains_spec, origin, destination,
                        earliest, deadline, capacities):
    """用“单停靠点节点 + 车站时刻链”的另一等价建图跑 Edmonds-Karp，仅用于对照。"""
    stations = {s["id"]: s for s in stations_spec}
    trains = [t for t in trains_spec if t["id"] in capacities]
    # 非席位边的“无限”上界：席位随时间复用（班次回访起点时后批旅客可复用
    # 前批腾出的席位），最大流可超过席位总和；但任一源汇路径至少经过一条
    # 乘坐边，故全部乘坐边容量之和是安全上界。
    inf = sum(capacities[t["id"]] * (len(t["stops"]) - 1) for t in trains)
    graph = [[] for _ in range(2)]  # 0=源点, 1=汇点

    def add(u, v, cap):
        graph[u].append([v, cap, len(graph[v])])
        graph[v].append([u, 0, len(graph[u]) - 1])

    def new():
        graph.append([])
        return len(graph) - 1

    instants = {}
    for t in trains:
        for stop in t["stops"]:
            arrive = parse_time(stop["arrive"]) if stop["arrive"] else None
            depart = parse_time(stop["depart"]) if stop["depart"] else None
            stop["arrive"], stop["depart"] = arrive, depart
            for m in (arrive, depart):
                if m is not None and earliest <= m <= deadline:
                    instants.setdefault(stop["station"], set()).add(m)
    chain = {}
    ordered = {}
    for sid, times in instants.items():
        ts = sorted(times)
        ordered[sid] = ts
        for m in ts:
            chain[(sid, m)] = new()
        for a, b in zip(ts, ts[1:]):
            add(chain[(sid, a)], chain[(sid, b)], inf)

    import bisect as _bisect

    def at_or_after(sid, moment):
        ts = ordered.get(sid)
        if not ts:
            return None
        i = _bisect.bisect_left(ts, moment)
        return chain[(sid, ts[i])] if i < len(ts) else None

    start = at_or_after(origin, earliest)
    if start is not None:
        add(0, start, inf)

    for t in trains:
        stop_nodes = [new() for _ in t["stops"]]
        for i, stop in enumerate(t["stops"]):
            node = stop_nodes[i]
            if stop["depart"] is not None and earliest <= stop["depart"] <= deadline:
                c = chain.get((stop["station"], stop["depart"]))
                if c is not None:
                    add(c, node, inf)
            if stop["arrive"] is not None and earliest <= stop["arrive"] <= deadline:
                if stop["station"] == destination:
                    add(node, 1, inf)
                else:
                    ready = stop["arrive"] + timedelta(
                        minutes=stations[stop["station"]]["transfer_minutes"]
                    )
                    a = at_or_after(stop["station"], ready)
                    if a is not None:
                        add(node, a, inf)
        for i in range(len(t["stops"]) - 1):
            cur, nxt = t["stops"][i], t["stops"][i + 1]
            if (cur["depart"] is not None and nxt["arrive"] is not None
                    and earliest <= cur["depart"] and nxt["arrive"] <= deadline):
                add(stop_nodes[i], stop_nodes[i + 1], capacities[t["id"]])

    flow = 0
    while True:
        prev = [-1] * len(graph)
        pedge = [-1] * len(graph)
        prev[0] = 0
        queue = collections.deque([0])
        while queue and prev[1] < 0:
            u = queue.popleft()
            for ei, (v, cap, _rev) in enumerate(graph[u]):
                if cap > 0 and prev[v] < 0:
                    prev[v], pedge[v] = u, ei
                    queue.append(v)
        if prev[1] < 0:
            return flow
        aug = inf
        v = 1
        while v != 0:
            u, ei = prev[v], pedge[v]
            aug = min(aug, graph[u][ei][1])
            v = u
        v = 1
        while v != 0:
            u, ei = prev[v], pedge[v]
            graph[u][ei][1] -= aug
            graph[v][graph[u][ei][2]][1] += aug
            v = u
        flow += aug


def test_random_instances_match_reference():
    rng = random.Random(20260926)
    base = datetime(2026, 10, 1, 0, 0)
    for _ in range(40):
        n_stations = rng.randint(2, 5)
        stations_spec = [
            station(f"S{i}", rng.randint(0, 10)) for i in range(n_stations)
        ]
        trains_spec = []
        n_trains = rng.randint(1, 8)
        for k in range(n_trains):
            length = rng.randint(2, 4)
            # 随机游走选站：允许班次回访同一站点（相邻停靠不得同站），
            # 以覆盖席位随时间复用、总人数超过席位总和的场景
            cur_station = rng.randrange(n_stations)
            cur_time = base + timedelta(minutes=rng.randint(0, 900))
            stops_spec = []
            for j in range(length):
                if j == 0:
                    stops_spec.append((f"S{cur_station}", None, hm(cur_time)))
                elif j == length - 1:
                    stops_spec.append((f"S{cur_station}", hm(cur_time), None))
                else:
                    arrive = cur_time
                    depart = arrive + timedelta(minutes=rng.randint(2, 15))
                    stops_spec.append((f"S{cur_station}", hm(arrive), hm(depart)))
                    cur_time = depart
                cur_time += timedelta(minutes=rng.randint(20, 90))
                nxt = rng.randrange(n_stations - 1)
                if nxt >= cur_station:
                    nxt += 1
                cur_station = nxt
            trains_spec.append(train(f"G{k}", stops_spec))
        capacities = {t["id"]: rng.randint(1, 20) for t in trains_spec}
        timetable = tt(stations_spec, trains_spec)
        earliest = base
        deadline = base + timedelta(days=2)
        origin = stations_spec[0]["id"]
        destination = stations_spec[-1]["id"]
        result = plan_passenger_flow(
            timetable, origin, destination, earliest, deadline, capacities
        )
        expected = _reference_max_flow(
            stations_spec, trains_spec, origin, destination,
            earliest, deadline, capacities,
        )
        assert result["total"] == expected
        verify_solution(
            result, timetable, origin, destination, earliest, deadline, capacities
        )


# ---------- 输入校验 ----------

def basic_timetable():
    return tt(
        [station("A"), station("B")],
        [train("T1", [("A", None, "08:00"), ("B", "09:00", None)])],
    )


def test_capacities_must_be_dict():
    with pytest.raises(TimetableError, match="capacities"):
        plan_passenger_flow(basic_timetable(), "A", "B", dt("08:00"), dt("10:00"), [])


def test_empty_capacities_rejected():
    with pytest.raises(TimetableError, match="1～60"):
        plan_passenger_flow(basic_timetable(), "A", "B", dt("08:00"), dt("10:00"), {})


def test_too_many_trains_rejected():
    stations_spec = [station("A"), station("B")]
    trains_spec = [
        train(f"T{k:02d}", [("A", None, "08:00"), ("B", "09:00", None)])
        for k in range(61)
    ]
    timetable = tt(stations_spec, trains_spec)
    with pytest.raises(TimetableError, match="60"):
        plan_passenger_flow(
            timetable, "A", "B", dt("08:00"), dt("10:00"),
            {f"T{k:02d}": 1 for k in range(61)},
        )


def test_unknown_train_in_capacities_rejected():
    with pytest.raises(TimetableError, match="班次 ID"):
        plan_passenger_flow(
            basic_timetable(), "A", "B", dt("08:00"), dt("10:00"), {"NOPE": 10}
        )


@pytest.mark.parametrize("seats", [0, -1, 10001, "10", 1.5, True, None])
def test_seat_value_must_be_int_1_to_10000(seats):
    with pytest.raises(TimetableError, match="席位数"):
        plan_passenger_flow(
            basic_timetable(), "A", "B", dt("08:00"), dt("10:00"), {"T1": seats}
        )


def test_total_stops_over_300_rejected():
    # 50 班 × 6 站 = 300 通过；再加 1 班 2 站 = 302 报错
    stations_spec = [station(f"S{i}") for i in range(6)]
    trains_spec = []
    for k in range(50):
        t0 = datetime(2026, 10, 1) + timedelta(minutes=15 * k)

        def f(m):
            return (t0 + timedelta(minutes=m)).strftime("%H:%M")

        trains_spec.append(train(f"G{k:02d}", [
            ("S0", None, f(0)), ("S1", f(2), f(3)), ("S2", f(5), f(6)),
            ("S3", f(8), f(9)), ("S4", f(11), f(12)), ("S5", f(14), None),
        ]))
    caps300 = {f"G{k:02d}": 10 for k in range(50)}
    plan_passenger_flow(
        tt(stations_spec, trains_spec), "S0", "S5",
        datetime(2026, 9, 30), datetime(2026, 10, 5), caps300,
    )
    trains_spec.append(train("X1", [
        ("S0", None, "00:00"), ("S5", "00:01", None),
    ]))
    caps302 = dict(caps300); caps302["X1"] = 10
    with pytest.raises(TimetableError, match="300"):
        plan_passenger_flow(
            tt(stations_spec, trains_spec), "S0", "S5",
            datetime(2026, 9, 30), datetime(2026, 10, 5), caps302,
        )


def test_time_window_order_validated():
    with pytest.raises(TimetableError, match="earliest 必须严格早于"):
        plan_passenger_flow(
            basic_timetable(), "A", "B", dt("09:00"), dt("09:00"), {"T1": 1}
        )


def test_station_and_od_validated():
    with pytest.raises(TimetableError, match="起点站"):
        plan_passenger_flow(basic_timetable(), "Z", "B", dt("08:00"), dt("10:00"), {"T1": 1})
    with pytest.raises(TimetableError, match="终点站"):
        plan_passenger_flow(basic_timetable(), "A", "Z", dt("08:00"), dt("10:00"), {"T1": 1})
    with pytest.raises(TimetableError, match="起点和终点不能相同"):
        plan_passenger_flow(basic_timetable(), "A", "A", dt("08:00"), dt("10:00"), {"T1": 1})


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
    return {
        "stations": [
            station("A"), station("B", 10), station("C"),
        ],
        "trains": [
            train("T1", [("A", None, "08:00"), ("B", "09:00", "09:05"), ("C", "10:00", None)]),
            train("T2", [("B", None, "09:20"), ("C", "09:50", None)]),
        ],
    }


def test_api_flow_ok(client):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]
    res = client.post("/api/passenger-flow", json={
        "origin": "A", "destination": "C",
        "earliest": f"{DAY}T08:00", "deadline": f"{DAY}T11:00",
        "capacities": {"T1": 100, "T2": 50},
    })
    data = res.get_json()
    assert res.status_code == 200
    assert data["ok"]
    assert data["origin"] == "A" and data["destination"] == "C"
    assert data["earliest"] == f"{DAY}T08:00"
    assert data["deadline"] == f"{DAY}T11:00"
    # T1 直达 100 人（08:00→10:00）；T1 的席位区段限制下 T2 无法带来增量
    assert data["total"] == 100
    assert sum(i["people"] for i in data["itineraries"]) == 100
    (item,) = data["itineraries"]
    seg = item["segments"][0]
    assert seg == {
        "train_id": "T1", "board_index": 0, "alight_index": 2,
        "from": "A", "to": "C",
        "depart": f"{DAY}T08:00", "arrive": f"{DAY}T10:00",
    }
    assert item["depart"] == f"{DAY}T08:00"
    assert item["arrive"] == f"{DAY}T10:00"


def test_api_transfer_itinerary(client):
    payload = api_payload()
    # T1 只到 B，送达 C 必须换乘 T2（B 换乘 10 分钟，09:00+10<=09:20）
    payload["trains"][0] = train("T1", [("A", None, "08:00"), ("B", "09:00", None)])
    assert client.post("/api/import", json=payload).get_json()["ok"]
    res = client.post("/api/passenger-flow", json={
        "origin": "A", "destination": "C",
        "earliest": f"{DAY}T08:00", "deadline": f"{DAY}T11:00",
        "capacities": {"T1": 30, "T2": 100},
    })
    data = res.get_json()
    assert res.status_code == 200
    assert data["total"] == 30
    item = data["itineraries"][0]
    assert item["people"] == 30
    assert item["transfers"] == 1
    assert [(s["train_id"], s["board_index"], s["alight_index"]) for s in item["segments"]] == [
        ("T1", 0, 1), ("T2", 0, 1),
    ]
    assert item["segments"][0]["depart"] == f"{DAY}T08:00"
    assert item["segments"][1]["arrive"] == f"{DAY}T09:50"


def test_api_no_route_total_zero(client):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]
    res = client.post("/api/passenger-flow", json={
        "origin": "C", "destination": "A",
        "earliest": f"{DAY}T08:00", "deadline": f"{DAY}T11:00",
        "capacities": {"T1": 100},
    })
    data = res.get_json()
    assert res.status_code == 200
    assert data["ok"] and data["total"] == 0 and data["itineraries"] == []


def test_api_is_read_only(client):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]
    client.post("/api/passenger-flow", json={
        "origin": "A", "destination": "C",
        "earliest": f"{DAY}T08:00", "deadline": f"{DAY}T11:00",
        "capacities": {"T1": 100, "T2": 50},
    })
    data = client.get("/api/timetable").get_json()
    assert len(data["trains"]) == 2 and len(data["stations"]) == 3


@pytest.mark.parametrize("body", [
    {"origin": "A", "destination": "C", "earliest": f"{DAY}T08:00",
     "deadline": f"{DAY}T11:00"},  # 缺 capacities
    {"origin": "A", "destination": "C", "earliest": f"{DAY}T08:00",
     "deadline": f"{DAY}T11:00", "capacities": {}},  # 0 班
    {"origin": "A", "destination": "C", "earliest": f"{DAY}T08:00",
     "deadline": f"{DAY}T11:00", "capacities": {"NOPE": 10}},  # 班次不存在
    {"origin": "A", "destination": "C", "earliest": f"{DAY}T08:00",
     "deadline": f"{DAY}T11:00", "capacities": {"T1": 0}},  # 席位越下界
    {"origin": "A", "destination": "C", "earliest": f"{DAY}T08:00",
     "deadline": f"{DAY}T11:00", "capacities": {"T1": 10001}},  # 席位越上界
    {"origin": "A", "destination": "C", "earliest": f"{DAY}T08:00",
     "deadline": f"{DAY}T11:00", "capacities": {"T1": "100"}},  # 席位非整数
    {"origin": "A", "destination": "A", "earliest": f"{DAY}T08:00",
     "deadline": f"{DAY}T11:00", "capacities": {"T1": 100}},  # 起终相同
    {"origin": "Z", "destination": "C", "earliest": f"{DAY}T08:00",
     "deadline": f"{DAY}T11:00", "capacities": {"T1": 100}},  # 起点不存在
    {"origin": "A", "destination": "C", "earliest": f"{DAY}T11:00",
     "deadline": f"{DAY}T11:00", "capacities": {"T1": 100}},  # earliest 不早于 deadline
    {"origin": "A", "destination": "C", "earliest": "2026-10-01 08:00",
     "deadline": f"{DAY}T11:00", "capacities": {"T1": 100}},  # 时间格式错误
], )
def test_api_bad_input_returns_400(client, body):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]
    res = client.post("/api/passenger-flow", json=body)
    assert res.status_code == 400
    assert not res.get_json()["ok"]


def test_api_non_json_body_400(client):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]
    res = client.post("/api/passenger-flow", data="x", content_type="text/plain")
    assert res.status_code == 400


def test_api_without_timetable_400(client):
    res = client.post("/api/passenger-flow", json={
        "origin": "A", "destination": "C",
        "earliest": f"{DAY}T08:00", "deadline": f"{DAY}T11:00",
        "capacities": {"T1": 100},
    })
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]
