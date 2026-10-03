# 山洪水位预警裁定服务

基于 **Python + FastAPI + SQLite** 的山洪水位预警裁定服务：监测站/设备登记、**汛期一站多设备轮值接管选数**、读数幂等补传、阈值版本管理（防汛方案调整，**支持登记前不落库预演**）、按规则自动裁定预警与解除、审核修订并重算、**防汛责任区跨站联动预警**、发布冻结公开结果。

## 一键启动

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
```

### 方式二：Docker

```bash
docker build -t flood-warning-service .
docker run -d -p 8000:8000 -v flood-data:/data --name flood-warning flood-warning-service
```

### 方式三：本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动时自动完成 SQLite 建表初始化（含阈值版本表、责任区表），无需额外迁移步骤；已有数据库重启后会自动补齐新表。

## 地址与端口

| 项 | 地址 |
|---|---|
| 服务地址 | http://127.0.0.1:8000 |
| 交互式 API 文档（Swagger） | http://127.0.0.1:8000/docs |
| OpenAPI  schema | http://127.0.0.1:8000/openapi.json |
| 健康检查 | `GET /health` |

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FLOOD_DB_PATH` | `./data/flood.db`（容器内 `/data/flood.db`） | SQLite 数据库文件路径 |
| `FLOOD_MIN_WATER_LEVEL` | `-50` | 水位合法下限，超出报 400 |
| `FLOOD_MAX_WATER_LEVEL` | `1000` | 水位合法上限，超出报 400 |
| `FLOOD_SEGMENT_GAP_MINUTES` | `15` | 相邻读数间隔超过该值即断段 |
| `FLOOD_RISE_MIN_COUNT` | `3` | 预警所需同段连续达上升阈值读数个数 |
| `FLOOD_RISE_MIN_SPAN_MINUTES` | `10` | 预警所需连续段首尾最小间隔（分钟） |
| `FLOOD_CLEAR_MIN_COUNT` | `2` | 解除所需连续不高于解除阈值读数个数 |
| `FLOOD_CLEAR_MIN_SPAN_MINUTES` | `5` | 解除所需连续段首尾最小间隔（分钟） |

## 裁定规则

1. 有效（未作废）读数按**观测时间**升序重排，相邻间隔 **> 15 分钟**即断段；
2. **预警**：同段内连续 **≥ 3** 个读数水位 **≥ 该读数当时生效的上升阈值**，且该连续段首尾相隔 **≥ 10 分钟**——在首次同时满足两项条件的读数时刻发出预警；
3. **解除**：预警状态下连续 **≥ 2** 个读数水位 **≤ 该读数当时生效的解除阈值**，且首尾相隔 **≥ 5 分钟**——在满足条件的读数时刻解除；
4. 断段只重置上升计数；**断段本身不自动解除预警**，解除计数可跨段延续；
5. **阈值版本**：每条有效读数按观测时间采用**最近已生效**的阈值版本，**同一生效时刻采用新版本**；早于首个版本生效时间的读数使用站点**登记基准阈值**；跨版本的连续读数分别按各自阈值判断，断段与解除规则不变；
6. **阈值调整本身不产生预警或解除事件**，只改变之后（及重算时）各读数采用的阈值；
7. 审核员**作废/更正**读数后，待发布裁定按**全部有效读数**立即重算；**发布**将各站时间线冻结为公开结果，之后的补传/修订/阈值调整只影响待发布结果，**重新发布**才更新公开结果，历史发布快照保持不变。
8. **汛期设备轮值接管选数**：首条接管生效前沿用站内全部有效读数；**自生效时刻起（含同刻）仅最近一条接管记录所指定设备**的有效读数进入本站裁定，接管设备缺测**不借用**其他设备读数；未入选读数仍可查询，不参与裁定与阈值命中（见下节）。

## 汛期设备轮值接管（一站多设备）

