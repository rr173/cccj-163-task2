# 高精地图瓦片 · 分区发布与车辆路线快照服务

零第三方依赖（Python 3.11 标准库 + 原生 HTML/JS），实现"地图编辑 → 接缝/跨区路线验证
→ 按区域代际发布 → 车辆路线快照固定 → 紧急补丁安全过渡 → 离线追赶"的完整闭环，
并附带**面向监管审计的只读时序证据查询**（哈希链事件导入 → 时刻空间状态重建 →
持久检查点续算 → 隔离分支演算 → 审计档案导出）。

## 启动

```bash
python3 server.py 8080          # 打开 http://localhost:8080
```

首次启动会自动生成演示世界并落盘到 `data/mapstate.json`（原子写）。
删除 `data/` 即可重置。命令行工具 `route_replay.py` 直接读写同一存储，无需开服务。

## 领域模型与不变量

| 概念 | 说明 |
|---|---|
| 瓦片 `tile` | 内容以 sha256 寻址、入库即不可变；含节点、车道规则、跨瓦片端口（拓扑边界）、bounds |
| 区域 `region` | 发布分区（演示为 north / south）；每个区域维护单调递增的 `generation` |
| 候选 `candidate` | 编辑器产出，绑定**基线代际**；发布前必须通过接缝与跨区走廊校验 |
| 发布 `release` | 按区域代际推进、原子提交；多区域发布要么全成要么全无；发布失败不产生任何新版本 |
| 有效版本 | `north=N/south=M+P-xxxx[p优先级]…`，其描述符再做一次 sha256（descriptor_hash） |
| 补丁 `patch` | **纯规则叠加**（只能封闭既有车道），固化基础版本瓦片哈希；不含几何/拓扑增删 |
| 快照 `snapshot` | 车辆取路线时固定"版本描述符 + 瓦片哈希集合 + 逐边路径"，永不被后续发布/补丁改写 |

**接缝校验**（候选发布闸门）：端口相互声明、全局坐标重合、限速/车道数 profile 一致、
无孤立端口，外加区域瓦片完整性（必须重述本区全部瓦片）与全部跨区验收走廊可贯通。

**新旧拓扑绝不拼接**：在途快照的 descriptor_hash 与瓦片集合恒定；途中有新封路时只发预警。
车辆在车道上（`on_lane`）请求过渡会被拒绝；必须先完成当前车道、到达节点（`at_node`），
才能切换到基于当前版本重算的**子快照**（父子链可审计）。

**补丁安全**：生成时即在"基础版本+封闭叠加"图上验证每条封闭车道仍存在节点间绕行；
激活前再次核对基础瓦片哈希（漂移则拒绝）、兼容性结论与重复封闭。无绕行的补丁标记
`incompatible` 且无法激活。撤销只影响新路线；历史快照仍按其固定补丁复算。

**离线追赶**：车辆带最后确认事件号重连，服务回放全部错过事件，给出唯一当前版本与
在途快照预警；同步本身幂等（事件检查点持久化）。

## 监管审计（只读时序证据）

| 概念 | 说明 |
|---|---|
| 证据事件 `audit event` | 运维导入的车辆运行事件：`seq`（序号）+ `source`（来源）+ `kind` + `payload` + `digest`（对 seq/source/kind/ts/payload/parent_digest 的 sha256）+ `parent_digest`，按车辆构成哈希链 |
| 导入批次 `import` | 整批原子校验：**序号跳跃 / 重复事件 / 摘要被改写 / 父事件缺失** 一律拒绝且不写入任何事件；同一 `import_key` 再次提交返回首次判定（接受或拒绝都幂等） |
| 时刻重建 `reconstruct` | 只读重演某序号时刻的空间状态：版本描述（version/descriptor_hash/基线代际/补丁）、瓦片摘要、道路管制、行驶步骤与进度；网页与 CLI 均可查看 |
| 检查点 `checkpoint` | 审计员把某时刻的重建状态持久化（含 ctx_digest）；长序列重建从最近检查点续算，导入也可以检查点摘要为父锚点 |
| 隔离分支 `branch` | 从已确认检查点出发演算**一次**假设：`version_switch`（切到指定区域代际，默认保持当时管制）或 `patch_revoke`（撤销一项道路管制）；只读线上数据重算路线，结果存为新档案 |
| 审计档案 `archive` | 查询/分支/导出的唯一产物（AR-xxxx，内容带 sha256）；**线上代际、车辆当前记录、原有事件链永不被审计操作改变** |

