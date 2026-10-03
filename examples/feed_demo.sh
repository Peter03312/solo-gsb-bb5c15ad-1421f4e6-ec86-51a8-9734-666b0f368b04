#!/usr/bin/env bash
# 下游值班系统「公开预警变更 · 断点领取」端到端调用示例。
#
# 流程：登记消费方（密钥只展示一次）→ 空批拉取 → 两轮直接发布
#       → limit 截断 / 重复拉取同批 / 越序确认 409 → 确认末尾编号
#       → 重复确认幂等 → 下一批（解除事件）→ 无效密钥 401 → 新消费方回放历史
#
# 用法：
#   ./examples/feed_demo.sh
#   BASE=http://127.0.0.1:8000 ./examples/feed_demo.sh
#
# 说明：脚本会新建一个监测站/设备（设备号带时间戳避免与已有数据冲突），
#       不依赖空库；发布为全局操作，会把当前全部待发布结果一并冻结。
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
TS=$(date +%s)
DEV="FEED-DEMO-$TS"

jget() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))' "$1"; }

echo "== 1) 登记消费方，取得仅展示一次的密钥 =="
REG=$(curl -s -X POST "$BASE/feed/consumers" \
  -H 'content-type: application/json' \
  -d '{"name":"下游值班系统-示例"}')
echo "$REG"
SECRET=$(echo "$REG" | jget 'd["secret"]')
H=(-H "X-Feed-Secret: $SECRET")
echo

echo "== 2) 尚无新发布时拉取 → 空批（不锁定） =="
curl -s "$BASE/feed/releases" "${H[@]}"; echo
echo

echo "== 3) 准备站点/设备与读数：先预警，发布 #1；再解除，发布 #2 =="
SID=$(curl -s -X POST "$BASE/stations" -H 'content-type: application/json' \
  -d '{"name":"断点领取示例站","rise_threshold":10.0,"clear_threshold":8.0}' | jget 'd["id"]')
curl -s -X POST "$BASE/stations/$SID/devices" -H 'content-type: application/json' \
  -d "{\"device_no\":\"$DEV\"}" >/dev/null
up() { # seq observed_at level
  curl -s -X POST "$BASE/readings" -H 'content-type: application/json' \
    -d "{\"device_no\":\"$DEV\",\"seq\":$1,\"observed_at\":\"$2\",\"water_level\":$3}" >/dev/null
}
up 1 2026-10-01T00:00:00Z 12
up 2 2026-10-01T00:05:00Z 12
up 3 2026-10-01T00:10:00Z 12
echo "publish #1 -> $(curl -s -X POST "$BASE/publish" -H 'content-type: application/json' -d '{"note":"首轮发布（预警）"}' | jget 'd["publication_id"]')"
up 4 2026-10-01T00:20:00Z 5
up 5 2026-10-01T00:26:00Z 5
echo "publish #2 -> $(curl -s -X POST "$BASE/publish" -H 'content-type: application/json' -d '{"note":"解除轮发布"}' | jget 'd["publication_id"]')"
echo

echo "== 4) 拉取下一批（limit=1：本批只含发布 #1，truncated=true） =="
PULL=$(curl -s "$BASE/feed/releases?limit=1" "${H[@]}")
echo "$PULL" | python3 -m json.tool
END1=$(echo "$PULL" | jget 'd["batch"]["last_publication_id"]')
echo

echo "== 5) 未确认前重复拉取 → 仍是同一批 [1] =="
curl -s "$BASE/feed/releases?limit=1" "${H[@]}" \
  | jget '[r["publication_id"] for r in d["releases"]]'
echo

echo "== 6) 越序确认（本批末尾为 $END1，却确认 2）→ 409 out_of_sequence =="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/feed/ack" \
  -H 'content-type: application/json' "${H[@]}" \
  -d '{"acknowledge_publication_id":2}'
echo

echo "== 7) 确认本批末尾编号 $END1 → 游标推进 =="
curl -s -X POST "$BASE/feed/ack" -H 'content-type: application/json' "${H[@]}" \
  -d "{\"acknowledge_publication_id\":$END1}"; echo
echo "== 8) 重复确认同一编号 → 幂等 duplicate=true =="
curl -s -X POST "$BASE/feed/ack" -H 'content-type: application/json' "${H[@]}" \
  -d "{\"acknowledge_publication_id\":$END1}"; echo
echo

echo "== 9) 再拉下一批 → 发布 #2（warning→normal，新增解除事件） =="
PULL2=$(curl -s "$BASE/feed/releases" "${H[@]}")
echo "$PULL2" | python3 -c '
import sys,json
b=json.load(sys.stdin)
for r in b["releases"]:
    print("publication_id:", r["publication_id"], "changed:", r["changed"])
    for s in r["stations"]:
        print("  station", s["station_id"], s["status_before"], "->", s["status_after"])
    print("  events_added:  ", r["events_added"])
    print("  events_removed:", r["events_removed"])'
END2=$(echo "$PULL2" | jget 'd["batch"]["last_publication_id"]')
curl -s -X POST "$BASE/feed/ack" -H 'content-type: application/json' "${H[@]}" \
  -d "{\"acknowledge_publication_id\":$END2}" >/dev/null
echo "确认后再拉取: $(curl -s "$BASE/feed/releases" "${H[@]}")"
echo

echo "== 10) 密钥校验：错误/缺失 → 401 =="
curl -s -o /dev/null -w "wrong key -> HTTP %{http_code}\n" "$BASE/feed/releases" -H "X-Feed-Secret: nope"
curl -s -o /dev/null -w "no key    -> HTTP %{http_code}\n" "$BASE/feed/releases"
echo

echo "== 11) 新登记消费方可从头发放（回放）全部历史发布 =="
S2=$(curl -s -X POST "$BASE/feed/consumers" -H 'content-type: application/json' \
  -d '{"name":"下游值班系统-示例-乙"}' | jget 'd["secret"]')
curl -s "$BASE/feed/releases" -H "X-Feed-Secret: $S2" \
  | jget '[r["publication_id"] for r in d["releases"]]'