汛期一站多设备轮值时，值班员可登记**本站设备**的接管记录：输入**设备号、带时区的生效时刻和非空原因**；查询记录按生效时刻升序返回设备与原因。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/stations/{id}/device-takeovers` | 登记一条接管记录：`{device_no, effective_at, reason}` |
| GET | `/stations/{id}/device-takeovers` | 查询本站接管记录（按生效时刻升序） |

选数规则（时间线、责任区联动、发布冻结与**阈值预演**全部一致遵守）：

1. **首条接管生效前沿用站内全部有效读数**；
2. **自生效时刻起（含同刻）**，每条有效读数按其**观测时间**取 `effective_at <= 观测时间` 的**最近一条接管记录**；读数设备与该接管指定设备**相同才进入本站裁定**（边界包含，同刻即按新接管设备选数）；
3. 接管设备在该时段**缺测不借用其他设备**——其他设备读数即使存在也一律落选；
4. 接管可在设备间反复切换：多条记录各自生效，任一时刻只由最近一条决定入选设备；
5. **未入选读数不删除**：仍出现在 `GET /stations/{id}/readings`，时间线 `readings[]` 中以 `selected=false` + `takeover_id` 标记落选，仅不参与分段/预警/解除裁定；
6. **补传、更正观测时间、作废或追溯登记接管后，一律按观测时间重算**本站待发布结果及**所属责任区**联动结果（追溯接管会使已不成立的历史事件消失）。

校验与状态码：

- `effective_at` 必须带时区、`reason` 必填（纯空白拒绝）、`device_no` 必填且非空白，否则 **400**；
- 监测站不存在 → **404**；设备号不存在 → **404**；**设备存在但隶属其他站（跨站设备）→ 400**（响应给出 `device_station_id`）；
- **同站同一生效瞬间只允许一条**（不同时区表示同一时刻也算）→ **409**（响应给出 `existing_takeover_id`）；不同站互不影响；
- 接管登记是影响裁定的写入：响应附带重算后的 `pending_adjudication`，并**推进数据版本、使此前全部受控发布核对凭证失效**（确认 → 409 `data_changed_after_check`）；
- 接管登记**不改写历史发布快照**：当前公开结果须**重新发布**才更新；新快照的 `readings[]` 带选数标记，旧快照原样保留。

登记成功响应与接管列表示例：

```json
{
  "takeover": {
    "id": 1,
    "station_id": 8,
    "effective_at": "2026-10-01T00:10:00Z",
    "reason": "甲机检修，乙机轮值",
    "created_at": "2026-10-03T08:00:00Z",
    "device": {"id": 17, "device_no": "DEV-009", "station_id": 8}
  },
  "pending_adjudication": {"station_id": 8, "status": "normal", "events": []}
}
```

时间线 `readings[]` 在原有字段基础上新增选数信息（**阈值预演的结果同样带这些字段**）：

```json
{
  "reading_id": 27, "device_id": 16, "device_no": "DEV-008",
  "observed_at": "2026-10-01T00:10:00Z", "water_level": 12.0,
  "rise_threshold": 10.0, "clear_threshold": 8.0, "threshold_version_id": null,
  "selected": false,                 // 是否进入本站裁定
  "takeover_id": 1                   // 落选时给出其观测时刻命中的接管记录；未命中任何接管为 null
}
```

顶层另增 `active_readings_total`（站内全部有效读数数）；原有的 `readings_considered` 语义明确为**经接管选数后实际进入裁定的读数数**。读数幂等补传、阈值版本与发布接口的既有请求/响应约定保持不变（仅新增字段）。

## 阈值版本（防汛方案调整）

防汛方案调整后，可对监测站登记带**生效时间（必带时区）**、**上升阈值**、**解除阈值**和**原因**的阈值版本，并查询版本历史：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/stations/{id}/threshold-versions` | 登记一版阈值：`{effective_at, rise_threshold, clear_threshold, reason}` |
| GET | `/stations/{id}/threshold-versions` | 版本历史（按生效时间升序），含登记基准 `baseline` |

校验与语义：

- `effective_at` 必须带时区（如 `2026-10-02T08:00:00+08:00` 或 `...Z`），否则 400；服务端按同一瞬间归一化比较，**同站重复生效时间 409**（不同时区表示同一时刻也算重复；不同站互不影响）；
- 阈值必须为**有限数值**（NaN / Infinity 拒绝），且**上升阈值严格大于解除阈值**，否则 400；`reason` 必填；
- 站点不存在返回 **404**；
- 登记成功的响应附带 `pending_adjudication`（重算后的待发布状态），但其中**不会因调整动作本身新增任何事件**——所有事件仍由读数判定产生；
- 版本按 `effective_at <= 观测时间` 取最晚一版（边界包含，同刻用新版）；未命中时回退登记阈值；
- 时间线响应新增每读数采用的阈值信息：`readings[].{reading_id, observed_at, water_level, rise_threshold, clear_threshold, threshold_version_id}`（`threshold_version_id` 为 `null` 表示基准值）；设备轮值接管上线后该数组另含 `device_id / device_no / selected / takeover_id` 选数字段（见“汛期设备轮值接管”）；`events[]` 也附带触发当时的 `rise_threshold / clear_threshold / threshold_version_id`。原有顶层字段与其他接口的请求响应保持不变。

## 阈值调整预演（登记前不落库演练）

