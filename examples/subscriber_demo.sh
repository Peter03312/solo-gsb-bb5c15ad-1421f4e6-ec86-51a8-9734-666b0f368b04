#!/usr/bin/env bash
# 下游值班系统「断点领取公开预警变更」——本轮 API 自包含调用示例
#
# 特性覆盖：
#   一次性密钥登记；首次以空快照比较；无变化发布占序位；历史发布回放；
#   未确认重复拉取同批；确认仅接受批次末尾编号、重复确认幂等、越序拒绝；
#   追溯修订造成的事件消失以撤销(events_removed)呈现；
#   待发布变化/失败确认不可见；各消费方游标独立；重启后续读；并发不漏不串。
#
# 用法：
#   ./examples/subscriber_demo.sh                 # 自动在 127.0.0.1:8011 起一个临时服务
#   BASE=http://127.0.0.1:8000 ./examples/subscriber_demo.sh   # 复用已运行的服务（数据会累积）
set -euo pipefail

BASE="${BASE:-}"
J='content-type: application/json'
DB="${TMPDIR:-/tmp}/flood-subscriber-demo.db"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

SERVER_PID=""
if [[ -z "$BASE" ]]; then
  rm -f "$DB" "$DB-wal" "$DB-shm"
  BASE="http://127.0.0.1:8011"
  FLOOD_DB_PATH="$DB" PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)" \
    python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8011 >/tmp/subscriber-demo-uvicorn.log 2>&1 &
  SERVER_PID=$!
  trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
  for _ in $(seq 1 30); do
    curl -sf "$BASE/health" >/dev/null 2>&1 && break
    sleep 0.3
  done
fi

jqr() { python3 -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
jlast() { python3 -c 'import sys,json;print(json.load(sys.stdin)["batch"]["last_publication_id"])'; }

# ---------- 场景准备：站 1/设备 D1，08:00/05/10 三条 12.0 → 预警 ----------
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"青石峡站","rise_threshold":10.0,"clear_threshold":8.0}' >/dev/null
curl -s -X POST "$BASE/stations/1/devices" -H "$J" -d '{"device_no":"DEV-001"}' >/dev/null
for m in 00 05 10; do
  curl -s -X POST "$BASE/readings" -H "$J" \
    -d "{\"device_no\":\"DEV-001\",\"seq\":1$m,\"observed_at\":\"2026-10-01T08:$m:00+08:00\",\"water_level\":12.0}" >/dev/null
done

step "1. 消费方登记 → 取得仅展示一次的密钥（服务端只存 SHA-256 哈希）"
SUB=$(curl -s -X POST "$BASE/subscribers" -H "$J" -d '{"name":"县防汛值班系统"}')
echo "$SUB"
SKEY=$(echo "$SUB" | jqr key)
SK=(-H "X-Subscriber-Key: $SKEY")

step "2. 尚无发布时拉取 → 空批次，无末编号可确认；越序确认 409"
curl -s "${SK[@]}" "$BASE/subscriber/releases"
echo
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/subscriber/ack" "${SK[@]}" -H "$J" \
  -d '{"last_publication_id":1}'

step "3. 产生发布 1（warning 冻结）与发布 2（无变化，仍占序位）"
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"首次发布"}' | python3 -c 'import sys,json;print("  ->",json.load(sys.stdin)["publication_id"])'
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"无变化发布"}' | python3 -c 'import sys,json;print("  ->",json.load(sys.stdin)["publication_id"])'

step "4. 拉取（limit=1）只取发布 1：相对空快照，status_before=null，warning 为新增 occurrence=1"
curl -s "${SK[@]}" "$BASE/subscriber/releases?limit=1"
echo

step "5. 未确认前重复拉取（期间产生发布 3）仍返回同批 [1]，不漏不串"
for m in 15 21; do
  curl -s -X POST "$BASE/readings" -H "$J" \
    -d "{\"device_no\":\"DEV-001\",\"seq\":1$m,\"observed_at\":\"2026-10-01T08:$m:00+08:00\",\"water_level\":7.0}" >/dev/null
done
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"解除发布"}' >/dev/null
curl -s "${SK[@]}" "$BASE/subscriber/releases?limit=10" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  重复拉取批次:",[r["publication_id"] for r in d["batch"]["releases"]],"has_more:",d["has_more"])'

step "6. 确认只接受批次末尾编号：越序 409；确认 1 成功；重复确认 1 幂等"
curl -s -o /dev/null -w '  越序确认 999 → HTTP %{http_code}\n' -X POST "$BASE/subscriber/ack" "${SK[@]}" -H "$J" \
  -d '{"last_publication_id":999}'