## API（均为 JSON，前缀 `/api`）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/status` | 区域代际/head/当前版本/活动补丁 |
| GET | `/tiles` · POST `/tiles` | 瓦片清单 · 导入（内容寻址，重复幂等） |
| GET/POST | `/candidates` | 候选列表 · 创建候选 `{regions, tiles:[{tile_id,hash}]}` |
| POST | `/candidates/{id}/validate` | 接缝+跨区走廊校验 |
| POST | `/candidates/{id}/publish` | 按区域代发布（基线过期→409，校验失败→422 原子拒绝） |
| GET | `/releases` · `/corridors` · `/events` | 发布史/验收走廊/审计日志 |
| GET/POST | `/patches` | 补丁队列 · 创建（自动兼容性+绕行校验） |
| POST | `/patches/{id}/activate` · `/revoke` | 生效（复核基础哈希）· 撤销 |
| POST | `/routes` | `{vehicle_id, from_node, to_node}` → 固定快照（201） |
| GET | `/snapshots` · `/snapshots/{id}` | 快照清单 · 完整复现载荷（含 tile 内容/预警） |
| POST | `/snapshots/{id}/progress` | 上报 `{step, state: at_node|on_lane}` |
| POST | `/snapshots/{id}/adopt-reroute` | **仅 at_node** 可过渡到绕行子快照 |
| POST | `/snapshots/{id}/complete` | 完成行程 |
| GET | `/vehicles/{id}` · `/vehicles/{id}/sync?last_event_id=N` | 车辆快照链 · 离线追赶 |
| GET | `/view?mode=published\|candidate\|snapshot\|audit&ref=` | 审查页地图数据（audit 时 ref 为 `车辆@序号`） |
| POST | `/audit/imports` | 导入证据批次 `{import_key, vehicle_id, events[]}`（幂等键，整批原子） |
| GET | `/audit/imports` · `/audit/events?vehicle_id=` | 导入判定记录 · 证据链事件 |
| GET | `/audit/reconstruct?vehicle_id=&seq=` | 重建指定时刻空间状态（只读） |
| GET/POST | `/audit/checkpoints` | 检查点列表 · 确认 `{vehicle_id, seq, note}` |
| GET/POST | `/audit/branches` | 分支列表 · 从检查点演算 `{scenario: version_switch\|patch_revoke}` |
| GET/POST | `/audit/exports` · GET `/audit/archives/{id}` | 导出档案（reconstruction/branch/chain）· 档案内容 |
| GET | `/audit/sample?vehicle_id=` | 生成可导入的示例批次（不落盘） |

## 页面

- **🗺️ 地图审查**：区域代际表、候选卡片（一键校验/发布）、SVG 拓扑图（接缝绿/红点、
  跨区走廊绿线）、结构化校验报告、发布时间线。
- **🚨 封路补丁**：勾选车道生成补丁（自动给出兼容/不兼容与绕行），激活/撤销。
- **🚗 路线复现**：请求路线、模拟"进入车道/到达节点"、封路预警、节点处安全过渡、
  行程完成、离线追赶事件回放。
- **🧾 监管审计**：导入带摘要链的车辆运行事件（幂等键、四类篡改拒绝）、按序号重建
  时刻空间状态（地图 + 版本描述 + 瓦片摘要 + 行驶步骤）、确认持久检查点、从检查点
  演算版本切换/管制撤销隔离分支、导出带摘要的审计档案。

## CLI：route_replay.py

```bash
./route_replay.py status
./route_replay.py plan VH-01 A00/w B01/e          # 固定快照
./route_replay.py replay S-0001                   # 确定性逐边复现（含版本哈希/瓦片哈希）
./route_replay.py progress S-0001 1 at_node
./route_replay.py advisory S-0001
./route_replay.py adopt-reroute S-0001
./route_replay.py patch A00 lce "事故封路" 100
./route_replay.py activate P-0001
./route_replay.py revoke P-0001 "管制解除"
./route_replay.py validate RC-0003
./route_replay.py publish RC-0002
./route_replay.py sync VH-02 0

# 监管审计（只读）
./route_replay.py audit-import batch.json              # 导入证据批次（幂等键）
./route_replay.py audit-reconstruct VH-01 4            # 重建序号 4 的空间状态
./route_replay.py audit-checkpoint VH-01 4 "抽查确认"  # 确认持久检查点
./route_replay.py audit-branch CP-0001 patch-revoke P-0001     # 隔离分支：撤销管制
./route_replay.py audit-branch CP-0001 version-switch north=1  # 隔离分支：版本切换
./route_replay.py audit-export reconstruction VH-01 4  # 导出审计档案
./route_replay.py audit-events VH-01                   # 证据链事件
./route_replay.py audit-archives                       # 档案清单
```

`examples/make_audit_batch.py` 可从车辆实时快照链生成一批合法证据事件
（含连续序号与摘要链），供演示导入。

## 推荐演示路径（种子已备好两个候选）

1. 打开地图审查：当前 `north=1/south=1`，4 条接缝全绿、C1/C2 走廊贯通。
2. 点开 **RC-0003（v3-bad）**：南端口限速改 80 + 删西入口车道 → 接缝红点、
   走廊 C1 断裂；点发布得到 422，版本纹丝不动（审计日志记 PUBLISH_REJECTED）。
3. 校验并发布 **RC-0002（v2，内部新增车道、边界不变）**：north→第 2 代。
4. 路线复现页为 VH-01 请求 `A00/w → B01/e`，固定 S-0001。
5. 封路补丁页封闭 `A00/lce`（生成时给出经 B00 的绕行），激活。
6. 回路线页：S-0001 仍是原版本；点"进入车道"后尝试"采用绕行"→ 被拒；
   点"到达节点"后采用 → 生成子快照 S-0002（版本带 `+P-0001`，全程无车道中途拼接）。
7. 撤销补丁：S-0002 历史复算仍封闭，新路线不再封闭；离线追赶一次性回放所有事件。
8. 监管审计：为 VH-01 生成并导入证据批次 → 重建序号 2/4 时刻 → 确认 CP-0001/CP-0002
   → 从 CP-0002 演算"撤销 P-0001"与"切到 north=1"两条隔离分支 → 导出档案；
   篡改/跳号/重复/缺父四类非法批次全部被拒，线上代际与事件链纹丝不动（demo.sh 12–22 步）。

## 文件

```
engine.py          领域引擎：拓扑图/Dijkstra、校验、发布、补丁、快照、原子持久化
audit.py           审计引擎：证据链校验/时刻重建/检查点/隔离分支/档案（只读线上状态）
server.py          标准库 HTTP API + 静态文件
route_replay.py    路线复现/生命周期 + 审计 CLI
static/            审查页（index.html / app.js / styles.css，含监管审计页签）
examples/          make_audit_batch.py 证据批次生成器
data/mapstate.json 原子落盘的单一事实源（运行后生成；审计数据隔离在 audit 键下）
```