防汛人员在**正式登记阈值版本前**，可先预演该项调整对**待发布预警**的影响：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/stations/{id}/threshold-versions/preview` | 预演一版阈值调整的影响，**不落库、不改变待发布查询或已冻结发布** |

- 请求体与正式登记**完全相同**：`{effective_at, rise_threshold, clear_threshold, reason}`，沿用同一套校验与冲突规则——必带时区、阈值有限且上升严格大于解除、原因必填（非法 → **400**）；未知站 → **404**；既有版本与候选**同一生效瞬间**（不同时区表示同一时刻也算）→ **409**；
- 预演版本不写入数据库：版本历史不可见、待发布时间线/区域结果不变、历史发布快照不变；
- 裁定语义与正式登记后完全一致：各读数按观测时间在“既有版本 + 候选版本”中取最近已生效版本，**生效同刻采用候选新值**；候选之后若还有既有版本，到其生效时刻仍由既有版本接替；候选版本在时间线中以临时 id **`-1`** 标识（正式登记后换成真实版本 id）；
- 预演同样遵循**汛期设备轮值接管选数规则**：落选设备的读数不参与预演裁定（`selected=false`），接管本身无候选、预演期间不可模拟，仅套用既有接管记录；
- 责任区联动：返回**该站及所有包含它的责任区**的 `current`（现状）与 `preview`（预演），区域**同一时刻合并所有站变化后再按 K 判定**；责任区内**其他成员站按现有待发布结果参与联动**（仅目标站套用候选版本）；
- 整个结果取自**同一只读数据视图**（单连接、进程内锁，全程无写入）；在无其他写入时，随后正式登记得到的**状态、事件类型与时刻与预演一致**。

响应结构（时间线字段与 `GET /stations/{id}/timeline`、`GET /zones/{id}/timeline` 相同）：

```json
{
  "dry_run": true,
  "station_id": 6,
  "candidate": {
    "effective_at": "2026-10-01T03:00:00Z",
    "rise_threshold": 9.0,
    "clear_threshold": 7.0,
    "reason": "入汛提级预演",
    "threshold_version_id": -1
  },
  "current": { "published": false, "status": "normal",  "events": [], "readings": [] },
  "preview": { "published": false, "status": "warning", "events": [], "readings": [] },
  "zones": [
    {
      "zone_id": 3,
      "zone_name": "南岭片区",
      "trigger_count": 2,
      "current": {
        "published": false, "status": "normal",
        "warning_station_ids": [7], "warning_station_count": 1,
        "events": [],
        "members": [ {"station_id": 6, "warning": false}, {"station_id": 7, "warning": true} ]
      },
      "preview": {
        "published": false, "status": "warning",
        "warning_station_ids": [6, 7], "warning_station_count": 2,
        "events": [ {"type": "warning", "at": "2026-10-01T03:10:00Z",
                     "warning_station_ids": [6, 7], "warning_station_count": 2} ],
        "members": []
      }
    }
  ]
}
```

> 预演不改变任何数据：反复调用安全；确认无误后再调
> `POST /stations/{id}/threshold-versions` 正式登记。

## 防汛责任区（跨站联动预警）

防汛人员按责任区查看多个监测站的**跨站联动**结果。登记责任区时提供：

- `name`：**非空**名称（纯空白拒绝；同名责任区允许并存）；
- `effective_at`：**必带时区**的生效时间；
- `station_ids`：成员监测站 ID 列表，**互不重复**且至少 1 个，均须为已登记站；
- `trigger_count`：触发站数 K，**1 ≤ K ≤ 成员站数量**。

校验：未知站 → **404**（响应给出 `unknown_station_ids`）；重复成员 / 无时区 / 空白名称 / 空成员 / K 越界 → **400**。**登记后配置保持不变**（不提供修改、删除接口）。

联动裁定规则：

1. **自生效时刻起**，按**观测时间**重放成员站待发布时间线中的预警 / 解除事件；生效时刻之前的区域不产生事件；
2. **此前仍在预警的站计入初始状态**：成员站在生效时刻之前最后一个事件是预警（其后无解除）即视为生效时仍预警；**初始即满足 K** 时，在**生效时刻**发出区域预警；
3. **同一时刻先合并所有站的状态变化**，再对预警站数计数：达到 K 发出区域预警、降到 K 以下解除；**每个时刻最多一个区域事件**（同刻一站解除、另一站预警时按合并结果一次性判定，不抖动）；
4. 区域可反复预警 / 解除；查询返回区域当前 `status`、事件 `events` 时间线，以及**每个事件发生时仍在预警的站 ID 列表** `warning_station_ids`（升序）与计数；
5. 成员站的**补传、审核修订、阈值版本调整、设备接管登记（含追溯接管）**后，待发布区域结果**立即整体重算**（与单站重算语义一致：不成立的历史事件随之消失）；
6. **发布时与各站时间线一同冻结**：之后的任何变化只影响待发布结果，重新发布才更新公开结果，**历史发布快照保持不变**；责任区在某次发布之后才登记的，该次历史发布不含它；**尚无发布时公开查询明确返回 404 无结果**。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/zones` | 登记责任区，响应含配置与 `pending_zone`（实时重算的待发布区域结果） |
| GET | `/zones` | 责任区列表（含成员站及登记顺序） |
| GET | `/zones/{id}` | 责任区配置（登记后不变） |
| GET | `/zones/{id}/timeline` | 待发布区域联动结果（实时重算） |
| GET | `/public/zones/{id}/timeline` | 该责任区最近一次发布冻结的联动结果（无发布 → 404） |

待发布区域时间线字段：

```json
{
  "published": false,
  "zone_id": 1,
  "zone_name": "青石峡片区",
  "status": "warning",                 // normal | warning（当前区域状态）
  "active_warning_since": "2026-10-01T00:10:00Z",
  "trigger_count": 2,                  // K
  "warning_station_count": 2,          // 当前预警成员站数
  "warning_station_ids": [1, 2],       // 当前预警成员站 ID（升序）
  "events": [
    {
      "type": "warning",               // warning | cleared
      "at": "2026-10-01T00:10:00Z",
      "warning_station_ids": [1, 2],   // 事件发生时预警的站 ID
      "warning_station_count": 2
    }
  ],
  "members": [
    {"station_id": 1, "station_name": "甲站", "warning": true,  "warning_before_effective": false},
    {"station_id": 2, "station_name": "乙站", "warning": true,  "warning_before_effective": false}
  ]
}
```

`POST /publish` 的响应、`GET /publications/{id}` 与 `GET /public/current` 在原有字段之外**新增** `zones` / `zone_timelines`（公开负载中均带 `published: true`）；原有字段与其他接口的请求响应保持不变。

