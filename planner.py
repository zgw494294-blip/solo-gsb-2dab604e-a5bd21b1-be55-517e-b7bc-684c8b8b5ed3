"""列车换乘规划核心逻辑：时刻表校验、最早到达行程搜索、单站站台分配、单线区间冲突排查、车底接续规划、限座旅客疏运测算与区间延误传播排程。"""
from __future__ import annotations

import bisect
import collections
import heapq
import itertools
import re
import sys
from datetime import datetime, timedelta

# 时间展开网络的流路径可能经过上千条边（站点等候链 + 停靠点边），
# 提高递归深度上限以便 Dinic 的 DFS 直接使用递归实现。
sys.setrecursionlimit(10000)

TIME_FORMAT = "%Y-%m-%dT%H:%M"
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
_MAX_ERRORS = 20


class TimetableError(ValueError):
    """时刻表数据校验失败。"""


def parse_time(value, field="time"):
    """解析 YYYY-MM-DDTHH:MM 时间，失败抛 TimetableError。"""
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise TimetableError(f"{field}: 时间必须为 YYYY-MM-DDTHH:MM 格式，实际为 {value!r}")
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except ValueError:
        raise TimetableError(f"{field}: {value!r} 不是有效的日期时间") from None


def format_time(moment):
    """把 datetime 格式化为 YYYY-MM-DDTHH:MM。"""
    return moment.strftime(TIME_FORMAT)


