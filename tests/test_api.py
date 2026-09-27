import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SAMPLE_TIMETABLE", "")
    application = create_app()
    application.config["TESTING"] = True
    with application.test_client() as client:
        yield client


def sample_payload():
    return {
        "stations": [
            {"id": "AAA", "name": "甲城", "transfer_minutes": 10},
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


def import_sample(client):
    return client.post("/api/import", json=sample_payload())


def test_import_and_query(client):
    res = import_sample(client)
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "stations": 3, "trains": 2}

    res = client.get("/api/timetable")
    data = res.get_json()
    assert [s["id"] for s in data["stations"]] == ["AAA", "BBB", "CCC"]
    assert len(data["trains"]) == 2

    # AAA->CCC：T1 直达 10:00；T1+T2 换乘 09:50 更早（BBB 换乘 5 分钟，09:00+5<=09:20）
    res = client.post("/api/query", json={
        "origin": "AAA", "destination": "CCC", "earliest": "2026-10-01T08:00",
    })
    data = res.get_json()
    assert data["ok"] and data["found"]
    assert data["trains"] == ["T1", "T2"]
    assert data["arrive"] == "2026-10-01T09:50"
    assert data["transfers"] == 1
    assert len(data["segments"]) == 2
    assert data["segments"][0]["wait_before_minutes"] == 0
    assert data["segments"][1]["wait_before_minutes"] == 20


def test_failed_import_keeps_old_timetable(client):
    assert import_sample(client).get_json()["ok"]

    bad = sample_payload()
    bad["trains"][0]["stops"][1]["station"] = "NOPE"  # 引用未定义站点
    res = client.post("/api/import", json=bad)
    assert res.status_code == 400
    body = res.get_json()
    assert not body["ok"]
    assert "未定义的站点" in body["error"]

    # 旧表保持不变
    data = client.get("/api/timetable").get_json()
    assert len(data["stations"]) == 3
    assert len(data["trains"]) == 2

    # 时序错误同样保留旧表
    bad2 = sample_payload()
    bad2["trains"][0]["stops"][1]["arrive"] = "2026-10-01T07:00"
    res = client.post("/api/import", json=bad2)
    assert res.status_code == 400
    assert len(client.get("/api/timetable").get_json()["trains"]) == 2


def test_import_rejects_non_json(client):
    res = client.post("/api/import", data="not json", content_type="text/plain")
    assert res.status_code == 400
    assert not res.get_json()["ok"]


def test_query_without_timetable(client):
    res = client.post("/api/query", json={
        "origin": "AAA", "destination": "CCC", "earliest": "2026-10-01T08:00",
    })
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]


def test_query_validation(client):
    import_sample(client)
    # 起终点相同
    res = client.post("/api/query", json={
        "origin": "AAA", "destination": "AAA", "earliest": "2026-10-01T08:00",
    })
    assert res.status_code == 400
    # 站点不存在
    res = client.post("/api/query", json={
        "origin": "AAA", "destination": "ZZZ", "earliest": "2026-10-01T08:00",
    })
    assert res.status_code == 400
    # 时间格式错误
    res = client.post("/api/query", json={
        "origin": "AAA", "destination": "CCC", "earliest": "2026-10-01 08:00",
    })
    assert res.status_code == 400


def test_query_not_found(client):
    import_sample(client)
    res = client.post("/api/query", json={
        "origin": "CCC", "destination": "AAA", "earliest": "2026-10-01T08:00",
    })
    data = res.get_json()
    assert data["ok"] and not data["found"]
    assert "未找到" in data["message"]


def test_query_first_train_not_before_earliest(client):
    import_sample(client)
    res = client.post("/api/query", json={
        "origin": "AAA", "destination": "CCC", "earliest": "2026-10-01T08:01",
    })
    data = res.get_json()
    assert data["ok"] and not data["found"]


def revisit_payload():
    """T 回访站点 B（B→D→B）；Q 从 D 到 E。"""
    return {
        "stations": [
            {"id": "B", "name": "北站", "transfer_minutes": 0},
            {"id": "D", "name": "东站", "transfer_minutes": 2},
            {"id": "E", "name": "南站", "transfer_minutes": 0},
        ],
        "trains": [
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
    }


def test_query_revisited_station_no_time_travel(client):
    assert client.post("/api/import", json=revisit_payload()).get_json()["ok"]

    # T 的 D→B（10:05→11:00）不能接上它早已发出的 B→D（09:05→10:00）再换 Q：
    # 查询 D→E、最早出发 10:05 应返回无可乘行程
    res = client.post("/api/query", json={
        "origin": "D", "destination": "E", "earliest": "2026-10-01T10:05",
    })
    data = res.get_json()
    assert data["ok"] and not data["found"]
    assert "未找到" in data["message"]

    # 合法行程不受影响：B→E 乘 T 到 D 换 Q，各段到发与等待时间连续且可乘
    res = client.post("/api/query", json={
        "origin": "B", "destination": "E", "earliest": "2026-10-01T09:00",
    })
    data = res.get_json()
    assert data["ok"] and data["found"]
    assert data["trains"] == ["T", "Q"]
    assert data["transfers"] == 1
    assert data["depart"] == "2026-10-01T09:05"
    assert data["arrive"] == "2026-10-01T10:30"
    segs = data["segments"]
    assert [(s["train"], s["from"], s["to"]) for s in segs] == [
        ("T", "B", "D"), ("Q", "D", "E"),
    ]
    assert segs[0]["depart"] == "2026-10-01T09:05"
    assert segs[0]["arrive"] == "2026-10-01T10:00"
    assert segs[0]["wait_before_minutes"] == 5  # 相对最早出发 09:00 候车 5 分钟
    assert segs[1]["depart"] == "2026-10-01T10:03"
    assert segs[1]["arrive"] == "2026-10-01T10:30"
    assert segs[1]["wait_before_minutes"] == 3