## 下游值班系统断点领取（一次性密钥 → 按批拉取 → 末编号确认）

下游值班系统以**断点续传**方式消费**公开预警变更**：消费方登记后取得一枚**仅展示一次**的密钥；凭密钥（请求头 `X-Subscriber-Key`）拉取未确认的下一批已冻结发布；每条发布给出其编号及**相对上一发布**的站点/责任区状态前后值与预警、解除事件的**新增/撤销**；确认后游标推进，服务重启后可续读。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/subscribers` | 消费方登记（可选 `{name}`），返回消费方编号与**仅展示一次**的密钥 `key` |
| GET | `/subscriber/releases?limit=100` | 拉取未确认的下一批发布（编号升序，1 ≤ limit ≤ 1000，默认 100） |
| POST | `/subscriber/ack` | 确认批次：`{last_publication_id}`，只接受本批末尾编号 |
| GET | `/subscriber/status` | 查询本消费方游标（已确认编号 / 未确认批次 / 最新发布） |

### 核心语义

1. **仅展示一次的密钥**：`POST /subscribers` 返回随机密钥，服务端只存其 SHA-256 哈希，之后任何接口不再回传明文；缺失/无效密钥（含 `ack`/`status`）一律 **401**；
2. **首批与回放**：新消费方游标为 0（首次以**空快照**比较，全部对象前值 `null`、`new_object: true`，既有事件全部计为新增）；**已有历史发布也可回放**（从发布 1 起按序领取）；
3. **发布条目**：每条给出 `publication_id`、`baseline_publication_id`（所相对的上一发布编号，首批为 `null`）、`published_at`、`note`、`changed`，以及各站/责任区的 `status_before`/`status_after`/`changed`、`events_added`/`events_removed` 和打平后的 `events_added`/`events_removed`（带 `object_type`/`object_id`）与 `summary`；
4. **无变化发布仍占序位**：相邻两次完全相同的发布是独立编号，条目标记 `changed: false`，下游确认/续号不得跳过；
5. **事件身份与撤销**：事件按**对象（站/责任区）+ 类型（warning/cleared）+ 发生时刻 + 该对象同类型出现次数 `occurrence`** 识别（同刻多次同类事件可区分）；**追溯修订（作废/更正/追溯接管/阈值调整）造成的历史事件消失必须显示为撤销**（`events_removed`）；
6. **只认真正冻结的快照**：发布条目只可能来自 `POST /publish` 或受控确认成功产生的发布行；**待发布变化不可见**，**失败的确认不产生发布、故不可见**；
7. **拉取/确认协议**：
   - 建立批次与发布在同一 `BEGIN IMMEDIATE` 事务内完成：**未确认前重复拉取返回同批**（同起止编号、同内容），期间的新发布留到确认后的下一批；
   - 尚无新发布时返回空批（`releases: []`、`last_publication_id: null`、`has_more: false`），空批不改变游标；
   - **确认只接受该批末尾编号**：越序（更大/跳号）或滞后（更小）→ **409** `out_of_order`；无未确认批次却确认非游标编号 → **409** `no_pending_batch`；
   - **重复确认同一末尾编号幂等**（批次已清空、游标恰在该编号时仍返回 200）；
8. **独立性与持久性**：各消费方游标**相互独立**（一行/消费方，随数据库持久化），**服务重启后可续读**；并发拉取与发布由单连接写事务串行化，**不漏项、不串项**（每发布每消费方恰好交付一次）。

### 调用示例

```bash
# 1) 登记，取得仅展示一次的密钥
KEY=$(curl -s -X POST http://127.0.0.1:8000/subscribers \
  -H 'content-type: application/json' -d '{"name":"县防汛值班系统"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')

# 2) 拉取未确认的下一批（未确认前重复调用返回同批）
curl -s -H "X-Subscriber-Key: $KEY" \
  'http://127.0.0.1:8000/subscriber/releases?limit=100'

# 3) 只确认本批末尾编号（重复确认幂等；越序/无批次 409）
curl -s -X POST http://127.0.0.1:8000/subscriber/ack \
  -H "X-Subscriber-Key: $KEY" -H 'content-type: application/json' \
  -d '{"last_publication_id": 12}'