def validate_timetable(data):
    """校验 JSON 时刻表并返回规范化结构（时间已解析为 datetime）。

    校验站点引用、时间格式与时序；任何一处错误都会抛出 TimetableError，
    调用方应保留旧数据。
    """
    if not isinstance(data, dict):
        raise TimetableError("时刻表必须是 JSON 对象")
    raw_stations = data.get("stations")
    raw_trains = data.get("trains")
    if not isinstance(raw_stations, list) or not raw_stations:
        raise TimetableError("stations 必须是非空数组")
    if not isinstance(raw_trains, list):
        raise TimetableError("trains 必须是数组")

    errors = []

    stations = {}
    for idx, item in enumerate(raw_stations):
        where = f"stations[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: 站点必须是对象")
            continue
        sid = item.get("id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"{where}: id 必须是非空字符串")
            continue
        sid = sid.strip()
        if sid in stations:
            errors.append(f"{where}: 站点 ID {sid!r} 重复")
            continue
        minutes = item.get("transfer_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
            errors.append(f"{where}: transfer_minutes 必须是非负整数")
            continue
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            errors.append(f"{where}: name 必须是字符串")
            continue
        stations[sid] = {"name": name or sid, "transfer_minutes": minutes}

    trains = []
    seen_ids = set()
    for idx, item in enumerate(raw_trains):
        where = f"trains[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: 班次必须是对象")
            continue
        tid = item.get("id")
        if not isinstance(tid, str) or not tid.strip():
            errors.append(f"{where}: id 必须是非空字符串")
            continue
        tid = tid.strip()
        if tid in seen_ids:
            errors.append(f"{where}: 班次 ID {tid!r} 重复")
            continue
        seen_ids.add(tid)
        raw_stops = item.get("stops")
        if not isinstance(raw_stops, list) or len(raw_stops) < 2:
            errors.append(f"{where}: stops 必须至少包含 2 个按序停靠点")
            continue
        stops = []
        stops_ok = True
        for sidx, stop in enumerate(raw_stops):
            sw = f"{where}.stops[{sidx}]"
            if not isinstance(stop, dict):
                errors.append(f"{sw}: 停靠点必须是对象")
                stops_ok = False
                continue
            stid = stop.get("station")
            if not isinstance(stid, str) or stid.strip() not in stations:
                errors.append(f"{sw}: 引用了未定义的站点 {stid!r}")
                stops_ok = False
                continue
            try:
                arrive = stop.get("arrive")
                depart = stop.get("depart")
                arrive = None if arrive is None else parse_time(arrive, f"{sw}.arrive")
                depart = None if depart is None else parse_time(depart, f"{sw}.depart")
            except TimetableError as exc:
                errors.append(str(exc))
                stops_ok = False
                continue
            stops.append({"station": stid.strip(), "arrive": arrive, "depart": depart})
        if not stops_ok:
            continue
        _check_stop_times(where, stops, errors)
        trains.append({"id": tid, "stops": stops})

    if errors:
        raise TimetableError(_join_errors(errors))
    return {"stations": stations, "trains": trains}


def _check_stop_times(where, stops, errors):
    """校验单个班次的到发时间：首末站约束、站内到发顺序、站间时序。"""
    first, last = stops[0], stops[-1]
    if first["arrive"] is not None:
        errors.append(f"{where}: 首站不应有到达时间")
    if first["depart"] is None:
        errors.append(f"{where}: 首站必须有发车时间")
    if last["depart"] is not None:
        errors.append(f"{where}: 末站不应有发车时间")
    if last["arrive"] is None:
        errors.append(f"{where}: 末站必须有到达时间")
    for idx, stop in enumerate(stops):
        if 0 < idx < len(stops) - 1 and (stop["arrive"] is None or stop["depart"] is None):
            errors.append(f"{where}.stops[{idx}]: 中间站必须同时有到达和发车时间")
        if (
            stop["arrive"] is not None
            and stop["depart"] is not None
            and stop["depart"] < stop["arrive"]
        ):
            errors.append(f"{where}.stops[{idx}]: 发车时间早于到达时间")
    for idx in range(1, len(stops)):
        prev, cur = stops[idx - 1], stops[idx]
        if prev["station"] == cur["station"]:
            errors.append(f"{where}.stops[{idx}]: 与上一站为同一站点 {cur['station']!r}")
        if (
            prev["depart"] is not None
            and cur["arrive"] is not None
            and cur["arrive"] < prev["depart"]
        ):
            errors.append(f"{where}.stops[{idx}]: 到达时间早于上一站发车时间，时序错误")


def _join_errors(errors):
    shown = "；".join(errors[:_MAX_ERRORS])
    if len(errors) > _MAX_ERRORS:
        shown += f"；……共 {len(errors)} 处错误"
    return shown


def find_itinerary(timetable, origin, destination, earliest):
    """搜索最早到达的可乘行程，找不到时返回 None。

    规则：
    - 首班发车不得早于 earliest；
    - 换车需满足换乘站的 transfer_minutes（发车 >= 到达 + 换乘分钟）；
    - 同班次继续乘坐只能沿该班次的后续停靠点前进，无需换乘；
    - 时间含日期，跨午夜按日期自然计算；
    - 到达时间相同，依次选换乘次数少、班次 ID 序列字典序小的行程。
    """
    stations = timetable["stations"]
    trains = timetable["trains"]
    if origin not in stations or destination not in stations:
        raise TimetableError("起点或终点不存在")
    if origin == destination:
        raise TimetableError("起点和终点不能相同")

    # 每个站点可上车的 (班次下标, 停靠下标)
    boardings = {}
    for tidx, train in enumerate(trains):
        for sidx, stop in enumerate(train["stops"]):
            if stop["depart"] is not None:
                boardings.setdefault(stop["station"], []).append((tidx, sidx))

    # Dijkstra：状态 = (当前站点, 到达时所乘班次, 所乘班次的停靠下标)，
    # 标号 = (到达时间, 换乘次数, 班次 ID 序列)，按字典序比较。
    # 停靠下标标记班次回访同一站点时所处的具体位置：同班次继续乘坐只能
    # 从该下标及之后的停靠点出发，避免接上早已发出的前序停靠造成时间倒流。
    start = (origin, None, None)
    best = {start: (earliest, 0, ())}
    parents = {}
    counter = itertools.count()
    queue = [((earliest, 0, ()), next(counter), start)]
    final_state = None

    while queue:
        label, _, state = heapq.heappop(queue)
        if best.get(state) != label:
            continue  # 过期的堆元素
        station, last_train, last_sidx = state
        if station == destination:
            final_state = state
            break
        arrive, transfers, seq = label
        for tidx, sidx in boardings.get(station, ()):
            train = trains[tidx]
            tid = train["id"]
            depart = train["stops"][sidx]["depart"]
            if last_train is None:
                if depart < earliest:
                    continue  # 首班发车不得早于所选时间
                n_transfers, n_seq = 0, (tid,)
            elif last_train == tid:
                if sidx < last_sidx:
                    continue  # 同班次只能沿后续停靠点前进，不能接回前序停靠
                # 时刻表校验保证班次内时间单调，故此处 depart >= arrive 恒成立
                n_transfers, n_seq = transfers, seq  # 同班次经停，无需换乘
            else:
                ready = arrive + timedelta(minutes=stations[station]["transfer_minutes"])
                if depart < ready:
                    continue  # 不满足本站换乘时间
                n_transfers, n_seq = transfers + 1, seq + (tid,)
            for aidx in range(sidx + 1, len(train["stops"])):
                stop = train["stops"][aidx]
                if stop["arrive"] is None:
                    continue
                nstate = (stop["station"], tid, aidx)
                nlabel = (stop["arrive"], n_transfers, n_seq)
                if nstate in best and best[nstate] <= nlabel:
                    continue
                best[nstate] = nlabel
                parents[nstate] = (
                    state,
                    {
                        "train": tid,
                        "from": station,
                        "to": stop["station"],
                        "depart": depart,
                        "arrive": stop["arrive"],
                    },
                )
                heapq.heappush(queue, (nlabel, next(counter), nstate))

    if final_state is None:
        return None

    segments = []
    state = final_state
    while state != start:
        prev, segment = parents[state]
        segments.append(segment)
        state = prev
    segments.reverse()
    final_label = best[final_state]
    return {
        "segments": segments,
        "transfers": final_label[1],
        "train_sequence": list(final_label[2]),
        "depart": segments[0]["depart"],
        "arrive": segments[-1]["arrive"],
    }


# ---------- 单站站台分配 ----------

PLATFORM_BUFFER_MINUTES = 5  # 每次停靠在到发时间前后各占用的缓冲分钟数
MAX_PLATFORM_STOPS = 20      # 单站可参与分配的停靠次数上限
MAX_PLATFORMS = 4            # 单站站台数量上限


def collect_station_occupations(timetable, station_id):
    """收集某站全部停靠的站台占用区间，按 (占用开始, 班次 ID, 停靠序号) 排序。

    每次停靠占用 [到达-5 分钟, 发车+5 分钟)；首站取发车时间、末站取到达时间，
    区间左闭右开，端点相接不算重叠。时间含日期，跨午夜按日期自然计算。
    """
    buffer = timedelta(minutes=PLATFORM_BUFFER_MINUTES)
    occupations = []
    for train in timetable["trains"]:
        for sidx, stop in enumerate(train["stops"]):
            if stop["station"] != station_id:
                continue
            anchor_in = stop["arrive"] if stop["arrive"] is not None else stop["depart"]
            anchor_out = stop["depart"] if stop["depart"] is not None else stop["arrive"]
            occupations.append(
                {
                    "train_id": train["id"],
                    "stop_index": sidx,
                    "start": anchor_in - buffer,
                    "end": anchor_out + buffer,
                }
            )
    occupations.sort(key=lambda occ: (occ["start"], occ["train_id"], occ["stop_index"]))
    return occupations


def _validate_locks(locks, timetable, station_id, platform_count):
    """校验预锁列表，返回 {(班次 ID, 停靠序号): 站台号}；无效锁定抛 TimetableError。"""
    if locks is None:
        return {}
    if not isinstance(locks, list):
        raise TimetableError("locks 必须是数组")
    trains = {train["id"]: train for train in timetable["trains"]}
    locked = {}
    for idx, lock in enumerate(locks):
        where = f"locks[{idx}]"
        if not isinstance(lock, dict):
            raise TimetableError(f"{where}: 预锁必须是对象")
        tid = lock.get("train_id")
        if not isinstance(tid, str) or tid not in trains:
            raise TimetableError(f"{where}: 班次 {tid!r} 不存在")
        train = trains[tid]
        sidx = lock.get("stop_index")
        if (
            isinstance(sidx, bool)
            or not isinstance(sidx, int)
            or not 0 <= sidx < len(train["stops"])
        ):
            raise TimetableError(
                f"{where}: 停靠序号 {sidx!r} 超出班次 {tid} 的范围（0～{len(train['stops']) - 1}）"
            )
        platform = lock.get("platform")
        if (
            isinstance(platform, bool)
            or not isinstance(platform, int)
            or not 1 <= platform <= platform_count
        ):
            raise TimetableError(
                f"{where}: 站台编号 {platform!r} 必须在 1～{platform_count} 之间"
            )
        if train["stops"][sidx]["station"] != station_id:
            raise TimetableError(
                f"{where}: 班次 {tid} 的第 {sidx} 次停靠不在站点 {station_id}"
            )
        key = (tid, sidx)
        if key in locked:
            raise TimetableError(f"{where}: 班次 {tid} 第 {sidx} 次停靠被重复预锁")
        locked[key] = platform
    return locked


def _max_simultaneous(occupations):
    """扫描线计算任一时刻同时存活的占用数（左闭右开：同刻先结束再开始）。"""
    events = []
    for occ in occupations:
        events.append((occ["start"], 1))
        events.append((occ["end"], -1))
    # 同一时刻先处理结束（-1）再处理开始（+1），端点相接不算重叠
    events.sort(key=lambda event: (event[0], event[1]))
    peak = current = 0
    for _, delta in events:
        current += delta
        if current > peak:
            peak = current
    return peak


def plan_platforms(timetable, station_id, platform_count, locks=None):
    """为单个站点的每次停靠分配站台，返回分配列表；无解返回 None。

    - 同一站台的占用区间不得重叠（左闭右开，端点相接可共用）；
    - 预锁（locks）必须保留，无效锁定抛 TimetableError；
    - 按 (占用开始, 班次 ID, 停靠序号) 的顺序依次分配，站台编号从小到大尝试，
      首个完整方案即站台编号序列字典序最小者。
    返回的每项含 train_id、stop_index、platform、start、end，按上述顺序排序。

    性能：先用最大重叠数（区间图色数下界）预检必然无解的请求，再以
    "当前仍存活的占用各在哪个站台"为键记忆化失败状态——等价的部分分配
    只展开一次，避免在前段大量彼此分离的停靠上回溯爆炸。
    """
    if station_id not in timetable["stations"]:
        raise TimetableError(f"站点 {station_id!r} 不存在")
    if (
        isinstance(platform_count, bool)
        or not isinstance(platform_count, int)
        or not 1 <= platform_count <= MAX_PLATFORMS
    ):
        raise TimetableError(
            f"站台数量必须是 1～{MAX_PLATFORMS} 的整数，实际为 {platform_count!r}"
        )
    occupations = collect_station_occupations(timetable, station_id)
    if len(occupations) > MAX_PLATFORM_STOPS:
        raise TimetableError(
            f"站点 {station_id} 共有 {len(occupations)} 次停靠，"
            f"超过 {MAX_PLATFORM_STOPS} 次上限"
        )
    locked = _validate_locks(locks, timetable, station_id, platform_count)

    # 无解预检：任一时刻同时存活的占用数是所需站台数的下界（区间图的色数
    # 等于最大重叠数），超过站台数必然无解。预锁只限制站台选择、不改变占用
    # 区间本身，故该判定对带预锁的请求同样成立。
    if _max_simultaneous(occupations) > platform_count:
        return None

    assignment = [None] * len(occupations)
    busy = [[] for _ in range(platform_count + 1)]  # busy[p]：站台 p 已占的 (start, end)
    failed = set()  # 已证明无解的搜索状态

    def fits(platform, occ):
        # 左闭右开区间仅当 occ.start < 已占.end 且 已占.start < occ.end 时重叠
        return all(
            not (occ["start"] < taken_end and taken_start < occ["end"])
            for taken_start, taken_end in busy[platform]
        )

    def search(idx):
        if idx == len(occupations):
            return True
        # 记忆化键：占用按开始时间升序处理，已结束的占用不会与后续任何占用
        # 重叠，后续搜索只取决于"当前仍存活的占用各自所在的站台"。预检保证
        # 存活数不超过站台数，等价分配因此只会展开一次。记忆化只剪去已证明
        # 无解的子树，不改变深度优先顺序，首个完整方案仍是字典序最小者。
        now = occupations[idx]["start"]
        key = (idx, tuple(
            (j, assignment[j])
            for j in range(idx)
            if occupations[j]["end"] > now
        ))
        if key in failed:
            return False
        occ = occupations[idx]
        pinned = locked.get((occ["train_id"], occ["stop_index"]))
        candidates = (pinned,) if pinned is not None else range(1, platform_count + 1)
        for platform in candidates:
            if fits(platform, occ):
                assignment[idx] = platform
                busy[platform].append((occ["start"], occ["end"]))
                if search(idx + 1):
                    return True
                busy[platform].pop()
                assignment[idx] = None
        failed.add(key)
        return False

    if not search(0):
        return None
    return [
        {
            "train_id": occ["train_id"],
            "stop_index": occ["stop_index"],
            "platform": assignment[idx],
            "start": occ["start"],
            "end": occ["end"],
        }
        for idx, occ in enumerate(occupations)
    ]


# ---------- 单线区间冲突排查 ----------

MAX_SECTION_PASSAGES = 20    # 单次分析可处理的区间通过次数上限
MAX_CLEARANCE_MINUTES = 30   # 清空间隔上限（分钟）


def collect_section_passages(timetable, station_a, station_b):
    """收集两站间任一方向全部相邻停靠的区间通过，按 (发车时间, 班次 ID, 停靠序号) 排序。

    每次通过占用 [发车, 到达]：起点停靠序号是发车站在班次停靠表中的下标，
    方向为 发车站→到达站。时间含日期，跨午夜按日期自然计算。
    """
    passages = []
    for train in timetable["trains"]:
        stops = train["stops"]
        for sidx in range(len(stops) - 1):
            cur, nxt = stops[sidx], stops[sidx + 1]
            if {cur["station"], nxt["station"]} != {station_a, station_b}:
                continue
            # 时刻表校验保证非末站必有发车、非首站必有到达，此处均不为 None
            passages.append(
                {
                    "train_id": train["id"],
                    "stop_index": sidx,
                    "from": cur["station"],
                    "to": nxt["station"],
                    "start": cur["depart"],
                    "end": nxt["arrive"],
                }
            )
    passages.sort(key=lambda p: (p["start"], p["train_id"], p["stop_index"]))
    return passages


def _passages_conflict(p, q, clearance):
    """两次区间通过是否冲突：占用重叠，或前车驶离后未满清空间隔。

    等效于把每次通过的占用向结束方向延长清空间隔后判断相交（左闭右开）：
    恰好满足间隔（端点相接）不算冲突。
    """
    return (
        p["start"] < q["end"] + clearance
        and q["start"] < p["end"] + clearance
    )


def analyze_section_conflicts(timetable, station_a, station_b, clearance_minutes):
    """分析两站间单线区间的班次冲突，返回通过列表、冲突对与建议暂缓班次。

    - 两站须在当前时刻表中有直接相邻停靠（任一方向），否则报错；
    - 清空间隔为 0～30 的整数分钟；
    - 不同班次的两次通过，占用重叠或前车驶离后未满清空间隔即冲突，
      恰好满足间隔不算；同一班次多次通过不与自身冲突；
    - 区间通过总次数超过 20 次报错；
    - 建议暂缓集合是使剩余班次互不冲突的最少班次（同一班次多次通过整体
      暂缓），同样最少时取排序后班次 ID 序列字典序最小者；无冲突时为空。
    分析不修改时刻表。
    """
    stations = timetable["stations"]
    if not isinstance(station_a, str) or station_a not in stations:
        raise TimetableError(f"站点 {station_a!r} 不存在")
    if not isinstance(station_b, str) or station_b not in stations:
        raise TimetableError(f"站点 {station_b!r} 不存在")
    if station_a == station_b:
        raise TimetableError("两个站点不能相同")
    if (
        isinstance(clearance_minutes, bool)
        or not isinstance(clearance_minutes, int)
        or not 0 <= clearance_minutes <= MAX_CLEARANCE_MINUTES
    ):
        raise TimetableError(
            f"清空间隔必须是 0～{MAX_CLEARANCE_MINUTES} 的整数分钟，"
            f"实际为 {clearance_minutes!r}"
        )
    passages = collect_section_passages(timetable, station_a, station_b)
    if not passages:
        raise TimetableError(
            f"站点 {station_a} 与 {station_b} 在当前时刻表中没有直接相邻的停靠"
        )
    if len(passages) > MAX_SECTION_PASSAGES:
        raise TimetableError(
            f"区间 {station_a}～{station_b} 共有 {len(passages)} 次通过，"
            f"超过 {MAX_SECTION_PASSAGES} 次上限"
        )
    clearance = timedelta(minutes=clearance_minutes)
    conflicts = []
    edges = set()
    for idx, first in enumerate(passages):
        for second in passages[idx + 1:]:
            if first["train_id"] == second["train_id"]:
                continue  # 同一班次多次通过不与自身冲突
            if _passages_conflict(first, second, clearance):
                conflicts.append({"first": first, "second": second})
                edges.add((first["train_id"], second["train_id"]))
    return {
        "passages": passages,
        "conflicts": conflicts,
        "suspended": _minimum_train_cover(edges),
    }


def _minimum_train_cover(edges):
    """求班次冲突图的最小点覆盖：需暂缓调整的最少班次集合（排序后返回）。

    同样最少时取排序后班次 ID 序列字典序最小者。分支定界：取当前度数最高的
    班次 u，分“暂缓 u”与“保留 u、暂缓其全部冲突对方”两支；以剩余冲突边的
    极大匹配大小作为还需暂缓班次数的下界剪枝。冲突班次数受区间通过次数
    上限（20）约束，可精确求解。
    """
    adj = {}
    for u, v in edges:
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)
    best = None

    def matching_bound(graph):
        # 极大匹配的边互不相邻，每条至少需暂缓一个端点，故是合法下界
        matched = set()
        size = 0
        for u, nbrs in graph.items():
            if u in matched or not nbrs:
                continue
            for v in nbrs:
                if v not in matched:
                    matched.add(u)
                    matched.add(v)
                    size += 1
                    break
        return size

    def search(graph, cover):
        nonlocal best
        if best is not None and len(cover) + matching_bound(graph) > len(best):
            return
        hub = None
        for u, nbrs in graph.items():
            if nbrs and (hub is None or len(nbrs) > len(graph[hub])):
                hub = u
        if hub is None:  # 冲突边已全部覆盖
            cand = tuple(sorted(cover))
            if (
                best is None
                or len(cand) < len(best)
                or (len(cand) == len(best) and cand < best)
            ):
                best = cand
            return
        neighbors = graph[hub]
        # 支路一：暂缓 hub
        search(
            {u: (nbrs - {hub}) for u, nbrs in graph.items() if u != hub},
            cover | {hub},
        )
        # 支路二：保留 hub，暂缓其全部冲突对方（hub 随之成为孤立点，一并移出）
        search(
            {
                u: (nbrs - neighbors)
                for u, nbrs in graph.items()
                if u != hub and u not in neighbors
            },
            cover | neighbors,
        )

    search(adj, frozenset())
    return sorted(best) if best else []


