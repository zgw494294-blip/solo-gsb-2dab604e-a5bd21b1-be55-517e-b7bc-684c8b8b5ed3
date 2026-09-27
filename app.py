"""Flask 应用：时刻表导入、行程查询与页面服务。"""
import json
import os
import sqlite3
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory

import planner

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    transfer_minutes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trains (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stops (
    train_id TEXT NOT NULL REFERENCES trains(id),
    seq INTEGER NOT NULL,
    station_id TEXT NOT NULL REFERENCES stations(id),
    arrive TEXT,
    depart TEXT,
    PRIMARY KEY (train_id, seq)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timetable_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    stations_count INTEGER NOT NULL,
    trains_count INTEGER NOT NULL,
    snapshot TEXT NOT NULL
);
"""


def serialize_timetable(tt):
    """把内部时刻表结构序列化为导入格式的 JSON 兼容字典（时间转为字符串）。"""
    return {
        "stations": [
            {"id": sid, "name": st["name"], "transfer_minutes": st["transfer_minutes"]}
            for sid, st in tt["stations"].items()
        ],
        "trains": [
            {
                "id": train["id"],
                "stops": [
                    {
                        "station": stop["station"],
                        "arrive": planner.format_time(stop["arrive"]) if stop["arrive"] else None,
                        "depart": planner.format_time(stop["depart"]) if stop["depart"] else None,
                    }
                    for stop in train["stops"]
                ],
            }
            for train in tt["trains"]
        ],
    }


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config["DB_PATH"] = os.environ.get(
        "DB_PATH", os.path.join(BASE_DIR, "data", "timetable.db")
    )
    app.config["SAMPLE_TIMETABLE"] = os.environ.get(
        "SAMPLE_TIMETABLE", os.path.join(BASE_DIR, "sample_data", "timetable.json")
    )
    app.json.ensure_ascii = False

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB_PATH"])
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def load_timetable():
        db = get_db()
        stations = {}
        for row in db.execute("SELECT id, name, transfer_minutes FROM stations ORDER BY rowid"):
            stations[row["id"]] = {
                "name": row["name"],
                "transfer_minutes": row["transfer_minutes"],
            }
        trains = []
        for trow in db.execute("SELECT id FROM trains ORDER BY seq"):
            stops = [
                {
                    "station": srow["station_id"],
                    "arrive": planner.parse_time(srow["arrive"]) if srow["arrive"] else None,
                    "depart": planner.parse_time(srow["depart"]) if srow["depart"] else None,
                }
                for srow in db.execute(
                    "SELECT station_id, arrive, depart FROM stops WHERE train_id = ? ORDER BY seq",
                    (trow["id"],),
                )
            ]
            trains.append({"id": trow["id"], "stops": stops})
        return {"stations": stations, "trains": trains}

    def replace_live_timetable(db, tt, moment):
        """在当前事务内整体替换现役时刻表（stations/trains/stops）与 imported_at。"""
        db.execute("DELETE FROM stops")
        db.execute("DELETE FROM trains")
        db.execute("DELETE FROM stations")
        for sid, st in tt["stations"].items():
            db.execute(
                "INSERT INTO stations (id, name, transfer_minutes) VALUES (?, ?, ?)",
                (sid, st["name"], st["transfer_minutes"]),
            )
        for tidx, train in enumerate(tt["trains"]):
            db.execute("INSERT INTO trains (id, seq) VALUES (?, ?)", (train["id"], tidx))
            for sidx, stop in enumerate(train["stops"]):
                db.execute(
                    "INSERT INTO stops (train_id, seq, station_id, arrive, depart)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        train["id"],
                        sidx,
                        stop["station"],
                        planner.format_time(stop["arrive"]) if stop["arrive"] else None,
                        planner.format_time(stop["depart"]) if stop["depart"] else None,
                    ),
                )
        db.execute(
            "INSERT INTO meta (key, value) VALUES ('imported_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (moment,),
        )

    def save_timetable(tt):
        """保存新时刻表：同一事务内写入不可变快照、替换现役表并设为当前版本。

        任何失败都会回滚，旧表与旧版本指针保持不变，也不会留下新版本。
        """
        db = get_db()
        try:
            moment = datetime.now().isoformat(timespec="seconds")
            replace_live_timetable(db, tt, moment)
            cur = db.execute(
                "INSERT INTO timetable_versions"
                " (created_at, stations_count, trains_count, snapshot)"
                " VALUES (?, ?, ?, ?)",
                (
                    moment,
                    len(tt["stations"]),
                    len(tt["trains"]),
                    json.dumps(serialize_timetable(tt), ensure_ascii=False),
                ),
            )
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('current_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(cur.lastrowid),),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/timetable")
    def api_timetable():
        tt = load_timetable()
        row = get_db().execute("SELECT value FROM meta WHERE key = 'imported_at'").fetchone()
        return jsonify(
            {
                "imported_at": row["value"] if row else None,
                "stations": [
                    {"id": sid, "name": st["name"], "transfer_minutes": st["transfer_minutes"]}
                    for sid, st in tt["stations"].items()
                ],
                "trains": [
                    {
                        "id": train["id"],
                        "stops": [
                            {
                                "station": stop["station"],
                                "arrive": planner.format_time(stop["arrive"])
                                if stop["arrive"]
                                else None,
                                "depart": planner.format_time(stop["depart"])
                                if stop["depart"]
                                else None,
                            }
                            for stop in train["stops"]
                        ],
                    }
                    for train in tt["trains"]
                ],
            }
        )

    @app.get("/api/sample")
    def api_sample():
        sample = app.config["SAMPLE_TIMETABLE"]
        if not sample or not os.path.exists(sample):
            return jsonify({"ok": False, "error": "示例数据文件不存在"}), 404
        with open(sample, encoding="utf-8") as fh:
            return jsonify(json.load(fh))

    @app.post("/api/import")
    def api_import():
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"ok": False, "error": "请求体必须是 JSON 时刻表"}), 400
        try:
            tt = planner.validate_timetable(data)
        except planner.TimetableError as exc:
            # 校验失败：不触碰数据库，保留旧表
            return jsonify({"ok": False, "error": str(exc)}), 400
        try:
            save_timetable(tt)
        except Exception as exc:  # 写入失败回滚，旧表保留
            app.logger.exception("导入时刻表失败")
            return jsonify({"ok": False, "error": f"写入数据库失败，已保留旧时刻表：{exc}"}), 500
        return jsonify({"ok": True, "stations": len(tt["stations"]), "trains": len(tt["trains"])})

    def _current_version(db):
        row = db.execute(
            "SELECT value FROM meta WHERE key = 'current_version'"
        ).fetchone()
        return int(row["value"]) if row and row["value"] else None

    @app.get("/api/timetable-versions")
    def api_timetable_versions():
        db = get_db()
        current = _current_version(db)
        versions = [
            {
                "version": row["version"],
                "created_at": row["created_at"],
                "stations": row["stations_count"],
                "trains": row["trains_count"],
                "is_current": row["version"] == current,
            }
            for row in db.execute(
                "SELECT version, created_at, stations_count, trains_count"
                " FROM timetable_versions ORDER BY version"
            )
        ]
        return jsonify({"current_version": current, "versions": versions})

    @app.post("/api/timetable-versions/<int:version>/restore")
    def api_restore_version(version):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "expected_version" not in data:
            return (
                jsonify({"ok": False, "error": "请求体必须包含 expected_version（页面读取时的当前版本号）"}),
                400,
            )
        expected = data.get("expected_version")
        if isinstance(expected, bool) or not isinstance(expected, int):
            return jsonify({"ok": False, "error": "expected_version 必须是整数版本号"}), 400

        db = get_db()
        try:
            # 立即取得写锁，在同一事务内核对当前版本、写快照、替换现役表，
            # 保证乐观锁检查与恢复落库之间不会被其他导入/恢复插入。
            db.execute("BEGIN IMMEDIATE")
            current = _current_version(db)
            if current != expected:
                db.rollback()
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": (
                                f"当前版本已变为 {current}（页面读取时为 {expected}），"
                                "请刷新版本列表后重试"
                            ),
                            "current_version": current,
                        }
                    ),
                    409,
                )
            row = db.execute(
                "SELECT snapshot FROM timetable_versions WHERE version = ?",
                (version,),
            ).fetchone()
            if row is None:
                db.rollback()
                return jsonify({"ok": False, "error": f"版本 {version} 不存在"}), 404
            try:
                tt = planner.validate_timetable(json.loads(row["snapshot"]))
            except (ValueError, planner.TimetableError) as exc:
                db.rollback()
                app.logger.exception("版本 %s 快照损坏", version)
                return jsonify({"ok": False, "error": f"版本 {version} 快照无法恢复：{exc}"}), 500
            moment = datetime.now().isoformat(timespec="seconds")
            replace_live_timetable(db, tt, moment)
            cur = db.execute(
                "INSERT INTO timetable_versions"
                " (created_at, stations_count, trains_count, snapshot)"
                " VALUES (?, ?, ?, ?)",
                (moment, len(tt["stations"]), len(tt["trains"]), row["snapshot"]),
            )
            new_version = cur.lastrowid
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('current_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(new_version),),
            )
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception("恢复版本失败")
            return jsonify({"ok": False, "error": "恢复失败，数据未改动"}), 500
        return jsonify(
            {
                "ok": True,
                "restored_from": version,
                "version": new_version,
                "stations": len(tt["stations"]),
                "trains": len(tt["trains"]),
            }
        )

    @app.post("/api/query")
    def api_query():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        origin = data.get("origin")
        destination = data.get("destination")
        earliest_raw = data.get("earliest")
        tt = load_timetable()
        if not tt["stations"]:
            return jsonify({"ok": False, "error": "尚未导入时刻表，请先导入"}), 400
        if origin not in tt["stations"]:
            return jsonify({"ok": False, "error": f"起点站 {origin!r} 不存在"}), 400
        if destination not in tt["stations"]:
            return jsonify({"ok": False, "error": f"终点站 {destination!r} 不存在"}), 400
        if origin == destination:
            return jsonify({"ok": False, "error": "起点和终点不能相同"}), 400
        try:
            earliest = planner.parse_time(earliest_raw, "earliest")
        except planner.TimetableError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        result = planner.find_itinerary(tt, origin, destination, earliest)
        if result is None:
            return jsonify(
                {
                    "ok": True,
                    "found": False,
                    "message": (
                        f"未找到可乘行程：没有在不早于 {planner.format_time(earliest)} 出发、"
                        f"且满足各站换乘时间要求的从 {origin} 到 {destination} 的行程。"
                    ),
                }
            )

        segments = []
        prev_arrive = None
        for seg in result["segments"]:
            wait_from = prev_arrive if prev_arrive is not None else earliest
            segments.append(
                {
                    "train": seg["train"],
                    "from": seg["from"],
                    "to": seg["to"],
                    "depart": planner.format_time(seg["depart"]),
                    "arrive": planner.format_time(seg["arrive"]),
                    "wait_before_minutes": int(
                        (seg["depart"] - wait_from).total_seconds() // 60
                    ),
                }
            )
            prev_arrive = seg["arrive"]
        return jsonify(
            {
                "ok": True,
                "found": True,
                "origin": origin,
                "destination": destination,
                "depart": planner.format_time(result["depart"]),
                "arrive": planner.format_time(result["arrive"]),
                "duration_minutes": int(
                    (result["arrive"] - result["depart"]).total_seconds() // 60
                ),
                "transfers": result["transfers"],
                "trains": result["train_sequence"],
                "segments": segments,
            }
        )

    @app.post("/api/platform-plan")
    def api_platform_plan():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        tt = load_timetable()
        if not tt["stations"]:
            return jsonify({"ok": False, "error": "尚未导入时刻表，请先导入"}), 400
        station_id = data.get("station_id")
        platform_count = data.get("platform_count")
        try:
            assignments = planner.plan_platforms(
                tt, station_id, platform_count, data.get("locks")
            )
        except planner.TimetableError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if assignments is None:
            return jsonify(
                {
                    "ok": True,
                    "feasible": False,
                    "message": (
                        f"无解：站点 {station_id} 的停靠在 {platform_count} 个站台下"
                        "无法完成分配（含预锁约束），请增加站台或调整预锁。"
                    ),
                }
            )
        return jsonify(
            {
                "ok": True,
                "feasible": True,
                "station": station_id,
                "platform_count": platform_count,
                "assignments": [
                    {
                        "train_id": item["train_id"],
                        "stop_index": item["stop_index"],
                        "platform": item["platform"],
                        "start": planner.format_time(item["start"]),
                        "end": planner.format_time(item["end"]),
                    }
                    for item in assignments
                ],
            }
        )

    @app.post("/api/section-conflicts")
    def api_section_conflicts():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        tt = load_timetable()
        if not tt["stations"]:
            return jsonify({"ok": False, "error": "尚未导入时刻表，请先导入"}), 400
        try:
            result = planner.analyze_section_conflicts(
                tt,
                data.get("station_a"),
                data.get("station_b"),
                data.get("clearance_minutes"),
            )
        except planner.TimetableError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        def passage_json(p):
            return {
                "train_id": p["train_id"],
                "stop_index": p["stop_index"],
                "from": p["from"],
                "to": p["to"],
                "direction": f"{p['from']}→{p['to']}",
                "start": planner.format_time(p["start"]),
                "end": planner.format_time(p["end"]),
            }

        return jsonify(
            {
                "ok": True,
                "station_a": data["station_a"],
                "station_b": data["station_b"],
                "clearance_minutes": data["clearance_minutes"],
                "passage_count": len(result["passages"]),
                "conflicts": [
                    {
                        "first": passage_json(pair["first"]),
                        "second": passage_json(pair["second"]),
                    }
                    for pair in result["conflicts"]
                ],
                "suspended": result["suspended"],
            }
        )

    @app.post("/api/rolling-stock-plan")
    def api_rolling_stock_plan():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        tt = load_timetable()
        if not tt["stations"]:
            return jsonify({"ok": False, "error": "尚未导入时刻表，请先导入"}), 400
        try:
            result = planner.plan_rolling_stock(
                tt, data.get("train_ids"), data.get("turnaround_minutes")
            )
        except planner.TimetableError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        def train_json(item):
            return {
                "train_id": item["train_id"],
                "origin": item["origin"],
                "destination": item["destination"],
                "depart": planner.format_time(item["depart"]),
                "arrive": planner.format_time(item["arrive"]),
            }

        return jsonify(
            {
                "ok": True,
                "train_count": sum(len(chain["trains"]) for chain in result["chains"]),
                "turnaround_minutes": data.get("turnaround_minutes"),
                "group_count": result["group_count"],
                "total_wait_minutes": result["total_wait_minutes"],
                "chains": [
                    {
                        "trains": [train_json(item) for item in chain["trains"]],
                        "connections": chain["connections"],
                    }
                    for chain in result["chains"]
                ],
            }
        )

    @app.post("/api/passenger-flow")
    def api_passenger_flow():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        tt = load_timetable()
        if not tt["stations"]:
            return jsonify({"ok": False, "error": "尚未导入时刻表，请先导入"}), 400
        origin = data.get("origin")
        destination = data.get("destination")
        if not isinstance(origin, str) or origin not in tt["stations"]:
            return jsonify({"ok": False, "error": f"起点站 {origin!r} 不存在"}), 400
        if not isinstance(destination, str) or destination not in tt["stations"]:
            return jsonify({"ok": False, "error": f"终点站 {destination!r} 不存在"}), 400
        if origin == destination:
            return jsonify({"ok": False, "error": "起点和终点不能相同"}), 400
        try:
            earliest = planner.parse_time(data.get("earliest"), "earliest")
            deadline = planner.parse_time(data.get("deadline"), "deadline")
        except planner.TimetableError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if earliest >= deadline:
            return jsonify(
                {"ok": False, "error": "earliest 必须严格早于 deadline（按完整日期比较）"}
            ), 400
        try:
            result = planner.plan_passenger_flow(
                tt, origin, destination, earliest, deadline, data.get("capacities")
            )
        except planner.TimetableError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        return jsonify(
            {
                "ok": True,
                "origin": origin,
                "destination": destination,
                "earliest": planner.format_time(earliest),
                "deadline": planner.format_time(deadline),
                "total": result["total"],
                "itineraries": [
                    {
                        "people": item["people"],
                        "depart": planner.format_time(item["depart"]),
                        "arrive": planner.format_time(item["arrive"]),
                        "transfers": item["transfers"],
                        "segments": [
                            {
                                "train_id": seg["train_id"],
                                "board_index": seg["board_index"],
                                "alight_index": seg["alight_index"],
                                "from": seg["from"],
                                "to": seg["to"],
                                "depart": planner.format_time(seg["depart"]),
                                "arrive": planner.format_time(seg["arrive"]),
                            }
                            for seg in item["segments"]
                        ],
                    }
                    for item in result["itineraries"]
                ],
            }
        )

    @app.post("/api/delay-plan")
    def api_delay_plan():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        tt = load_timetable()
        if not tt["stations"]:
            return jsonify({"ok": False, "error": "尚未导入时刻表，请先导入"}), 400
        try:
            result = planner.plan_delay_propagation(
                tt,
                data.get("train_ids"),
                data.get("initial_delays"),
                data.get("clearance_minutes"),
            )
        except planner.TimetableError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        def stop_json(stop):
            return {
                "station": stop["station"],
                "arrive": planner.format_time(stop["arrive"]) if stop["arrive"] else None,
                "depart": planner.format_time(stop["depart"]) if stop["depart"] else None,
                "original_arrive": planner.format_time(stop["original_arrive"])
                if stop["original_arrive"]
                else None,
                "original_depart": planner.format_time(stop["original_depart"])
                if stop["original_depart"]
                else None,
            }

        return jsonify(
            {
                "ok": True,
                "clearance_minutes": data["clearance_minutes"],
                "train_count": len(result["trains"]),
                "total_stops": result["total_stops"],
                "trains": [
                    {
                        "train_id": item["train_id"],
                        "initial_delay_minutes": item["initial_delay_minutes"],
                        "final_delay_minutes": item["final_delay_minutes"],
                        "stops": [stop_json(stop) for stop in item["stops"]],
                    }
                    for item in result["trains"]
                ],
            }
        )

    @app.post("/api/delay-attribution")
    def api_delay_attribution():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        tt = load_timetable()
        if not tt["stations"]:
            return jsonify({"ok": False, "error": "尚未导入时刻表，请先导入"}), 400
        try:
            result = planner.attribute_delay_propagation(
                tt,
                data.get("train_ids"),
                data.get("initial_delays"),
                data.get("clearance_minutes"),
            )
        except planner.TimetableError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        def chain_step_json(step):
            return {
                "train_id": step["train_id"],
                "stop_index": step["stop_index"],
                "station": step["station"],
                "event": step["event"],
                "constraint": step["constraint"],
                "time": planner.format_time(step["time"]),
            }

        def passage_json(passage):
            return {
                "train_id": passage["train_id"],
                "stop_index": passage["stop_index"],
                "from": passage["from"],
                "to": passage["to"],
                "original_depart": planner.format_time(passage["original_depart"]),
                "original_arrive": planner.format_time(passage["original_arrive"]),
                "depart": planner.format_time(passage["depart"]),
                "arrive": planner.format_time(passage["arrive"]),
            }

        def root_json(root):
            out = {
                "type": root["type"],
                "chain": [chain_step_json(step) for step in root["chain"]],
            }
            if root["type"] == "initial_delay":
                out.update(
                    {
                        "train_id": root["train_id"],
                        "stop_index": root["stop_index"],
                        "station": root["station"],
                        "delay_minutes": root["delay_minutes"],
                        "original_depart": planner.format_time(root["original_depart"]),
                        "adjusted_depart": planner.format_time(root["adjusted_depart"]),
                    }
                )
            else:
                out.update(
                    {
                        "section": root["section"],
                        "clearance_minutes": root["clearance_minutes"],
                        "shortfall_minutes": root["shortfall_minutes"],
                        "ahead": passage_json(root["ahead"]),
                        "follow": {
                            "train_id": root["follow"]["train_id"],
                            "stop_index": root["follow"]["stop_index"],
                            "from": root["follow"]["from"],
                            "to": root["follow"]["to"],
                            "original_depart": planner.format_time(
                                root["follow"]["original_depart"]
                            ),
                            "depart": planner.format_time(root["follow"]["depart"]),
                        },
                    }
                )
            return out

        return jsonify(
            {
                "ok": True,
                "clearance_minutes": data["clearance_minutes"],
                "train_count": len(result["trains"]),
                "total_stops": result["total_stops"],
                "trains": [
                    {
                        "train_id": item["train_id"],
                        "initial_delay_minutes": item["initial_delay_minutes"],
                        "final_delay_minutes": item["final_delay_minutes"],
                        "original_final_arrive": planner.format_time(
                            item["original_final_arrive"]
                        ),
                        "final_arrive": planner.format_time(item["final_arrive"]),
                        "roots": [root_json(root) for root in item["roots"]],
                    }
                    for item in result["trains"]
                ],
            }
        )

    # 初始化数据库；为空时尝试导入示例数据，方便直接体验
    db_dir = os.path.dirname(app.config["DB_PATH"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        db.commit()
        count = db.execute("SELECT COUNT(*) AS c FROM stations").fetchone()["c"]
        has_versions = db.execute(
            "SELECT COUNT(*) AS c FROM timetable_versions"
        ).fetchone()["c"]
        if count > 0 and has_versions == 0:
            # 升级旧库：把已有现役时刻表登记为不可变的首个版本
            tt = load_timetable()
            imported = db.execute(
                "SELECT value FROM meta WHERE key = 'imported_at'"
            ).fetchone()
            moment = imported["value"] if imported else datetime.now().isoformat(timespec="seconds")
            cur = db.execute(
                "INSERT INTO timetable_versions"
                " (created_at, stations_count, trains_count, snapshot)"
                " VALUES (?, ?, ?, ?)",
                (
                    moment,
                    len(tt["stations"]),
                    len(tt["trains"]),
                    json.dumps(serialize_timetable(tt), ensure_ascii=False),
                ),
            )
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('current_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(cur.lastrowid),),
            )
            db.commit()
            app.logger.info("已把现有时刻表登记为首个版本 v%s", cur.lastrowid)
        sample = app.config["SAMPLE_TIMETABLE"]
        if count == 0 and sample and os.path.exists(sample):
            with open(sample, encoding="utf-8") as fh:
                tt = planner.validate_timetable(json.load(fh))
            save_timetable(tt)
            app.logger.info("已导入示例时刻表：%s", sample)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