# 4) 游标查询（重启后续读、排查卡点）
curl -s -H "X-Subscriber-Key: $KEY" http://127.0.0.1:8000/subscriber/status
```

拉取响应（本批含发布 11、12；发布 12 中站 1 由预警转正常并新增解除、站 2 为新对象）：

```json
{
  "subscriber_id": 1,
  "acked_publication_id": 10,
  "batch": {
    "from_publication_id": 11,
    "last_publication_id": 12,
    "count": 2,
    "releases": [
      {
        "publication_id": 11,
        "published_at": "2026-10-03T01:00:00Z",
        "note": "整点发布",
        "baseline_publication_id": 10,
        "changed": false,
        "stations": [ {"station_id": 1, "station_name": "青石峡站", "status_before": "warning",
                       "status_after": "warning", "changed": false, "new_object": false,
                       "events_added": [], "events_removed": []} ],
        "zones": [],
        "events_added": [], "events_removed": [],
        "summary": { "stations_total": 1, "stations_status_changed": 0, "stations_new": 0,
                     "zones_total": 0, "zones_status_changed": 0, "zones_new": 0,
                     "events_added": {"total":0,"station_warning":0,"station_cleared":0,"zone_warning":0,"zone_cleared":0},
                     "events_removed": {"total":0,"station_warning":0,"station_cleared":0,"zone_warning":0,"zone_cleared":0} }
      },
      {
        "publication_id": 12,
        "published_at": "2026-10-03T02:00:00Z",
        "note": "解除发布",
        "baseline_publication_id": 11,
        "changed": true,
        "stations": [
          {"station_id": 1, "station_name": "青石峡站", "status_before": "warning",
           "status_after": "normal", "changed": true, "new_object": false,
           "events_added":   [ {"type":"cleared","at":"2026-10-01T00:21:00Z","occurrence":1} ],
           "events_removed": []},
          {"station_id": 2, "station_name": "南河站", "status_before": null,
           "status_after": "normal", "changed": false, "new_object": true,
           "events_added": [], "events_removed": []}
        ],
        "zones": [],
        "events_added": [ {"object_type":"station","object_id":1,"type":"cleared",
                           "at":"2026-10-01T00:21:00Z","occurrence":1} ],
        "events_removed": []
      }
    ]
  },
  "has_more": false,
  "pulled_at": "2026-10-03T02:05:00Z"
}
```

> 自包含演练（自动起临时服务，覆盖一次性密钥、空快照基准、无变化占序、重复拉取同批、越序拒绝/幂等确认、追溯修订显示撤销、失败确认不可见、游标独立与回放）：
> `./examples/subscriber_demo.sh`

## 值班员受控发布（核对 → 凭凭证确认）

值班员发布前必须先**核对**当前待发布裁定相对**最新公开快照**的变化，再凭核对返回的**一次性确认凭证**完成受控发布。直接发布接口 `POST /publish` 保持可用（向后兼容）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/publish/checks` | 发布前核对：返回基准发布编号、状态前后值、新增/消失事件与确认凭证 |
| POST | `/publish/confirm` | 凭凭证 + 可选备注确认发布，成功返回发布编号 |

### 核对 `POST /publish/checks`

- 基准为**最新一次公开快照**；**首次发布以空快照为基准**，核对后才新登记的站点 / 责任区也按空基准比较（前值 `null`、`new_object: true`）；
- 返回**各站及责任区的状态前后值**（`status_before` / `status_after` / `changed`）与**按对象、事件类型、发生时刻和出现次数**计算的新增 / 消失事件（`events_added` / `events_removed`，每对象内同类型事件按时间顺序计第 n 次出现 `occurrence`，同刻多次同类事件可区分）；
- 结果附**确认凭证** `token`（随机一次性令牌，服务端仅存其 SHA-256 哈希）及核对时的数据版本 `data_version`；核对本身不产生发布、不改变待发布与公开结果。

```json
{
  "checked_at": "2026-10-03T02:30:00Z",
  "data_version": 7,
  "token": "cLROkm0sbRkxpl1shN6T2G7wUfi7qkhYTFVBvbVVAcE",
  "baseline_publication_id": 3,
  "baseline_published_at": "2026-10-02T22:00:00Z",
  "stations": [
    {
      "station_id": 1, "station_name": "青石峡站",
      "status_before": "warning", "status_after": "normal",
      "changed": true, "new_object": false,
      "events_added":   [ {"type":"cleared", "at":"2026-10-01T00:21:00Z", "occurrence": 1} ],
      "events_removed": []
    }
  ],
  "zones": [
    {
      "zone_id": 1, "zone_name": "青石峡片区", "trigger_count": 2,
      "status_before": "warning", "status_after": "normal",
      "changed": true, "new_object": false,
      "warning_station_ids_before": [1, 2], "warning_station_ids_after": [],
      "events_added":   [ {"type":"cleared", "at":"2026-10-01T00:21:00Z", "occurrence": 1} ],
      "events_removed": []
    }
  ],
  "events_added":   [ {"object_type":"station","object_id":1,"type":"cleared","at":"...","occurrence":1} ],
  "events_removed": [],
  "summary": { "stations_total": 1, "stations_status_changed": 1, "stations_new": 0,
               "zones_total": 1, "zones_status_changed": 1, "zones_new": 0,
               "events_added":   {"total":2,"station_warning":0,"station_cleared":1,"zone_warning":0,"zone_cleared":1},
               "events_removed": {"total":0,"station_warning":0,"station_cleared":0,"zone_warning":0,"zone_cleared":0} }
}
```

### 确认 `POST /publish/confirm`

请求体：`{"token": "<核对返回的凭证>", "note": "可选备注"}`，成功返回与 `POST /publish` 相同结构的发布结果（含 `publication_id`）。

- **无效凭证**（未知令牌）→ **404**；
- 核对后若发生**影响裁定**的站点、设备、读数补传（幂等同内容重试除外）或修订（作废/更正）、阈值版本登记、**设备接管登记**、责任区登记写入，或**有其他发布先完成**（含 `POST /publish` 或其他成功确认），确认一律 **409** 拒绝（`detail.reason` 区分情形：`data_changed_after_check` / `pending_mismatch` / `token_already_used` / `token_voided`），**不生成发布记录**；
- 确认凭证**只能成功使用一次**：重复确认 → **409**；
- **凭证校验与冻结在同一 `BEGIN IMMEDIATE` 事务内完成**：同一凭证并发确认恰有一次成功；事务内重算待发布结果并核对规范化摘要，**冻结内容与核对时的待发布结果一致**才提交，否则拒绝；
- 阈值预演（`.../preview`）等**只读操作与幂等同内容重试不影响凭证有效性**；
- **历史快照不得改写**：受控发布与直接发布一样只新增发布记录，公开查询与历史版本接口的请求响应保持兼容。

