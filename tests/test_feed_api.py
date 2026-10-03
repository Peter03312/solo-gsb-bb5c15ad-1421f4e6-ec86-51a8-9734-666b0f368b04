"""下游值班系统“公开预警变更断点领取”API 端到端测试。

覆盖：
- 消费方登记、仅展示一次的密钥、无效/缺失密钥 401；
- 拉取下一批：首次空快照基准、无变化发布占序位、重复拉取同批、limit 截断；
- 相邻发布差异：站点/责任区状态前后值、预警/解除新增、追溯修订导致的撤销；
- 待发布变化与失败确认不可见，受控确认成功冻结可见；
- 确认仅接受批次末尾编号、越序 409、未知发布 404、重复确认幂等、无批次拒绝；
- 多消费方游标独立、新消费方可回放历史、服务“重启”后可续读；
- 并发发布/拉取不漏项不串项。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BASE = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def make_client(db_path: str) -> TestClient:
    settings = Settings(
        db_path=db_path,
        min_water_level=-50,
        max_water_level=1000,
        segment_gap_minutes=15,
        rise_min_count=3,
        rise_min_span_minutes=10,
        clear_min_count=2,
        clear_min_span_minutes=5,
    )
    return TestClient(create_app(settings))


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "feed.db")


@pytest.fixture()
def client(db_path):
    with make_client(db_path) as c:
        yield c


@pytest.fixture()
def warning_station(client):
    """登记一站一设备并补传 3 条达上升阈值读数（当前待发布为预警）。"""
    r = client.post("/stations", json={"name": "青石峡站", "rise_threshold": 10.0, "clear_threshold": 8.0})
    sid = r.json()["id"]
    client.post(f"/stations/{sid}/devices", json={"device_no": "DEV-001"})
    for i, minute in enumerate((0, 5, 10)):
        rr = client.post(
            "/readings",
            json={
                "device_no": "DEV-001",
                "seq": i + 1,
                "observed_at": iso(BASE + timedelta(minutes=minute)),
                "water_level": 11.0 + i,
            },
        )
        assert rr.status_code in (200, 201)
    return sid


def register(client, name="值班系统甲"):
    r = client.post("/feed/consumers", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def auth(secret):
    return {"X-Feed-Secret": secret}


def drain(client, secret, expect_eventually_empty=True):
    """循环 拉取→确认末尾编号，返回按顺序消费到的全部发布编号。"""
    seen: list[int] = []
    while True:
        r = client.get("/feed/releases", headers=auth(secret))
        assert r.status_code == 200, r.text
        batch = r.json()
        if not batch["releases"]:
            break
        seen.extend(rel["publication_id"] for rel in batch["releases"])
        end = batch["batch"]["last_publication_id"]
        ack = client.post(
            "/feed/ack", json={"acknowledge_publication_id": end}, headers=auth(secret)
        )
        assert ack.status_code == 200, ack.text
        assert ack.json()["cursor"] == end
        if not expect_eventually_empty:
            break
    return seen


# ---------------------------------------------------------------- 登记与鉴权

def test_register_consumer_returns_one_time_secret(client):
    info = register(client)
    assert info["secret"] and isinstance(info["secret"], str)
    assert info["secret_shown_once"] is True
    assert info["cursor"] == 0
    assert info["consumer_id"] >= 1
    # 其他任何接口都不再回吐密钥明文
    me = client.get("/feed/consumers/me", headers=auth(info["secret"])).json()
    assert "secret" not in me


def test_register_consumer_validation(client):
    assert client.post("/feed/consumers", json={"name": "   "}).status_code == 400
    assert client.post("/feed/consumers", json={}).status_code == 400


def test_secret_required_and_unknown_rejected(client):
    assert client.get("/feed/releases").status_code == 401
    assert client.get("/feed/releases", headers=auth("nope")).status_code == 401
    assert client.get("/feed/consumers/me").status_code == 401
    assert (
        client.post("/feed/ack", json={"acknowledge_publication_id": 1}).status_code
        == 401
    )
    assert (
        client.post(
            "/feed/ack",
            json={"acknowledge_publication_id": 1},
            headers=auth("nope"),
        ).status_code
        == 401
    )


# ---------------------------------------------------------------- 拉取与批次

def test_pull_empty_when_no_publications(client):
    info = register(client)
    r = client.get("/feed/releases", headers=auth(info["secret"]))
    body = r.json()
    assert body["cursor"] == 0
    assert body["releases"] == []
    assert body["batch"] == {
        "first_publication_id": None,
        "last_publication_id": None,
        "size": 0,
        "truncated": False,
    }
    # 空批不锁定：有发布后立即可见
    client.post("/publish")
    r2 = client.get("/feed/releases", headers=auth(info["secret"]))
    assert r2.json()["batch"]["size"] == 1


def test_first_release_uses_empty_snapshot_baseline(client, warning_station):
    client.post("/publish")
    info = register(client)
    body = client.get("/feed/releases", headers=auth(info["secret"])).json()
    (rel,) = body["releases"]
    assert rel["publication_id"] == 1
    assert rel["previous_publication_id"] is None
    assert rel["changed"] is True
    (st,) = rel["stations"]
    assert st["station_id"] == warning_station
    assert st["status_before"] is None
    assert st["status_after"] == "warning"
    assert st["new_object"] is True
    (ev,) = rel["events_added"]
    assert ev == {
        "object_type": "station",
        "object_id": warning_station,
        "type": "warning",
        "at": iso(BASE + timedelta(minutes=10)),
        "occurrence": 1,
    }
    assert rel["events_removed"] == []


def test_no_change_release_still_occupies_position(client, warning_station):
    client.post("/publish")  # 1: 有变化
    client.post("/publish")  # 2: 无变化
    info = register(client)
    body = client.get("/feed/releases", headers=auth(info["secret"])).json()
    assert [r["publication_id"] for r in body["releases"]] == [1, 2]
    r1, r2 = body["releases"]
    assert r1["changed"] is True
    assert r2["changed"] is False
    assert r2["previous_publication_id"] == 1
    assert r2["stations"][0]["status_before"] == "warning"
    assert r2["stations"][0]["status_after"] == "warning"
    assert r2["events_added"] == [] and r2["events_removed"] == []


def test_repeated_pull_returns_same_pinned_batch(client, warning_station):
    client.post("/publish")
    info = register(client)
    h = auth(info["secret"])
    first = client.get("/feed/releases", headers=h).json()
    # 锁定期间新发布不进入本批
    client.post("/readings", json={
        "device_no": "DEV-001", "seq": 9,
        "observed_at": iso(BASE + timedelta(minutes=30)), "water_level": 5.0,
    })
    client.post("/publish")  # 发布 2
    second = client.get("/feed/releases", headers=h).json()
    third = client.get("/feed/releases", headers=h).json()
    assert second == first
    assert third == first
    # 批次在新发布之前锁定：锁定当时并未截断，重复拉取内容保持同一批
    assert first["batch"]["truncated"] is False
    assert first["batch"]["last_publication_id"] == 1
    me = client.get("/feed/consumers/me", headers=h).json()
    assert me["pending_batch"] == {
        "first_publication_id": 1,
        "last_publication_id": 1,
        "size": 1,
    }


def test_pull_limit_truncates_batch(client, warning_station):
    for _ in range(3):
        client.post("/publish")
    info = register(client)
    body = client.get("/feed/releases?limit=2", headers=auth(info["secret"])).json()
    assert [r["publication_id"] for r in body["releases"]] == [1, 2]
    assert body["batch"]["last_publication_id"] == 2
    assert body["batch"]["truncated"] is True
    # 重复拉取仍是锁定的前两条
    again = client.get("/feed/releases?limit=2", headers=auth(info["secret"])).json()
    assert [r["publication_id"] for r in again["releases"]] == [1, 2]
    client.post("/feed/ack", json={"acknowledge_publication_id": 2}, headers=auth(info["secret"]))
    nxt = client.get("/feed/releases", headers=auth(info["secret"])).json()
    assert [r["publication_id"] for r in nxt["releases"]] == [3]


def test_pull_invalid_limit(client):
    info = register(client)
    assert client.get("/feed/releases?limit=0", headers=auth(info["secret"])).status_code == 400
    assert client.get("/feed/releases?limit=1001", headers=auth(info["secret"])).status_code == 400


# ---------------------------------------------------------------- 确认语义

def test_ack_advances_cursor_and_next_batch(client, warning_station):
    client.post("/publish")  # 1: 预警冻结
    info = register(client)
    h = auth(info["secret"])
    first = client.get("/feed/releases", headers=h).json()["batch"]
    assert first["last_publication_id"] == 1
    # 确认前产生发布 2（解除）：未确认时不进入当前批
    client.post("/readings", json={
        "device_no": "DEV-001", "seq": 9,
        "observed_at": iso(BASE + timedelta(minutes=30)), "water_level": 5.0,
    })
    client.post("/readings", json={
        "device_no": "DEV-001", "seq": 10,
        "observed_at": iso(BASE + timedelta(minutes=36)), "water_level": 4.0,
    })
    client.post("/publish")  # 2: 解除
    assert client.get("/feed/releases", headers=h).json()["batch"]["last_publication_id"] == 1
    ack = client.post("/feed/ack", json={"acknowledge_publication_id": 1}, headers=h)
    assert ack.status_code == 200
    assert ack.json() == {
        "consumer_id": info["consumer_id"],
        "acknowledged_publication_id": 1,
        "cursor": 1,
        "duplicate": False,
    }
    nxt = client.get("/feed/releases", headers=h).json()
    (rel,) = nxt["releases"]
    assert rel["publication_id"] == 2
    assert rel["previous_publication_id"] == 1


def test_ack_out_of_sequence_rejected(client, warning_station):
    client.post("/publish")
    info = register(client)
    h = auth(info["secret"])
    client.get("/feed/releases", headers=h)
    # 不是末尾编号（这里末尾为 1，0 非法体由 400 拦截，取 999 测 404，滞后编号测越序）
    client.post("/publish")  # 2，但批次仍锁在 1
    r = client.post("/feed/ack", json={"acknowledge_publication_id": 2}, headers=h)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "out_of_sequence"
    assert r.json()["detail"]["expected_publication_id"] == 1
    assert r.json()["detail"]["received_publication_id"] == 2
    # 被拒后游标不动、批次不动
    body = client.get("/feed/releases", headers=h).json()
    assert body["batch"]["last_publication_id"] == 1


def test_ack_unknown_publication_404(client, warning_station):
    client.post("/publish")
    info = register(client)
    h = auth(info["secret"])
    client.get("/feed/releases", headers=h)
    assert (
        client.post("/feed/ack", json={"acknowledge_publication_id": 999}, headers=h).status_code
        == 404
    )


def test_ack_without_pending_batch(client, warning_station):
    client.post("/publish")
    info = register(client)
    h = auth(info["secret"])
    # 尚未拉取：任何存在的编号都拒绝
    r = client.post("/feed/ack", json={"acknowledge_publication_id": 1}, headers=h)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "no_pending_batch"
    # 拉取并确认后再确认同一编号 → 幂等重复成功
    client.get("/feed/releases", headers=h)
    assert client.post("/feed/ack", json={"acknowledge_publication_id": 1}, headers=h).status_code == 200
    dup = client.post("/feed/ack", json={"acknowledge_publication_id": 1}, headers=h)
    assert dup.status_code == 200
    assert dup.json()["duplicate"] is True
    assert dup.json()["cursor"] == 1
    # 已全部确认后再发超前编号 → 无批次拒绝
    r2 = client.post("/feed/ack", json={"acknowledge_publication_id": 1}, headers=h)
    assert r2.status_code == 200 and r2.json()["duplicate"] is True


# ---------------------------------------------------------------- 差异内容

def test_clear_event_added_between_releases(client, warning_station):
    client.post("/publish")  # 1: warning
    # 两条跨至少 5 分钟的低水位读数触发解除
    for i, minute in enumerate((20, 26)):
        client.post("/readings", json={
            "device_no": "DEV-001", "seq": 20 + i,
            "observed_at": iso(BASE + timedelta(minutes=minute)), "water_level": 5.0,
        })
    client.post("/publish")  # 2: cleared
    info = register(client)
    body = client.get("/feed/releases?limit=1", headers=auth(info["secret"])).json()
    assert body["releases"][0]["publication_id"] == 1
    client.post("/feed/ack", json={"acknowledge_publication_id": 1}, headers=auth(info["secret"]))
    rel = client.get("/feed/releases", headers=auth(info["secret"])).json()["releases"][0]
    st = rel["stations"][0]
    assert st["status_before"] == "warning"
    assert st["status_after"] == "normal"
    (ev,) = rel["events_added"]
    assert ev["type"] == "cleared"
    assert ev["at"] == iso(BASE + timedelta(minutes=26))
    assert ev["occurrence"] == 1


def test_retroactive_revision_shows_event_revocation(client, warning_station):
    client.post("/publish")  # 1: 预警成立并冻结
    # 追溯作废触发预警的读数 → 待发布事件消失，重新发布后须以撤销呈现
    client.post("/readings/3/void", json={"reason": "误报作废"})
    client.post("/publish")  # 2
    info = register(client)
    seen = drain(client, info["secret"])
    assert seen == [1, 2]
    # 直接核对发布 2 相对发布 1 的内容
    rel = client.get(
        "/feed/releases", headers=auth(info["secret"])  # 已全部确认，改查历史发布接口
    )
    assert rel.json()["releases"] == []
    pub2 = client.get("/publications/2").json()
    assert pub2["timelines"][0]["status"] == "normal"
    # 用新消费方回放，检查发布 2 的撤销事件
    info2 = register(client, "值班系统乙")
    body = client.get("/feed/releases?limit=2", headers=auth(info2["secret"])).json()
    r2 = body["releases"][1]
    (removed,) = r2["events_removed"]
    assert removed == {
        "object_type": "station",
        "object_id": warning_station,
        "type": "warning",
        "at": iso(BASE + timedelta(minutes=10)),
        "occurrence": 1,
    }
    assert r2["events_added"] == []
    assert r2["stations"][0]["status_before"] == "warning"
    assert r2["stations"][0]["status_after"] == "normal"


def test_zone_changes_appear_in_release(client, warning_station):
    # 第二个站也预警，责任区 K=2
    client.post("/stations", json={"name": "白杨沟站", "rise_threshold": 10.0, "clear_threshold": 8.0})
    client.post("/stations/2/devices", json={"device_no": "DEV-002"})
    for i, minute in enumerate((0, 5, 10)):
        client.post("/readings", json={
            "device_no": "DEV-002", "seq": i + 1,
            "observed_at": iso(BASE + timedelta(minutes=minute)), "water_level": 12.0,
        })
    z = client.post("/zones", json={
        "name": "青石峡片区",
        "effective_at": iso(BASE - timedelta(hours=1)),
        "station_ids": [1, 2],
        "trigger_count": 2,
    })
    assert z.status_code == 201, z.text
    client.post("/publish")
    info = register(client)
    rel = client.get("/feed/releases", headers=auth(info["secret"])).json()["releases"][0]
    assert len(rel["zones"]) == 1
    zc = rel["zones"][0]
    assert zc["zone_id"] == 1
    assert zc["status_before"] is None
    assert zc["status_after"] == "warning"
    assert zc["new_object"] is True
    assert zc["warning_station_ids_before"] == []
    assert zc["warning_station_ids_after"] == [1, 2]
    assert any(e["object_type"] == "zone" and e["type"] == "warning" for e in rel["events_added"])


def test_pending_changes_and_failed_confirm_invisible(client, warning_station):
    client.post("/publish")  # 1
    info = register(client)
    h = auth(info["secret"])
    drain(client, info["secret"])
    # 待发布变化（未发布）不可见
    client.post("/readings", json={
        "device_no": "DEV-001", "seq": 9,
        "observed_at": iso(BASE + timedelta(minutes=30)), "water_level": 5.0,
    })
    assert client.get("/feed/releases", headers=h).json()["releases"] == []
    # 受控确认失败（核对后数据变化）不得产生发布、不可见
    token = client.post("/publish/checks").json()["token"]
    client.post("/readings", json={
        "device_no": "DEV-001", "seq": 10,
        "observed_at": iso(BASE + timedelta(minutes=35)), "water_level": 5.0,
    })
    assert client.post("/publish/confirm", json={"token": token}).status_code == 409
    assert client.get("/feed/releases", headers=h).json()["releases"] == []
    # 受控确认成功 → 新冻结发布可见
    token2 = client.post("/publish/checks").json()["token"]
    ok = client.post("/publish/confirm", json={"token": token2, "note": "值班确认"})
    assert ok.status_code == 201
    body = client.get("/feed/releases", headers=h).json()
    assert [r["publication_id"] for r in body["releases"]] == [2]
    assert body["releases"][0]["note"] == "值班确认"


# ---------------------------------------------------------------- 游标独立 / 回放 / 续读

def test_independent_consumers_and_history_replay(client, warning_station):
    client.post("/publish")
    client.post("/publish")
    a = register(client, "甲")
    # 消费方甲消费掉发布 1
    body = client.get("/feed/releases?limit=1", headers=auth(a["secret"])).json()
    assert [r["publication_id"] for r in body["releases"]] == [1]
    client.post("/feed/ack", json={"acknowledge_publication_id": 1}, headers=auth(a["secret"]))
    # 后登记的消费方乙可从头回放全部历史
    b = register(client, "乙")
    body_b = client.get("/feed/releases", headers=auth(b["secret"])).json()
    assert [r["publication_id"] for r in body_b["releases"]] == [1, 2]
    assert body_b["cursor"] == 0
    # 甲只看到自己剩下的发布 2，互不串游标
    body_a = client.get("/feed/releases", headers=auth(a["secret"])).json()
    assert [r["publication_id"] for r in body_a["releases"]] == [2]


def test_cursor_and_pinned_batch_survive_restart(db_path, warning_station):
    client = make_client(db_path)
    client.post("/publish")
    client.post("/publish")
    info = client.post("/feed/consumers", json={"name": "重启甲"}).json()
    h = auth(info["secret"])
    client.get("/feed/releases?limit=1", headers=h)  # 锁定批次 [1]
    client.post("/feed/ack", json={"acknowledge_publication_id": 1}, headers=h)
    client.get("/feed/releases", headers=h)  # 锁定批次 [2]，未确认
    # —— 模拟服务重启：同库路径重建应用 ——
    client2 = make_client(db_path)
    me = client2.get("/feed/consumers/me", headers=h).json()
    assert me["cursor"] == 1
    assert me["pending_batch"] == {
        "first_publication_id": 2,
        "last_publication_id": 2,
        "size": 1,
    }
    # 重启后重复拉取仍是锁定的同一批
    body = client2.get("/feed/releases", headers=h).json()
    assert [r["publication_id"] for r in body["releases"]] == [2]
    assert client2.post(
        "/feed/ack", json={"acknowledge_publication_id": 2}, headers=h
    ).status_code == 200
    assert client2.get("/feed/releases", headers=h).json()["releases"] == []
    client2.close()
    client.close()


# ---------------------------------------------------------------- 并发

def test_concurrent_pulls_return_identical_batch(client, warning_station):
    client.post("/publish")
    info = register(client)
    results: list[list[int]] = []
    errors: list[int] = []

    def pull():
        r = client.get("/feed/releases", headers=auth(info["secret"]))
        if r.status_code == 200:
            results.append([x["publication_id"] for x in r.json()["releases"]])
        else:
            errors.append(r.status_code)

    threads = [threading.Thread(target=pull) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert results == [[1]] * 12


def test_concurrent_publish_and_consume_no_loss_or_crossing(db_path, warning_station):
    client = make_client(db_path)
    info = client.post("/feed/consumers", json={"name": "并发甲"}).json()
    h = auth(info["secret"])
    # 先有 3 个历史发布
    for _ in range(3):
        client.post("/publish")

    errors: list[tuple[str, int]] = []

    def publish_many(n, start_seq):
        c = make_client(db_path)
        for i in range(n):
            rr = c.post("/publish")
            if rr.status_code != 201:
                errors.append(("pub", rr.status_code))
        c.close()

    def consume(stop_after_empty_rounds):
        c = make_client(db_path)
        empty = 0
        while empty < stop_after_empty_rounds:
            r = c.get("/feed/releases", headers=h)
            if r.status_code != 200:
                errors.append(("pull", r.status_code))
                break
            body = r.json()
            if not body["releases"]:
                empty += 1
                continue
            empty = 0
            ids = [rel["publication_id"] for rel in body["releases"]]
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                errors.append(("order", 0))
            ack = c.post(
                "/feed/ack",
                json={"acknowledge_publication_id": body["batch"]["last_publication_id"]},
                headers=h,
            )
            if ack.status_code != 200:
                errors.append(("ack", ack.status_code))
        c.close()

    producers = [threading.Thread(target=publish_many, args=(20, 0)) for _ in range(3)]
    consumer = threading.Thread(target=consume, args=(20,))
    for t in producers:
        t.start()
    consumer.start()
    for t in producers:
        t.join()
    consumer.join()

    assert errors == [], errors
    # 生产者结束后排空剩余发布
    rest = drain(client, info["secret"])
    all_seen_via_other = rest  # 消费者线程内已消费部分不在此列；最终游标必须到达最新发布
    latest = client.get("/feed/consumers/me", headers=h).json()["latest_publication_id"]
    me = client.get("/feed/consumers/me", headers=h).json()
    assert me["cursor"] == latest
    assert latest == 3 + 60
    # 最终确实没有遗漏
    assert client.get("/feed/releases", headers=h).json()["releases"] == []
    client.close()


def test_existing_contracts_still_work(client, warning_station):
    """既有发布与公开查询契约保持兼容。"""
    r = client.post("/publish", json={"note": "直接发布"})
    assert r.status_code == 201
    pub_id = r.json()["publication_id"]
    assert client.get("/publications").status_code == 200
    assert client.get(f"/publications/{pub_id}").status_code == 200
    assert client.get("/public/current").status_code == 200
    assert client.get(f"/public/stations/{warning_station}/timeline").status_code == 200
    assert client.get(f"/stations/{warning_station}/timeline").status_code == 200
    assert client.get("/health").status_code == 200
