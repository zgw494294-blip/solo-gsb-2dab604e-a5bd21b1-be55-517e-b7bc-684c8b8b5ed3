"""时刻表历史版本与恢复的 API 测试。"""
import os

import pytest

from app import create_app


def make_app(db_path, sample=""):
    os.environ["DB_PATH"] = str(db_path)
    os.environ["SAMPLE_TIMETABLE"] = sample
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def client(db_path):
    app = make_app(db_path)
    with app.test_client() as c:
        yield c


def payload_v1():
    return {
        "stations": [
            {"id": "AAA", "name": "甲城", "transfer_minutes": 10},
            {"id": "BBB", "name": "乙城", "transfer_minutes": 5},
        ],
        "trains": [
            {"id": "T1", "stops": [
                {"station": "AAA", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "BBB", "arrive": "2026-10-01T09:00", "depart": None},
            ]},
        ],
    }


def payload_v2():
    return {
        "stations": [
            {"id": "AAA", "name": "甲城", "transfer_minutes": 2},
            {"id": "BBB", "name": "乙城", "transfer_minutes": 5},
            {"id": "CCC", "name": "丙城", "transfer_minutes": 0},
        ],
        "trains": [
            {"id": "T1", "stops": [
                {"station": "AAA", "arrive": None, "depart": "2026-10-01T08:00"},
                {"station": "BBB", "arrive": "2026-10-01T09:00", "depart": "2026-10-01T09:10"},
                {"station": "CCC", "arrive": "2026-10-01T10:00", "depart": None},
            ]},
            {"id": "T2", "stops": [
                {"station": "BBB", "arrive": None, "depart": "2026-10-01T09:20"},
                {"station": "CCC", "arrive": "2026-10-01T09:50", "depart": None},
            ]},
        ],
    }


def test_no_versions_before_import(client):
    data = client.get("/api/timetable-versions").get_json()
    assert data == {"current_version": None, "versions": []}