```bash
# 1) 核对（相对最新公开快照；首次为空快照）
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/publish/checks \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 2) 凭凭证确认（可带备注）→ 返回 publication_id
curl -X POST http://127.0.0.1:8000/publish/confirm \
  -H 'content-type: application/json' \
  -d "{\"token\":\"$TOKEN\",\"note\":\"值班员确认发布\"}"
```

## API 一览

### 监测站与设备| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/stations` | 登记监测站（上升阈值必须严格大于解除阈值，否则 400） |
| GET | `/stations` / `/stations/{id}` | 查询监测站 |
| POST | `/stations/{id}/devices` | 登记设备（设备号全局唯一，重复 409） |
| GET | `/stations/{id}/devices` | 查询站内设备 |
| POST | `/stations/{id}/device-takeovers` | 登记汛期设备轮值接管（未知站/设备 404，跨站设备/无时区/空原因 400，同站同刻 409） |
| GET | `/stations/{id}/device-takeovers` | 查询接管记录（按生效时刻升序） |
| POST | `/stations/{id}/threshold-versions` | 登记阈值版本（同站重复生效时间 409，非法阈值/无时区 400，未知站 404） |
| POST | `/stations/{id}/threshold-versions/preview` | **预演**阈值调整对待发布预警的影响（不落库；校验/冲突规则同上，返回本站及所属责任区现状/预演） |
| GET | `/stations/{id}/threshold-versions` | 查询阈值版本历史（含登记基准 baseline） |

### 读数补传（幂等）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/readings` | 补传读数：`{device_no, seq, observed_at, water_level}` |
| GET | `/stations/{id}/readings?include_voided=false` | 查询站内读数 |
| GET | `/readings/{id}` | 查询单条读数 |

- `observed_at` 必须带时区（如 `2026-10-01T08:00:00+08:00` 或 `...Z`），否则 400；
- 水位须为有限数值且在合法范围内，否则 400；未知设备号 404；
- **同设备号 + 同序号 + 同内容**重试 → 200 返回原结果（`deduplicated: true`）；
- **同设备号 + 同序号 + 异内容** → 409 冲突，不写入。

### 审核（作废 / 更正 / 历史）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/readings/{id}/void` | 作废读数，必填 `reason`，可带 `auditor` |
| POST | `/readings/{id}/correct` | 更正观测时间和/或水位，必填 `reason` |
| GET | `/readings/{id}/revisions` | 查询原值与历次修订 |

作废/更正后响应中直接附带重算后的待发布裁定状态。

### 裁定与发布

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/stations/{id}/timeline` | 待发布裁定时间线（实时重算） |
| POST | `/zones` | 登记防汛责任区（未知站 404，重复成员/无时区/K 越界 400） |
| GET | `/zones` / `/zones/{id}` | 查询责任区列表 / 配置 |
| GET | `/zones/{id}/timeline` | 待发布跨站联动结果（实时重算） |
| GET | `/public/zones/{id}/timeline` | 责任区最近一次发布冻结结果（无发布 404） |
| POST | `/publish/checks` | **受控发布核对**：对比待发布结果与最新公开快照（首次为空快照），返回状态前后值、新增/消失事件与一次性确认凭证 |
| POST | `/publish/confirm` | **受控发布确认**：`{token, note?}`，校验与冻结同事务；未知凭证 404，凭证已用/核对后数据变化/他发布先完成 409 |
| POST | `/publish` | 直接发布（保持兼容）：冻结全部监测站与责任区当前时间线 |
| GET | `/publications` / `/publications/{id}` | 查询历史发布版本（含 `zone_timelines`） |
| GET | `/public/current` | 最新发布的公开结果（含 `zone_timelines`） |
| GET | `/public/stations/{id}/timeline` | 某站最新公开时间线 |

### 下游断点领取（X-Subscriber-Key）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/subscribers` | 消费方登记（可选 `name`），返回仅展示一次的密钥 |
| GET | `/subscriber/releases?limit=100` | 拉取未确认的下一批发布差异（相对上一发布；首次空快照；重复拉取同批） |
| POST | `/subscriber/ack` | 确认 `{last_publication_id}`：仅接受批次末尾编号，重复确认幂等，越序/无批次 409 |
| GET | `/subscriber/status` | 查询消费方游标（各消费方独立、重启续读） |

## 调用示例

完整端到端示例脚本（登记 → 补传 → 幂等重试 → 冲突 → 预警 → 发布 → 解除 → 更正 → 重发布 → 作废 → 阈值版本 → **责任区登记/同刻合并/初始状态/发布冻结/公开无结果** → **阈值调整预演/不落库/同刻冲突/登记一致性** → **值班员受控发布核对/确认/变更后拒绝/凭证一次性** → **汛期设备轮值接管/选数/追溯重算/校验/凭证失效/预演选数** → **下游值班系统断点领取/一次性密钥/空快照基准/无变化占序/重复拉取同批/越序拒绝与幂等确认/游标独立/无效密钥**）：