# ---------- 车底接续规划 ----------

MAX_ROLLING_STOCK_TRAINS = 60   # 单次规划可选班次数上限
MIN_ROLLING_STOCK_TRAINS = 1    # 单次规划可选班次数下限
MAX_TURNAROUND_MINUTES = 120    # 整备时间上限（分钟）
MIN_TURNAROUND_MINUTES = 1      # 整备时间下限（分钟）


def plan_rolling_stock(timetable, train_ids, turnaround_minutes):
    """为所选班次规划可互换车底的接续链。

    每个班次以首站发车至末站到达为一个完整任务；一组车底执行一条按时间
    顺序的班次链，链中相邻两班必须满足：前班终站 == 后班首站，且后班发车
    不早于前班到达 + 整备分钟数（端点相接允许，不允许空驶）。每班恰好执行
    一次；首班前不限制车底来源，故链首即为新增车底。

    目标按字典序：先最少化车底组数（链数），再最小化全部接续的等待分钟数
    之和（等待 = 后班发车 - 前班到达），并列时任一方案均可。

    模型：可行接续边由时间严格指向未来（同刻到达也不能在整备 > 0 时接续），
    任务图是 DAG。其最小路径覆盖数 = 任务数 - 二部图最大匹配数；在所有最大
    匹配中取边权（等待分钟）总和最小者即第二目标。用最小费用最大流
    （SPFA 求最短路的连续最短路算法）求带权最大匹配：源点 → 左 → 右 → 汇点，
    接续边容量 1、费用为等待分钟；60 班次下至多增广 60 次。

    返回 {"group_count", "total_wait_minutes", "chains"}：每条链含按执行顺序
    的 trains（首末站与到发时间）与 connections（接续等待，长度 = 班次数 - 1）。
    """
    if (
        isinstance(turnaround_minutes, bool)
        or not isinstance(turnaround_minutes, int)
        or not MIN_TURNAROUND_MINUTES <= turnaround_minutes <= MAX_TURNAROUND_MINUTES
    ):
        raise TimetableError(
            f"整备分钟数必须是 {MIN_TURNAROUND_MINUTES}～{MAX_TURNAROUND_MINUTES} "
            f"的整数，实际为 {turnaround_minutes!r}"
        )
    if not isinstance(train_ids, list):
        raise TimetableError("train_ids 必须是数组")
    if not MIN_ROLLING_STOCK_TRAINS <= len(train_ids) <= MAX_ROLLING_STOCK_TRAINS:
        raise TimetableError(
            f"所选班次必须为 {MIN_ROLLING_STOCK_TRAINS}～{MAX_ROLLING_STOCK_TRAINS} 个，"
            f"实际为 {len(train_ids)} 个"
        )

    trains_by_id = {train["id"]: train for train in timetable["trains"]}
    tasks = []
    seen = set()
    for pos, raw in enumerate(train_ids):
        if not isinstance(raw, str) or not raw.strip():
            raise TimetableError(f"train_ids[{pos}]: 班次 ID 必须是非空字符串")
        tid = raw.strip()
        if tid in seen:
            raise TimetableError(f"班次 {tid!r} 在所选列表中重复")
        seen.add(tid)
        if tid not in trains_by_id:
            raise TimetableError(f"班次 {tid!r} 不存在于当前时刻表")
        train = trains_by_id[tid]
        first_stop, last_stop = train["stops"][0], train["stops"][-1]
        # 时刻表校验保证首站必有发车、末站必有到达，此处均不为 None
        tasks.append(
            {
                "id": tid,
                "origin": first_stop["station"],
                "destination": last_stop["station"],
                "depart": first_stop["depart"],
                "arrive": last_stop["arrive"],
            }
        )

    n = len(tasks)
    turnaround = timedelta(minutes=turnaround_minutes)
    # 可行接续边：同站、时间满足整备；按 (前班位置, 后班位置) 加入，保证求解
    # 与并列方案的选择完全确定。费用即等待分钟（后班发车 - 前班到达，含整备）。
    edges = []
    for i, prev in enumerate(tasks):
        ready = prev["arrive"] + turnaround
        for j, nxt in enumerate(tasks):
            if i == j or prev["destination"] != nxt["origin"]:
                continue
            if nxt["depart"] >= ready:
                edges.append(
                    (
                        i,
                        j,
                        int((nxt["depart"] - prev["arrive"]).total_seconds() // 60),
                    )
                )

    matching = _min_cost_maximum_matching(n, edges)
    matched = sum(matching)  # 匹配数 = 被合并的接续数

    # 由匹配重建接续链：successor[i] = i 的后班下标
    successor = [-1] * n
    predecessor = [-1] * n
    total_wait = 0
    for edge_idx, is_used in enumerate(matching):
        if is_used:
            i, j, cost = edges[edge_idx]
            successor[i] = j
            predecessor[j] = i
            total_wait += cost

    chains = []
    for head in (idx for idx in range(n) if predecessor[idx] < 0):
        chain_trains = []
        connections = []
        cur = head
        while cur >= 0:
            task = tasks[cur]
            chain_trains.append(
                {
                    "train_id": task["id"],
                    "origin": task["origin"],
                    "destination": task["destination"],
                    "depart": task["depart"],
                    "arrive": task["arrive"],
                }
            )
            nxt = successor[cur]
            if nxt >= 0:
                wait = int(
                    (tasks[nxt]["depart"] - tasks[cur]["arrive"]).total_seconds() // 60
                )
                connections.append(
                    {
                        "from_train": task["id"],
                        "to_train": tasks[nxt]["id"],
                        "wait_minutes": wait,
                    }
                )
            cur = nxt
        chains.append({"trains": chain_trains, "connections": connections})

    # 链按（首班发车时间, 首班 ID）排序，展示稳定
    chains.sort(key=lambda chain: (chain["trains"][0]["depart"], chain["trains"][0]["train_id"]))
    assert sum(len(chain["trains"]) for chain in chains) == n
    return {
        "group_count": n - matched,
        "total_wait_minutes": total_wait,
        "chains": chains,
    }


def _min_cost_maximum_matching(n, edges):
    """求 n 个任务二部图上的最小费用最大匹配。

    节点：0=源点，1..n=左（任务的“前班”角色），n+1..2n=右（“后班”角色），
    2n+1=汇点。源点→左、右→汇点容量 1 费用 0；接续边 左 i → 右 j 容量 1
    费用 = 等待分钟。用 SPFA 连续最短路逐单位增广：每次增广在保持已选匹配
    可增广（匹配数最大）的前提下取当前最短路，整体得到“先最大匹配、再最小
    费用”的字典序最优；费用非负且网络无负环。汇点不可达即匹配已最大。
    返回长度为 len(edges) 的 0/1 列表，按 edges 加入顺序标记该接续是否入选。
    """
    source, sink = 0, 2 * n + 1
    graph = [[] for _ in range(2 * n + 2)]

    def add_edge(u, v, capacity, cost):
        graph[u].append([v, capacity, cost, len(graph[v])])
        graph[v].append([u, 0, -cost, len(graph[u]) - 1])

    for i in range(n):
        add_edge(source, 1 + i, 1, 0)
        add_edge(1 + n + i, sink, 1, 0)
    # 记录每条接续边正向边所在的（左节点邻接表, 局部下标），便于最终判定是否入选
    edge_refs = []
    for i, j, cost in edges:
        edge_refs.append((1 + i, len(graph[1 + i])))
        add_edge(1 + i, 1 + n + j, 1, cost)

    inf = float("inf")
    flow = 0
    while True:
        # SPFA：网络残留含负费用反向边，用 Bellman-Ford 队列实现求最短费用路
        dist = [inf] * len(graph)
        prev_node = [-1] * len(graph)
        prev_edge = [-1] * len(graph)
        in_queue = [False] * len(graph)
        dist[source] = 0
        queue = collections.deque([source])
        in_queue[source] = True
        while queue:
            u = queue.popleft()
            in_queue[u] = False
            for eidx, (v, capacity, cost, _rev) in enumerate(graph[u]):
                if capacity > 0 and dist[u] + cost < dist[v]:
                    dist[v] = dist[u] + cost
                    prev_node[v] = u
                    prev_edge[v] = eidx
                    if not in_queue[v]:
                        queue.append(v)
                        in_queue[v] = True
        if prev_node[sink] < 0:
            break  # 汇点不可达：匹配数已最大
        v = sink
        while v != source:
            u, eidx = prev_node[v], prev_edge[v]
            edge = graph[u][eidx]
            edge[1] -= 1
            graph[v][edge[3]][1] += 1
            v = u
        flow += 1

    # 接续边容量为 1，残留容量为 0 即该边入选匹配
    used = [1 if graph[u][eidx][1] == 0 else 0 for (u, eidx) in edge_refs]
    assert flow == sum(used)
    return used


# ---------- 限座旅客疏运测算 ----------

MAX_FLOW_TRAINS = 60        # 单次测算可选班次数上限
MIN_FLOW_TRAINS = 1         # 单次测算可选班次数下限
MAX_FLOW_SEATS = 10000      # 单班席位数上限
MIN_FLOW_SEATS = 1          # 单班席位数下限
MAX_FLOW_TOTAL_STOPS = 300  # 所选班次停靠点总数上限


class _Dinic:
    """整数容量 Dinic 最大流；节点可在建图过程中按需追加。

    边以 [对端, 残留容量, 反向边下标] 存储；正向边初始容量即边容量，
    反向边初始为 0。
    """

    def __init__(self):
        self.graph = [[]]
        self.forward_edges = []  # 全部正向边的 (起点, 邻接表下标)

    def new_node(self):
        self.graph.append([])
        return len(self.graph) - 1

    def add_edge(self, u, v, capacity):
        forward = [v, capacity, len(self.graph[v])]
        backward = [u, 0, len(self.graph[u])]
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        edge_index = len(self.graph[u]) - 1
        self.forward_edges.append((u, edge_index))
        return edge_index  # 正向边在 u 邻接表中的下标

    def edge_flow(self, u, edge_index):
        """正向边上的流量（原始容量 - 残留容量）。"""
        v, residual, rev = self.graph[u][edge_index]
        return self.graph[v][rev][1]

    def max_flow(self, source, sink):
        total = 0
        n = len(self.graph)
        while True:
            level = [-1] * n
            level[source] = 0
            queue = collections.deque([source])
            while queue:
                u = queue.popleft()
                for v, capacity, _rev in self.graph[u]:
                    if capacity > 0 and level[v] < 0:
                        level[v] = level[u] + 1
                        queue.append(v)
            if level[sink] < 0:
                return total
            cur = [0] * n

            def dfs(u, pushed):
                if u == sink:
                    return pushed
                while cur[u] < len(self.graph[u]):
                    edge = self.graph[u][cur[u]]
                    v, capacity, rev = edge
                    if capacity > 0 and level[v] == level[u] + 1:
                        sent = dfs(v, min(pushed, capacity))
                        if sent:
                            edge[1] -= sent
                            self.graph[v][rev][1] += sent
                            return sent
                    cur[u] += 1
                return 0

            while True:
                sent = dfs(source, float("inf"))
                if not sent:
                    break
                total += sent


def plan_passenger_flow(timetable, origin, destination, earliest, deadline, capacities):
    """计算时间窗内最多可从 origin 送达 destination 的旅客人数。

    规则：
    - 只使用 capacities 中给出席位数的班次；每个相邻停靠区段（乘坐边）
      容量为该班席位，旅客下车后容量即可被后段复用；
    - 同班次只能沿后续停靠点前进，经停等待不占席位之外的资源、无需换乘时间；
    - 换车须满足换乘站 transfer_minutes（后班发车 >= 前班到达 + 换乘分钟）；
    - 首班发车不早于 earliest、末次到达不晚于 deadline，按完整日期比较。

    模型为时间展开 DAG 上的整数最大流：
    - 每个相关停靠点拆成“到达节点”“发车节点”，二者间为无限容量的经停边
      （同车人原地等候）；相邻停靠点的“发车 → 到达”为席位容量的乘坐边；
    - 每个车站把时间窗内相关时刻按顺序连成等候链，链上无限容量表示候车；
      上车边（时刻点 → 发车节点）无限，终到直接接入汇点，中途下车须留足
      本站换乘时间后接入等候链对应时刻。
    “无限容量”实现为全部乘坐边容量之和：席位随时间复用（班次回访起点时
    后一批旅客可使用前一批腾出的席位），送达总人数可超过各班列席位之和，
    但任一源汇路径至少经过一条乘坐边，故该和是永不成为瓶颈的安全上界。
    所有容量为整数，最大流为整数；再把源汇间的整数流分解为带人数的行程。

    返回 {"total", "itineraries"}：每条行程含人数 people 与按顺序的乘坐段
    segments（班次、上下车停靠序号、上下车站、到发时间）。并列方案任选；
    无路时 total 为 0、行程为空。测算只读，不修改时刻表。
    """
    stations = timetable["stations"]
    trains_by_id = {train["id"]: train for train in timetable["trains"]}
    if not isinstance(origin, str) or origin not in stations:
        raise TimetableError(f"起点站 {origin!r} 不存在")
    if not isinstance(destination, str) or destination not in stations:
        raise TimetableError(f"终点站 {destination!r} 不存在")
    if origin == destination:
        raise TimetableError("起点和终点不能相同")
    if not isinstance(earliest, datetime) or not isinstance(deadline, datetime):
        raise TimetableError("earliest、deadline 必须为解析后的日期时间")
    if earliest >= deadline:
        raise TimetableError("earliest 必须严格早于 deadline")
    if not isinstance(capacities, dict):
        raise TimetableError("capacities 必须是班次 ID 到席位数的映射（JSON 对象）")

    # 校验席位映射并按时刻表顺序固定所选班次，保证建图与并列方案确定
    selected = []
    total_stops = 0
    for pos, (tid, seats) in enumerate(capacities.items()):
        if not isinstance(tid, str) or not tid.strip() or tid not in trains_by_id:
            raise TimetableError(f"capacities 的键必须是当前时刻表中的班次 ID（第 {pos + 1} 个为 {tid!r}）")
        if isinstance(seats, bool) or not isinstance(seats, int) or not MIN_FLOW_SEATS <= seats <= MAX_FLOW_SEATS:
            raise TimetableError(
                f"班次 {tid} 的席位数必须是 {MIN_FLOW_SEATS}～{MAX_FLOW_SEATS} 的整数，"
                f"实际为 {seats!r}"
            )
    if not MIN_FLOW_TRAINS <= len(capacities) <= MAX_FLOW_TRAINS:
        raise TimetableError(
            f"所选班次必须为 {MIN_FLOW_TRAINS}～{MAX_FLOW_TRAINS} 个，"
            f"实际为 {len(capacities)} 个"
        )
    for train in timetable["trains"]:
        if train["id"] in capacities:
            selected.append((train, capacities[train["id"]]))
            total_stops += len(train["stops"])
    if total_stops > MAX_FLOW_TOTAL_STOPS:
        raise TimetableError(
            f"所选班次停靠点总数为 {total_stops}，超过 {MAX_FLOW_TOTAL_STOPS} 上限"
        )

    # 非席位边（源点放人、候车链、上下车、同车经停、终到交差）的容量上界。
    # 席位只约束相邻停靠区段（乘坐边）：旅客下车后席位即被后续区段复用，
    # 班次回访起点时后一批旅客可使用前一批腾出的席位，送达总人数因此可以
    # 超过各班列席位之和。任一源汇路径至少经过一条乘坐边，故最大流不超过
    # 全部乘坐边容量之和；取该和作为非席位边容量，既为有限整数又永不成为瓶颈。
    unbounded = sum(seats * (len(train["stops"]) - 1) for train, seats in selected)

    # ---------- 建图 ----------
    dinic = _Dinic()
    source = dinic.new_node()  # 0
    sink = dinic.new_node()    # 1

    # 车站等候链：收集时间窗内每个车站需要出现的时刻（相关停靠的到/发）
    instants = collections.defaultdict(set)
    for train, _seats in selected:
        for sidx, stop in enumerate(train["stops"]):
            if stop["depart"] is not None and earliest <= stop["depart"] <= deadline:
                instants[stop["station"]].add(stop["depart"])
            if stop["arrive"] is not None and earliest <= stop["arrive"] <= deadline:
                instants[stop["station"]].add(stop["arrive"])

    chain_node = {}   # (站点, 时刻) -> 等候链节点
    chain_times = {}  # 站点 -> 排序去重后的时刻列表
    for sid, times in instants.items():
        ordered = sorted(times)
        chain_times[sid] = ordered
        for moment in ordered:
            chain_node[(sid, moment)] = dinic.new_node()
        for a, b in zip(ordered, ordered[1:]):
            dinic.add_edge(chain_node[(sid, a)], chain_node[(sid, b)], unbounded)

    def chain_at_or_after(sid, moment):
        """车站等候链中第一个不早于 moment 的时刻点，不存在返回 None。"""
        ordered = chain_times.get(sid)
        if not ordered:
            return None
        pos = bisect.bisect_left(ordered, moment)
        return chain_node[(sid, ordered[pos])] if pos < len(ordered) else None

    # 起点：从 earliest 对应时刻点放人（人数不设上限，席位随时间复用）
    start_node = chain_at_or_after(origin, earliest)
    if start_node is not None:
        dinic.add_edge(source, start_node, unbounded)

    # ride_ref[(班次 ID, 上车站停靠序号)] -> 乘坐边元信息（供流分解还原行程）
    ride_refs = {}
    for train, seats in selected:
        tid = train["id"]
        stops = train["stops"]
        arrive_node = [None] * len(stops)
        depart_node = [None] * len(stops)
        for sidx, stop in enumerate(stops):
            if stop["arrive"] is not None and earliest <= stop["arrive"] <= deadline:
                arrive_node[sidx] = dinic.new_node()
            if stop["depart"] is not None and earliest <= stop["depart"] <= deadline:
                depart_node[sidx] = dinic.new_node()

        for sidx, stop in enumerate(stops):
            an, dn = arrive_node[sidx], depart_node[sidx]
            if an is not None and dn is not None:
                # 中间站经停：同车人不下车、沿原班继续，无需换乘时间
                dinic.add_edge(an, dn, unbounded)
            station = stop["station"]
            if dn is not None:
                # 上车：在该站候车至本班发车时刻
                board = chain_node.get((station, stop["depart"]))
                if board is not None:
                    dinic.add_edge(board, dn, unbounded)
            if an is not None:
                if station == destination:
                    # 终到：直接交差（到达时刻不晚于 deadline 才有此节点），
                    # 不接入等候链，避免“到终点后再乘车离开”的绕路行程
                    dinic.add_edge(an, sink, unbounded)
                else:
                    # 下车换车：须留足本站换乘时间后进入等候链
                    ready = stop["arrive"] + timedelta(
                        minutes=stations[station]["transfer_minutes"]
                    )
                    alight = chain_at_or_after(station, ready)
                    if alight is not None:
                        dinic.add_edge(an, alight, unbounded)

        # 相邻停靠区段：席位容量；下车后即可被后续区段复用
        for sidx in range(len(stops) - 1):
            dn, an = depart_node[sidx], arrive_node[sidx + 1]
            if dn is not None and an is not None:
                edge_index = dinic.add_edge(dn, an, seats)
                ride_refs[(tid, sidx)] = (dn, edge_index)

    # ---------- 最大流 ----------
    total = dinic.max_flow(source, sink)
    if total == 0:
        return {"total": 0, "itineraries": []}

    # ---------- 流分解：把正向边上的整数流拆成源→汇行程 ----------
    # 仅保留有流量的正向边；时间展开网络在无容量限制的平行时刻点上可能形成
    # 环流（下车边与等候链、经停边同刻相接），沿途成环时按环上最小流量扣除，
    # 不影响送抵终点的总人数。
    adj = collections.defaultdict(list)  # u -> [[v, 当前流量, 乘坐段 key 或 None]]
    ride_key_of = {(u, edge_index): key for key, (u, edge_index) in ride_refs.items()}
    for u, edge_index in dinic.forward_edges:
        flow = dinic.edge_flow(u, edge_index)
        if flow:
            v = dinic.graph[u][edge_index][0]
            adj[u].append([v, flow, ride_key_of.get((u, edge_index))])

    groups = {}  # 乘坐段 key 元组 -> 人数
    remaining = total
    while remaining:
        path_nodes = [source]
        path_keys = []
        pos = {source: 0}
        cur = source
        while cur != sink:
            while adj[cur] and adj[cur][-1][1] <= 0:
                adj[cur].pop()
            nxt, _flow, key = adj[cur][-1]
            if nxt in pos:
                # 出现环流：从环上第一条边到当前边，按最小流量整体扣除
                cycle_start = pos[nxt]
                cycle_edges = []
                for node in path_nodes[cycle_start:]:
                    cycle_edges.append(adj[node][-1])
                cut = min(edge[1] for edge in cycle_edges)
                for edge in cycle_edges:
                    edge[1] -= cut
                # 回退到环起点重新探路
                for node in path_nodes[cycle_start + 1:]:
                    pos.pop(node, None)
                del path_nodes[cycle_start + 1:]
                del path_keys[cycle_start:]
                cur = nxt
                continue
            pos[nxt] = len(path_nodes)
            path_nodes.append(nxt)
            path_keys.append(key)
            cur = nxt
        path_edges = [adj[node][-1] for node in path_nodes[:-1]]
        amount = min(edge[1] for edge in path_edges)
        for edge in path_edges:
            edge[1] -= amount
        shape = tuple(key for key in path_keys if key is not None)
        groups[shape] = groups.get(shape, 0) + amount
        remaining -= amount

    # ---------- 由乘坐段序列还原行程 ----------
    itineraries = []
    for shape, people in groups.items():
        segments = []
        cur_train = None
        for key in shape:
            tid, sidx = key
            if cur_train != tid:
                train = trains_by_id[tid]
                cur_train = tid
                segments.append({
                    "train_id": tid,
                    "board_index": sidx,
                    "alight_index": sidx + 1,
                    "from": train["stops"][sidx]["station"],
                    "to": train["stops"][sidx + 1]["station"],
                    "depart": train["stops"][sidx]["depart"],
                    "arrive": train["stops"][sidx + 1]["arrive"],
                })
            else:
                seg = segments[-1]
                train = trains_by_id[tid]
                seg["alight_index"] = sidx + 1
                seg["to"] = train["stops"][sidx + 1]["station"]
                seg["arrive"] = train["stops"][sidx + 1]["arrive"]
        itineraries.append({
            "people": people,
            "segments": segments,
            "depart": segments[0]["depart"],
            "arrive": segments[-1]["arrive"],
            "transfers": len(segments) - 1,
        })

    # 行程排序确定：出发时刻、到达时刻、首班 ID、首段上车站序号
    itineraries.sort(key=lambda item: (
        item["depart"], item["arrive"],
        item["segments"][0]["train_id"], item["segments"][0]["board_index"],
    ))
    assert sum(item["people"] for item in itineraries) == total
    return {"total": total, "itineraries": itineraries}


# ---------- 区间延误传播排程 ----------

MAX_DELAY_TRAINS = 40       # 单次排程可选班次数上限
MIN_DELAY_TRAINS = 1        # 单次排程可选班次数下限
MAX_DELAY_MINUTES = 120     # 单班首发延误上限（分钟）
MIN_DELAY_MINUTES = 0       # 单班首发延误下限（分钟）
MAX_DELAY_TOTAL_STOPS = 200  # 所选班次停靠点总数上限


def _compute_delay_schedule(timetable, train_ids, initial_delays, clearance_minutes):
    """校验输入并计算逐项最早可行排程的中间状态（排程与晚点归因共用）。

    校验失败抛 TimetableError。成功时返回：
    - selected：按时刻表顺序的所选班次；
    - delays：班次 ID -> 首发延误分钟数（仅显式填写的班次）；
    - clearance：清空间隔（timedelta）；
    - run：(班次 ID, 起点停靠序号) -> 区间运行时长；
    - dwell：(班次 ID, 停靠序号) -> 中间站原停留时长；
    - predecessor：(班次 ID, 起点停靠序号) -> 同一区间顺序上前一次不同班次通过；
    - adjusted_depart：(班次 ID, 停靠序号) -> 调整后发车时刻（逐项最早）；
    - total_stops：所选班次停靠点总数。
    """
    trains_by_id = {train["id"]: train for train in timetable["trains"]}
    if (
        isinstance(clearance_minutes, bool)
        or not isinstance(clearance_minutes, int)
        or not 0 <= clearance_minutes <= MAX_CLEARANCE_MINUTES
    ):
        raise TimetableError(
            f"清空间隔必须是 0～{MAX_CLEARANCE_MINUTES} 的整数分钟，"
            f"实际为 {clearance_minutes!r}"
        )
    if not isinstance(train_ids, list):
        raise TimetableError("train_ids 必须是数组")
    if not MIN_DELAY_TRAINS <= len(train_ids) <= MAX_DELAY_TRAINS:
        raise TimetableError(
            f"所选班次必须为 {MIN_DELAY_TRAINS}～{MAX_DELAY_TRAINS} 个，"
            f"实际为 {len(train_ids)} 个"
        )
    seen = set()
    for pos, raw in enumerate(train_ids):
        if not isinstance(raw, str) or not raw.strip():
            raise TimetableError(f"train_ids[{pos}]: 班次 ID 必须是非空字符串")
        tid = raw.strip()
        if tid in seen:
            raise TimetableError(f"班次 {tid!r} 在所选列表中重复")
        seen.add(tid)
        if tid not in trains_by_id:
            raise TimetableError(f"班次 {tid!r} 不存在于当前时刻表")

    if initial_delays is None:
        initial_delays = {}
    if not isinstance(initial_delays, dict):
        raise TimetableError("initial_delays 必须是班次 ID 到首发延误分钟数的映射（JSON 对象）")
    delays = {}
    for tid, minutes in initial_delays.items():
        if not isinstance(tid, str) or tid not in seen:
            raise TimetableError(f"initial_delays 的键 {tid!r} 不是所选班次")
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, int)
            or not MIN_DELAY_MINUTES <= minutes <= MAX_DELAY_MINUTES
        ):
            raise TimetableError(
                f"班次 {tid} 的首发延误必须是 {MIN_DELAY_MINUTES}～{MAX_DELAY_MINUTES} "
                f"的整数分钟，实际为 {minutes!r}"
            )
        delays[tid] = minutes

    # 按时刻表顺序固定所选班次，保证结果与输入顺序无关
    selected = [train for train in timetable["trains"] if train["id"] in seen]
    total_stops = sum(len(train["stops"]) for train in selected)
    if total_stops > MAX_DELAY_TOTAL_STOPS:
        raise TimetableError(
            f"所选班次停靠点总数为 {total_stops}，超过 {MAX_DELAY_TOTAL_STOPS} 上限"
        )

    # 区间运行时长与站内停留时长（调整前后不变/不少于）
    run = {}    # (班次 ID, 起点停靠序号) -> 区间运行时长
    dwell = {}  # (班次 ID, 停靠序号) -> 中间站停留时长
    for train in selected:
        stops = train["stops"]
        for sidx in range(len(stops) - 1):
            # 时刻表校验保证非末站必有发车、非首站必有到达，此处均不为 None
            run[(train["id"], sidx)] = stops[sidx + 1]["arrive"] - stops[sidx]["depart"]
        for sidx in range(1, len(stops) - 1):
            dwell[(train["id"], sidx)] = stops[sidx]["depart"] - stops[sidx]["arrive"]

    # 单线区间：相邻站点的无向站对。收集每次通过并按
    # （原定发车时间, 班次 ID, 起点停靠序号）固定顺序；顺序相邻且班次不同的
    # 两次通过间建立 清空约束：后车发车 >= 前车到达 + 清空间隔
    sections = {}
    for train in selected:
        stops = train["stops"]
        for sidx in range(len(stops) - 1):
            pair = tuple(sorted((stops[sidx]["station"], stops[sidx + 1]["station"])))
            sections.setdefault(pair, []).append(
                (stops[sidx]["depart"], train["id"], sidx)
            )
    predecessor = {}  # (班次 ID, 起点停靠序号) -> 顺序上前一次不同班次通过
    for passages in sections.values():
        passages.sort()
        for prev, cur in zip(passages, passages[1:]):
            if prev[1] != cur[1]:  # 同一班次的连续通过不自我约束
                predecessor[(cur[1], cur[2])] = (prev[1], prev[2])

    # 按（原发车时刻, 班次 ID, 停靠序号）升序传播：班内与区间依赖在该键下
    # 均严格递增，故每个发车时刻被处理时其全部前驱已定，直接取下界最大值
    clearance = timedelta(minutes=clearance_minutes)
    adjusted_depart = {}  # (班次 ID, 停靠序号) -> 调整后发车时刻
    departures = sorted(
        (stop["depart"], train["id"], sidx)
        for train in selected
        for sidx, stop in enumerate(train["stops"][:-1])
    )
    for orig_depart, tid, sidx in departures:
        lower = orig_depart
        if sidx == 0:
            lower += timedelta(minutes=delays.get(tid, 0))  # 首发不早于原时刻 + 指定延误
        else:
            # 站内停留不少于原定时长；同时也保证不早于原发车时刻
            arrive = adjusted_depart[(tid, sidx - 1)] + run[(tid, sidx - 1)]
            lower = max(lower, arrive + dwell[(tid, sidx)])
        pred = predecessor.get((tid, sidx))
        if pred is not None:
            lower = max(lower, adjusted_depart[pred] + run[pred] + clearance)
        adjusted_depart[(tid, sidx)] = lower

    return {
        "selected": selected,
        "delays": delays,
        "clearance": clearance,
        "run": run,
        "dwell": dwell,
        "predecessor": predecessor,
        "adjusted_depart": adjusted_depart,
        "total_stops": total_stops,
    }