curl -s -X POST "$BASE/subscriber/ack" "${SK[@]}" -H "$J" -d '{"last_publication_id":1}'
echo
curl -s -o /dev/null -w '  重复确认 1（幂等）→ HTTP %{http_code}\n' -X POST "$BASE/subscriber/ack" "${SK[@]}" -H "$J" \
  -d '{"last_publication_id":1}'

step "7. 继续领取：发布 2 无变化(changed=false)；发布 3 相对发布 2 为 warning→normal，新增 cleared#1"
curl -s "${SK[@]}" "$BASE/subscriber/releases?limit=10" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for r in d["batch"]["releases"]:
    s = r["stations"][0]
    print("  发布", r["publication_id"], "基准", r["baseline_publication_id"], "changed=", r["changed"],
          "| 站状态", s["status_before"], "->", s["status_after"],
          "| 新增", [(e["type"], e["occurrence"]) for e in s["events_added"]],
          "| 撤销", [(e["type"], e["occurrence"]) for e in s["events_removed"]])
'
LAST=$(curl -s "${SK[@]}" "$BASE/subscriber/releases?limit=10" | jlast)
curl -s -o /dev/null -X POST "$BASE/subscriber/ack" "${SK[@]}" -H "$J" -d "{\"last_publication_id\":$LAST}"
echo "  已确认到 $LAST"

step "8. 追溯修订：作废关键读数使历史 warning 不成立，重发布后该事件以撤销(events_removed)呈现"
RID=$(curl -s "$BASE/stations/1/readings" | python3 -c 'import sys,json;print(json.load(sys.stdin)[1]["id"])')
curl -s -X POST "$BASE/readings/$RID/void" -H "$J" -d '{"reason":"误报剔除"}' >/dev/null
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"追溯修订后发布"}' >/dev/null
curl -s "${SK[@]}" "$BASE/subscriber/releases" | python3 -c '
import sys, json
r = json.load(sys.stdin)["batch"]["releases"][0]
s = r["stations"][0]
print("  发布", r["publication_id"], "站状态", s["status_before"], "->", s["status_after"])
print("  新增事件:", s["events_added"])
print("  撤销事件:", s["events_removed"], "（追溯修订造成的消失必须显示为撤销）")
'
LAST=$(curl -s "${SK[@]}" "$BASE/subscriber/releases" | python3 -c 'import sys,json;print(json.load(sys.stdin)["batch"]["last_publication_id"])')
curl -s -o /dev/null -X POST "$BASE/subscriber/ack" "${SK[@]}" -H "$J" -d "{\"last_publication_id\":$LAST}"

step "9. 待发布变化与失败的受控确认不可见：只有成功冻结的发布才进入拉取"
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":900,"observed_at":"2026-10-01T20:00:00+08:00","water_level":5.0}' >/dev/null
TOKEN=$(curl -s -X POST "$BASE/publish/checks" | jqr token)
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":901,"observed_at":"2026-10-01T20:05:00+08:00","water_level":5.0}' >/dev/null
curl -s -o /dev/null -w '  核对后发生写入 → 受控确认 HTTP %{http_code}（不生成发布）\n' \
  -X POST "$BASE/publish/confirm" -H "$J" -d "{\"token\":\"$TOKEN\"}"
curl -s "${SK[@]}" "$BASE/subscriber/releases" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  失败确认后拉取批次发布号:",[r["publication_id"] for r in d["batch"]["releases"]],"（空=不可见）")'

step "10. 游标独立：第二个消费方从发布 1 完整回放历史，不受第一个消费方影响"
KEY2=$(curl -s -X POST "$BASE/subscribers" -H "$J" -d '{"name":"值班备份系统"}' | jqr key)
curl -s -H "X-Subscriber-Key: $KEY2" "$BASE/subscriber/releases?limit=100" \
  | python3 -c 'import sys,json;print("  新消费方可回放发布:",[r["publication_id"] for r in json.load(sys.stdin)["batch"]["releases"]])'

step "11. 游标查询（持久化、可重启续读）"
curl -s "${SK[@]}" "$BASE/subscriber/status"
echo

step "12. 无效/缺失密钥一律 401"
curl -s -o /dev/null -w '  无请求头   → HTTP %{http_code}\n' "$BASE/subscriber/releases"
curl -s -o /dev/null -w '  错误密钥   → HTTP %{http_code}\n' -H 'X-Subscriber-Key: wrong' "$BASE/subscriber/releases"

echo
echo "完成。接口文档：$BASE/docs"
[[ -n "$SERVER_PID" ]] && echo "（临时服务随脚本退出自动停止）"