```bash
./examples/demo.sh            # 默认打 http://127.0.0.1:8000
BASE=http://127.0.0.1:8000 ./examples/demo.sh
```

下游断点领取另有**自包含、可重复执行**的专项演练（自动用临时数据库起停服务，另含追溯修订显示撤销、失败确认不可见、历史回放、重启续读）：

```bash
./examples/subscriber_demo.sh                       # 自动起临时服务
BASE=http://127.0.0.1:8000 ./examples/subscriber_demo.sh   # 或复用已运行的服务
```

curl 速览：

```bash
# 登记监测站与设备
curl -X POST http://127.0.0.1:8000/stations \
  -H 'content-type: application/json' \
  -d '{"name":"青石峡站","rise_threshold":10.0,"clear_threshold":8.0}'
curl -X POST http://127.0.0.1:8000/stations/1/devices \
  -H 'content-type: application/json' -d '{"device_no":"DEV-001"}'

# 设备补传读数（带时区观测时间）
curl -X POST http://127.0.0.1:8000/readings \
  -H 'content-type: application/json' \
  -d '{"device_no":"DEV-001","seq":1,"observed_at":"2026-10-01T08:00:00+08:00","water_level":12.0}'

# 查看待发布裁定时间线
curl http://127.0.0.1:8000/stations/1/timeline

# 发布并查看公开结果
curl -X POST http://127.0.0.1:8000/publish
curl http://127.0.0.1:8000/public/current

# 受控发布：先核对拿凭证，再凭凭证确认（核对后有影响裁定的写入或他发布先完成则 409）
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/publish/checks \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -X POST http://127.0.0.1:8000/publish/confirm \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\",\"note\":\"值班员确认\"}"

# 审核更正（带原因）并查看修订历史
curl -X POST http://127.0.0.1:8000/readings/1/correct \
  -H 'content-type: application/json' \
  -d '{"water_level":9.0,"reason":"比测后修正","auditor":"王审"}'
curl http://127.0.0.1:8000/readings/1/revisions

# 防汛方案调整：登记阈值版本（带时区生效时间 + 原因），查询版本历史
curl -X POST http://127.0.0.1:8000/stations/1/threshold-versions \
  -H 'content-type: application/json' \
  -d '{"effective_at":"2026-10-02T08:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"入汛提级"}'
curl http://127.0.0.1:8000/stations/1/threshold-versions

# 登记前预演：同一入参，不落库返回本站及所属责任区的现状(current)/预演(preview)
curl -X POST http://127.0.0.1:8000/stations/1/threshold-versions/preview \
  -H 'content-type: application/json' \
  -d '{"effective_at":"2026-10-02T08:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"入汛提级预演"}'

# 登记防汛责任区（非空名称、带时区生效时间、不重复成员站、触发站数 K=2）
curl -X POST http://127.0.0.1:8000/zones \
  -H 'content-type: application/json' \
  -d '{"name":"青石峡片区","effective_at":"2026-10-02T08:00:00+08:00","station_ids":[1,2],"trigger_count":2}'

# 汛期设备轮值：登记接管（设备号 + 带时区生效时刻 + 非空原因），查询接管记录
curl -X POST http://127.0.0.1:8000/stations/1/device-takeovers \
  -H 'content-type: application/json' \
  -d '{"device_no":"DEV-002","effective_at":"2026-10-02T08:00:00+08:00","reason":"DEV-001 检修，DEV-002 轮值"}'
curl http://127.0.0.1:8000/stations/1/device-takeovers

# 查看待发布跨站联动结果（状态 / 事件时间线 / 事件发生时预警的站 ID）
curl http://127.0.0.1:8000/zones/1/timeline

# 发布后查询冻结的公开结果（责任区尚无发布时返回 404）
curl -X POST http://127.0.0.1:8000/publish
curl http://127.0.0.1:8000/public/zones/1/timeline

# 下游值班系统断点领取：登记取一次性密钥 → 拉取未确认批次 → 确认批次末尾编号
KEY=$(curl -s -X POST http://127.0.0.1:8000/subscribers \
  -H 'content-type: application/json' -d '{"name":"县值班系统"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
curl -s -H "X-Subscriber-Key: $KEY" 'http://127.0.0.1:8000/subscriber/releases?limit=100'
curl -s -X POST http://127.0.0.1:8000/subscriber/ack \
  -H "X-Subscriber-Key: $KEY" -H 'content-type: application/json' \
  -d '{"last_publication_id":1}'
curl -s -H "X-Subscriber-Key: $KEY" http://127.0.0.1:8000/subscriber/status
```

## 并发与一致性设计