def plan_delay_propagation(timetable, train_ids, initial_delays, clearance_minutes):
    """为所选班次计算首发延误沿班次与单线区间传播后的逐项最早可行排程。

    规则：
    - 所选班次首发不得早于原时刻 + 指定首发延误（initial_delays 未列的班次按 0）；
      其余各站到发不得早于原时刻；
    - 区间运行时长不变（调整后到达 = 调整后发车 + 原运行时长），站内停留不少于
      原定时长（调整后发车 >= 调整后到达 + 原停留时长）；
    - 相邻站点的无向站对视为同一单线区间；区间内各次通过的顺序按
      （原定发车时间, 班次 ID, 起点停靠序号）固定，不随延误改变；
      同一区间顺序相邻且属于不同班次的两次通过，须满足
      后车发车 >= 前车到达 + 清空间隔（同一班次的连续通过不自我约束，
      与区间冲突排查“同一班次不与自身冲突”一致，传递约束由班内时序保证）；
    - 全部约束都是下界，所求为所有到发时刻逐项最早的可行排程。

    算法：唯一未知量是各班次非末站的调整后发车时刻（到达时刻由运行时长唯一
    确定）。班内依赖（上一站发车）与区间依赖（顺序上前一次通过）在键
    （原发车时刻, 班次 ID, 停靠序号）下都严格递增——班内原发车时刻沿停靠
    单调不减、区间顺序即按该键排序——故按该键升序处理即拓扑序，每个变量
    一次取足全部下界即为逐项最早解。排程只读，不修改时刻表。

    返回 {"trains", "total_stops"}：trains 按时刻表顺序，每班含
    train_id、initial_delay_minutes、final_delay_minutes（末到延误分钟数）
    与 stops（每站原时刻与调整后时刻）。
    """
    schedule = _compute_delay_schedule(
        timetable, train_ids, initial_delays, clearance_minutes
    )
    selected = schedule["selected"]
    run = schedule["run"]
    delays = schedule["delays"]
    adjusted_depart = schedule["adjusted_depart"]

    result_trains = []
    for train in selected:
        tid = train["id"]
        stops = train["stops"]
        adjusted_stops = []
        for sidx, stop in enumerate(stops):
            arrive = (
                adjusted_depart[(tid, sidx - 1)] + run[(tid, sidx - 1)]
                if sidx > 0
                else None
            )
            depart = adjusted_depart.get((tid, sidx))
            adjusted_stops.append(
                {
                    "station": stop["station"],
                    "arrive": arrive,
                    "depart": depart,
                    "original_arrive": stop["arrive"],
                    "original_depart": stop["depart"],
                }
            )
        final_delay = int(
            (adjusted_stops[-1]["arrive"] - stops[-1]["arrive"]).total_seconds() // 60
        )
        result_trains.append(
            {
                "train_id": tid,
                "initial_delay_minutes": delays.get(tid, 0),
                "final_delay_minutes": final_delay,
                "stops": adjusted_stops,
            }
        )
    return {"trains": result_trains, "total_stops": schedule["total_stops"]}