def test_successful_imports_create_incremental_versions(client):
    assert client.post("/api/import", json=payload_v1()).get_json()["ok"]
    assert client.post("/api/import", json=payload_v2()).get_json()["ok"]

    data = client.get("/api/timetable-versions").get_json()
    assert data["current_version"] == 2
    versions = data["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert [(v["stations"], v["trains"]) for v in versions] == [(2, 1), (3, 2)]
    assert [v["is_current"] for v in versions] == [False, True]
    assert versions[0]["created_at"]
    # 快照不可变：v1 仍是 2 站 1 班的内容
    data = client.get("/api/timetable").get_json()
    assert len(data["stations"]) == 3

    # 导入失败（校验不通过）不产生新版本，当前版本不变
    bad = payload_v2()
    bad["trains"][0]["stops"][1]["station"] = "NOPE"
    res = client.post("/api/import", json=bad)
    assert res.status_code == 400
    data = client.get("/api/timetable-versions").get_json()
    assert data["current_version"] == 2
    assert [v["version"] for v in data["versions"]] == [1, 2]
    # 现役时刻表也未被改动
    assert len(client.get("/api/timetable").get_json()["stations"]) == 3


def test_restore_creates_new_version_and_takes_effect(client):
    client.post("/api/import", json=payload_v1())
    client.post("/api/import", json=payload_v2())

    res = client.post(
        "/api/timetable-versions/1/restore",
        json={"expected_version": 2},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"]
    assert body["restored_from"] == 1
    assert body["version"] == 3
    assert body["stations"] == 2 and body["trains"] == 1

    data = client.get("/api/timetable-versions").get_json()
    assert data["current_version"] == 3
    assert [v["version"] for v in data["versions"]] == [1, 2, 3]
    assert [v["is_current"] for v in data["versions"]] == [False, False, True]
    # v3 的统计与 v1 相同（同一快照内容）
    v1, v3 = data["versions"][0], data["versions"][2]
    assert (v3["stations"], v3["trains"]) == (v1["stations"], v1["trains"])

    # 行程查询立即使用恢复后的时刻表：CCC 已不存在
    tt = client.get("/api/timetable").get_json()
    assert {s["id"] for s in tt["stations"]} == {"AAA", "BBB"}
    res = client.post("/api/query", json={
        "origin": "AAA", "destination": "CCC", "earliest": "2026-10-01T08:00",
    })
    assert res.status_code == 400

    # 旧版仍可再次恢复（v2 -> v4）
    res = client.post(
        "/api/timetable-versions/2/restore",
        json={"expected_version": 3},
    )
    assert res.status_code == 200
    assert res.get_json()["version"] == 4
    data = client.get("/api/timetable-versions").get_json()
    assert data["current_version"] == 4
    assert [v["version"] for v in data["versions"]] == [1, 2, 3, 4]
    assert len(client.get("/api/timetable").get_json()["stations"]) == 3
    # 恢复后站台分配同样基于恢复后的时刻表
    res = client.post("/api/platform-plan", json={
        "station_id": "CCC", "platform_count": 2,
    })
    assert res.get_json()["ok"] and res.get_json()["feasible"]


def test_restore_conflict_returns_409_and_changes_nothing(client):
    client.post("/api/import", json=payload_v1())
    client.post("/api/import", json=payload_v2())

    # 页面读取时是 v2，但当前版本已变成 4 -> 409
    client.post("/api/timetable-versions/1/restore", json={"expected_version": 2})
    res = client.post(
        "/api/timetable-versions/1/restore",
        json={"expected_version": 2},
    )
    assert res.status_code == 409
    body = res.get_json()
    assert not body["ok"]
    assert body["current_version"] == 3

    # 数据不变：仍指向 v3（v1 内容），且没有新增版本
    data = client.get("/api/timetable-versions").get_json()
    assert data["current_version"] == 3
    assert [v["version"] for v in data["versions"]] == [1, 2, 3]
    assert len(client.get("/api/timetable").get_json()["stations"]) == 2


def test_restore_requires_expected_version(client):
    client.post("/api/import", json=payload_v1())
    assert client.post("/api/timetable-versions/1/restore", json={}).status_code == 400
    assert client.post(
        "/api/timetable-versions/1/restore", json={"expected_version": "1"}
    ).status_code == 400
    # 失败不产生版本
    data = client.get("/api/timetable-versions").get_json()
    assert data["current_version"] == 1
    assert [v["version"] for v in data["versions"]] == [1]


def test_restore_unknown_version_returns_404(client):
    client.post("/api/import", json=payload_v1())
    res = client.post(
        "/api/timetable-versions/99/restore", json={"expected_version": 1}
    )
    assert res.status_code == 404
    data = client.get("/api/timetable-versions").get_json()
    assert data["current_version"] == 1
    assert [v["version"] for v in data["versions"]] == [1]


def test_versions_persist_across_restart(db_path):
    app = make_app(db_path)
    with app.test_client() as c:
        c.post("/api/import", json=payload_v1())
        c.post("/api/import", json=payload_v2())
        c.post("/api/timetable-versions/1/restore", json={"expected_version": 2})
        assert c.get("/api/timetable-versions").get_json()["current_version"] == 3

    # 模拟重启：同一 DB_PATH 重新创建应用
    app = make_app(db_path)
    with app.test_client() as c:
        data = c.get("/api/timetable-versions").get_json()
        assert data["current_version"] == 3
        assert [v["version"] for v in data["versions"]] == [1, 2, 3]
        assert [(v["stations"], v["trains"]) for v in data["versions"]] == [
            (2, 1), (3, 2), (2, 1),
        ]
        assert [v["is_current"] for v in data["versions"]] == [False, False, True]
        # 现役时刻表内容同样不丢
        tt = c.get("/api/timetable").get_json()
        assert {s["id"] for s in tt["stations"]} == {"AAA", "BBB"}


def test_legacy_timetable_registered_as_first_version(db_path):
    """旧版本库（现役时刻表、无版本记录）启动后登记为 v1。"""
    import sqlite3

    os.environ["DB_PATH"] = str(db_path)
    os.environ["SAMPLE_TIMETABLE"] = ""
    # 先用旧 schema 建库并写入一份现役时刻表
    with sqlite3.connect(db_path) as db:
        db.executescript("""
        CREATE TABLE stations (id TEXT PRIMARY KEY, name TEXT NOT NULL, transfer_minutes INTEGER NOT NULL);
        CREATE TABLE trains (id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
        CREATE TABLE stops (
            train_id TEXT NOT NULL REFERENCES trains(id), seq INTEGER NOT NULL,
            station_id TEXT NOT NULL REFERENCES stations(id), arrive TEXT, depart TEXT,
            PRIMARY KEY (train_id, seq)
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        db.execute("INSERT INTO stations VALUES ('AAA', '甲城', 10)")
        db.execute("INSERT INTO stations VALUES ('BBB', '乙城', 5)")
        db.execute("INSERT INTO trains VALUES ('T1', 0)")
        db.execute("INSERT INTO stops VALUES ('T1', 0, 'AAA', NULL, '2026-10-01T08:00')")
        db.execute("INSERT INTO stops VALUES ('T1', 1, 'BBB', '2026-10-01T09:00', NULL)")
        db.execute("INSERT INTO meta VALUES ('imported_at', '2026-05-01T10:00:00')")
        db.commit()

    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        data = c.get("/api/timetable-versions").get_json()
        assert data["current_version"] == 1
        assert len(data["versions"]) == 1
        v = data["versions"][0]
        assert v["version"] == 1 and v["is_current"]
        assert v["stations"] == 2 and v["trains"] == 1
        assert v["created_at"] == "2026-05-01T10:00:00"

        # 之后正常导入产生递增的 v2
        assert c.post("/api/import", json=payload_v2()).get_json()["ok"]
        data = c.get("/api/timetable-versions").get_json()
        assert data["current_version"] == 2
        assert [x["version"] for x in data["versions"]] == [1, 2]

        # 旧版可恢复
        res = c.post("/api/timetable-versions/1/restore", json={"expected_version": 2})
        assert res.status_code == 200
        assert res.get_json()["version"] == 3
        tt = c.get("/api/timetable").get_json()
        assert {s["id"] for s in tt["stations"]} == {"AAA", "BBB"}

    # 重启不会重复登记 v1
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        versions = c.get("/api/timetable-versions").get_json()["versions"]
        assert [v["version"] for v in versions] == [1, 2, 3]