- 所有写操作（补传、作废、更正、阈值版本登记、**设备接管登记**、责任区登记、发布）在 **`BEGIN IMMEDIATE` 事务**中执行，并由进程内锁串行化；SQLite 开启 **WAL + busy_timeout**，并发请求不会丢失已提交记录；
- `readings(device_id, seq)` **唯一约束**是幂等的最终防线：并发下同序号写只成功一次，其余按内容比对返回原结果或 409；
- `threshold_versions(station_id, effective_at)` **唯一约束**保证同站生效时刻不重复（生效时间统一归一化为 UTC 存储，不同时区表示的同一时刻同样冲突）；
- `device_takeovers(station_id, effective_at)` **唯一约束**保证同站同一生效瞬间只有一条接管记录（同样按归一化 UTC 比较）；接管选数在单站裁定读取时按观测时间即时计算，补传/更正/作废/追溯登记接管均通过重算待发布时间线即时反映到本站及所属责任区；
- 责任区配置（`zones` / `zone_members`）只登记、不修改，区域联动结果在读取与发布时按成员站当前待发布时间线实时重算；发布在单事务内为全部监测站与责任区生成快照，公开结果读取的是冻结快照，与后续写入（含补传、审核修订、阈值调整）互不影响。
- **阈值预演为纯只读操作**：在一次 `db.read()`（同一连接 + 进程内锁）内完成现状与预演的全部单站/责任区裁定，候选版本仅以内存中的虚拟行（临时 id `-1`）参与计算，不开启写事务、不产生任何数据库写入；因此预演期间拿到的现状/预演取自同一数据视图，且绝不会影响待发布查询与已冻结发布。
- **受控发布的乐观并发控制**：影响裁定的写入（站点/设备登记、读数新增、作废/更正、阈值版本、**设备接管登记**、责任区登记、发布）在同一事务内推进持久化的单调版本号 `app_meta.data_version`；核对时记录版本号与待发布结果摘要，确认时在 `BEGIN IMMEDIATE` 事务内校验版本与摘要、冻结并把凭证标记为已使用——核对与冻结原子完成，并发确认同一凭证恰有一次成功，任何插入的写入或他发布都会使确认以 409 拒绝且不留发布记录。幂等同内容重试与只读预演（阈值预演等）不推进版本。
- **下游断点领取的游标一致性**：消费方密钥仅存哈希；游标（已确认编号 `acked_publication_id` + 未确认批次 `pending_from/pending_to`）按消费方独立行持久化，重启后续读。拉取在 `BEGIN IMMEDIATE` 事务内“确定批次范围 + 建立未确认批次”，与并发发布互斥：发布要么整体落入本批，要么留到下一批，不漏不串；未确认批次期间重复拉取只重放该批范围，不随新发布推进；确认在同事务内核验“末编号 == pending_to”并推进游标、清空批次，重复确认同一末编号幂等。发布差异完全取自 `publication_timelines`/`publication_zones` 的冻结快照（相邻发布互比，首批对空快照），复用与受控核对相同的 `(对象, 类型, 发生时刻, 出现次数)` 事件身份比较——待发布变化与失败确认不产生发布行，故天然对下游不可见。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

覆盖：阈值/时间/水位/设备校验、幂等与冲突、预警与解除规则（含断段、乱序重排、跨段解除）、作废与更正重算、修订历史、**阈值版本（基准回退、同刻采用新版本、跨版本分别判定、调整不产生事件、快照冻结）**、**阈值版本预演（同一校验/同刻 409 冲突、不落库不改待发布与冻结结果、本站与所属责任区现状/预演、其他成员站按待发布联动、同刻合并、与正式登记状态/事件类型/时刻一致）**、**汛期设备轮值接管（登记/列表、首条接管前全选、生效同刻边界、最近接管切换、缺测不借用、未入选可读、追溯/补传/更正/作废后按观测时间重算本站与责任区、校验 400/404/409、接管使核对凭证失效、公开快照须重发布才更新且历史不改写、阈值预演遵循选数、接管与阈值版本正交、跨设备断段）**、**防汛责任区（登记校验、初始状态计入、同刻合并、K 达到/跌破、事件站 ID、补传/修订/阈值调整/**接管**重算、发布冻结与公开无结果、replay_zone 纯函数边界）**、发布冻结、**值班员受控发布（空快照基准、新对象按空基准、状态前后值、按对象/类型/时刻/出现次数的新增消失事件、未知凭证 404、凭证一次性、核对后读数补传/作废/更正/阈值版本/责任区/站点/设备/**接管**写入或他发布先完成 → 409 且无发布记录、幂等重试与只读预演不影响凭证、校验冻结同事务、冻结内容与核对一致、并发确认仅一次成功、历史快照不改写）**、并发补传/发布不丢记录、**下游断点领取（一次性密钥登记仅存哈希、密钥缺失/无效 401、空快照基准与 new_object、无变化发布占序、历史发布回放、重复拉取返回同批、确认仅接受批次末尾编号、越序/滞后/无批次 409、重复确认幂等、各消费方游标独立、重启后续读、追溯修订事件以撤销呈现、重复事件 occurrence 身份、待发布变化与失败确认不可见、受控确认成功可见、责任区差异、limit 分批与 has_more、并发拉取/发布不漏不串、并发同消费方拉取同批）**。

## 项目结构

```
app/
  adjudication.py   # 裁定核心（纯函数：单站分段预警/解除 + 责任区跨站联动重放 replay_zone）
  config.py         # 环境变量配置
  db.py             # SQLite 连接、事务与建表（含 consumers/subscriber_cursors 游标表）
  schemas.py        # 请求模型与校验
  main.py           # FastAPI 路由与应用工厂
tests/
  conftest.py       # 导入期重定向默认数据库，避免污染工作区
  test_api.py       # 端到端测试（158 例）
examples/demo.sh             # 调用示例脚本（全功能）
examples/subscriber_demo.sh  # 下游断点领取自包含演练（自动起临时服务，可重复执行）
Dockerfile / docker-compose.yml
```