# ---------- 晚点来源追踪（延误归因） ----------


def attribute_delay_propagation(timetable, train_ids, initial_delays, clearance_minutes):
    """为区间延误传播排程的结果追踪晚点来源：每个末到晚于原时刻的班次，
    给出末到延误分钟数、造成当前末到时刻的全部根源，以及每个根源的一条传导链。

    与 plan_delay_propagation 接收同一组输入（班次、首发延误、清空间隔），
    先在同一套下界约束下算出逐项最早排程，再从每个晚点班次的末到时刻沿约束
    回溯。根源只有两类：
    - 起作用的首发延误：某班首发下界（原时刻 + 首发延误）恰好等于排程时刻；
    - 区间冲突：原时刻表本就违反所填清空间隔——顺序相邻的两次不同班次通过中，
      后车原发车时刻早于前车原到达时刻 + 清空间隔，且该清空约束在本次排程中
      取等（缺口按原定时刻计算，不论前车本次是否晚点）。

    回溯规则（与排程约束一一对应）：
    - 班内运行：到达时刻 = 上一站调整后发车 + 原运行时长，唯一前驱；
    - 站内停留：仅当 调整后到达 + 原停留时长 恰好等于本次排程发车时刻时回溯；
    - 区间清空：仅当 前一次通过的调整后到达 + 清空间隔 恰好等于本次排程发车
      时刻时，沿固定区间顺序回溯到该次通过，并分别判断两类根源——
      原时刻表本就违反清空间隔（后车原发车早于前车原到达 + 清空间隔）时记
      区间冲突根源（缺口按原定时刻计算，与前车本次是否晚点无关）；
      前车调整后到达晚于其原定到达时，继续向前车的到达时刻回溯其晚点根源；
      两者可同时成立，根源并列保留；
    - 只回溯下界恰好等于本次排程时刻的约束；同一时刻多个约束并列（下界同时
      取等）时全部回溯，保留全部根源；同一根源经多条路径到达时只保留一条链。

    依赖键（原发车时刻, 班次 ID, 停靠序号）在回溯中严格递减，回溯必然终止。
    传导链从根源连到末到，每步标明班次、停靠序号、事件（发车/到达）、约束类型
    与完整日期时刻（调整后的排程时刻）。

    返回 {"trains", "total_stops"}：trains 按时刻表顺序，每班含 train_id、
    initial_delay_minutes、final_delay_minutes、original_final_arrive、
    final_arrive 与 roots（无晚点时为空列表）。每个 root 含 type
    （"initial_delay" 或 "section_conflict"）、根源详情与 chain（传导链）。
    分析只读，不修改时刻表。
    """
    schedule = _compute_delay_schedule(
        timetable, train_ids, initial_delays, clearance_minutes
    )
    selected = schedule["selected"]
    delays = schedule["delays"]
    clearance = schedule["clearance"]
    run = schedule["run"]
    dwell = schedule["dwell"]
    predecessor = schedule["predecessor"]
    adjusted_depart = schedule["adjusted_depart"]
    trains_by_id = {train["id"]: train for train in selected}

    def adjusted_arrive(tid, sidx):
        """调整后到达时刻（sidx >= 1）：上一站调整后发车 + 原运行时长。"""
        return adjusted_depart[(tid, sidx - 1)] + run[(tid, sidx - 1)]

    def chain_step(tid, sidx, event, constraint, moment):
        return {
            "train_id": tid,
            "stop_index": sidx,
            "station": trains_by_id[tid]["stops"][sidx]["station"],
            "event": event,
            "constraint": constraint,
            "time": moment,
        }

    # 回溯记忆化：时刻点 -> {根源 key: 从根源到该时刻点的传导链}
    memo_arrive = {}
    memo_depart = {}

    def trace_arrive(tid, sidx):
        """到达时刻的前驱唯一：上一站发车经班内运行（约束类型 run）。"""
        key = (tid, sidx)
        if key not in memo_arrive:
            moment = adjusted_arrive(tid, sidx)
            memo_arrive[key] = {
                root: chain + [chain_step(tid, sidx, "arrive", "run", moment)]
                for root, chain in trace_depart(tid, sidx - 1).items()
            }
        return memo_arrive[key]

    def trace_depart(tid, sidx):
        """发车时刻：回溯全部下界恰好等于排程时刻的约束，取根源并集。"""
        key = (tid, sidx)
        if key in memo_depart:
            return memo_depart[key]
        moment = adjusted_depart[(tid, sidx)]
        original = trains_by_id[tid]["stops"][sidx]["depart"]
        roots = {}
        if moment > original:  # 正点的时刻没有晚点需要解释
            if sidx == 0:
                delay = delays.get(tid, 0)
                if delay > 0 and original + timedelta(minutes=delay) == moment:
                    # 首发延误下界恰好等于排程时刻：根源即本班首发延误
                    roots[("initial_delay", tid)] = [
                        chain_step(tid, 0, "depart", "initial_delay", moment)
                    ]
            else:
                # 站内停留：调整后到达 + 原停留时长 == 排程发车时刻
                if adjusted_arrive(tid, sidx) + dwell[(tid, sidx)] == moment:
                    for root, chain in trace_arrive(tid, sidx).items():
                        roots.setdefault(
                            root,
                            chain + [chain_step(tid, sidx, "depart", "dwell", moment)],
                        )
            pred = predecessor.get((tid, sidx))
            if pred is not None:
                ahead_arrive = adjusted_depart[pred] + run[pred]
                if ahead_arrive + clearance == moment:
                    # 清空约束在本次排程取等：分别判断原表冲突与前车晚点
                    ptid, psidx = pred
                    ahead_original = trains_by_id[ptid]["stops"][psidx + 1]["arrive"]
                    if original < ahead_original + clearance:
                        # 原时刻表本就违反清空间隔（按原定时刻判定）：
                        # 根源是这两次通过之间的区间冲突，与前车本次是否晚点无关
                        roots.setdefault(
                            ("section_conflict", ptid, psidx, tid, sidx),
                            [
                                chain_step(ptid, psidx, "depart",
                                           "section_conflict", adjusted_depart[pred]),
                                chain_step(ptid, psidx + 1, "arrive",
                                           "run", ahead_arrive),
                                chain_step(tid, sidx, "depart",
                                           "clearance", moment),
                            ],
                        )
                    if ahead_arrive > ahead_original:
                        # 前车自身晚点：继续向前车的到达时刻回溯
                        for root, chain in trace_arrive(ptid, psidx + 1).items():
                            roots.setdefault(
                                root,
                                chain + [chain_step(tid, sidx, "depart",
                                                    "clearance", moment)],
                            )
        memo_depart[key] = roots
        return roots

    def build_root(root_key, chain):
        """由根源 key 与传导链组装根源详情。"""
        if root_key[0] == "initial_delay":
            _, tid = root_key
            first = trains_by_id[tid]["stops"][0]
            return {
                "type": "initial_delay",
                "train_id": tid,
                "stop_index": 0,
                "station": first["station"],
                "delay_minutes": delays.get(tid, 0),
                "original_depart": first["depart"],
                "adjusted_depart": adjusted_depart[(tid, 0)],
                "chain": chain,
            }
        _, ptid, psidx, ctid, csidx = root_key
        ahead_stop = trains_by_id[ptid]["stops"][psidx]
        ahead_next = trains_by_id[ptid]["stops"][psidx + 1]
        follow_stop = trains_by_id[ctid]["stops"][csidx]
        ahead_arrive = adjusted_depart[(ptid, psidx)] + run[(ptid, psidx)]
        return {
            "type": "section_conflict",
            "section": sorted((ahead_stop["station"], ahead_next["station"])),
            "clearance_minutes": int(clearance.total_seconds() // 60),
            "ahead": {
                "train_id": ptid,
                "stop_index": psidx,
                "from": ahead_stop["station"],
                "to": ahead_next["station"],
                "original_depart": ahead_stop["depart"],
                "original_arrive": ahead_next["arrive"],
                "depart": adjusted_depart[(ptid, psidx)],
                "arrive": ahead_arrive,
            },
            "follow": {
                "train_id": ctid,
                "stop_index": csidx,
                "from": follow_stop["station"],
                "to": trains_by_id[ctid]["stops"][csidx + 1]["station"],
                "original_depart": follow_stop["depart"],
                "depart": adjusted_depart[(ctid, csidx)],
            },
            # 原时刻表违反清空间隔的缺口（按原定时刻计算）：
            # 前车原到达 + 清空 - 后车原发车
            "shortfall_minutes": int(
                (ahead_next["arrive"] + clearance - follow_stop["depart"])
                .total_seconds() // 60
            ),
            "chain": chain,
        }

    def root_sort_key(root):
        head = root["chain"][0]
        if root["type"] == "initial_delay":
            ident = (root["train_id"],)
        else:
            ident = (
                root["ahead"]["train_id"], root["ahead"]["stop_index"],
                root["follow"]["train_id"], root["follow"]["stop_index"],
            )
        return (head["time"], root["type"], ident)

    result_trains = []
    for train in selected:
        tid = train["id"]
        stops = train["stops"]
        final_arrive = adjusted_arrive(tid, len(stops) - 1)
        original_final = stops[-1]["arrive"]
        final_delay = int((final_arrive - original_final).total_seconds() // 60)
        roots = []
        if final_delay > 0:
            for root_key, chain in trace_arrive(tid, len(stops) - 1).items():
                roots.append(build_root(root_key, chain))
            roots.sort(key=root_sort_key)
        result_trains.append(
            {
                "train_id": tid,
                "initial_delay_minutes": delays.get(tid, 0),
                "final_delay_minutes": final_delay,
                "original_final_arrive": original_final,
                "final_arrive": final_arrive,
                "roots": roots,
            }
        )
    return {"trains": result_trains, "total_stops": schedule["total_stops"]}
