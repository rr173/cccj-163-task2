#!/usr/bin/env bash
# End-to-end guided demo against the local store (no server needed).
# Safe to re-run: resets data/ first.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf data
PY="python3 route_replay.py"
say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

say "1. 初始状态（north=1/south=1）"
$PY status | python3 -c 'import json,sys;d=json.load(sys.stdin);print(" ",d["current_version"])'

say "2. 校验坏候选 RC-0003（接缝限速 80 + 删除西入口车道）"
$PY validate RC-0003 | python3 -c 'import json,sys
d=json.load(sys.stdin)
print("  status:", d["status"])
[print("  -",e["code"],"@",e["where"],":",e["message"]) for e in d["errors"]]'

say "3. 发布坏候选 -> 422，区域代际不变"
$PY publish RC-0003 2>&1 | sed 's/^/  /' || true

say "4. 校验并发布好候选 RC-0002（内部新增车道，边界不变）-> north 第2代"
$PY validate RC-0002 | python3 -c 'import json,sys;print("  validate:",json.load(sys.stdin)["status"])'
$PY publish RC-0002 | python3 -c 'import json,sys;print("  published:",json.load(sys.stdin)["generations"])'

say "5. 车辆 VH-01 请求 A00/w -> B01/e，固定快照 S-0001"
$PY plan VH-01 A00/w B01/e | python3 -c 'import json,sys;d=json.load(sys.stdin);print(" ",d["snapshot_id"],d["version"]["version"],len(d["tiles"]),"tiles")'

say "6. 紧急封闭 A00/lce（自动验证兼容性与绕行）并激活"
$PY patch A00 lce "K12 事故临时管制" 100 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(" ",d["patch_id"],d["compatibility"]["status"]);print("  detour:",[s.get("lane","seam") for s in d["detour"]["A00/lce"]])'
$PY activate P-0001 | python3 -c 'import json,sys;print(" ",json.load(sys.stdin)["status"])'

say "7. 快照预警：途中出现新封路"
$PY advisory S-0001 | python3 -c 'import json,sys;d=json.load(sys.stdin);[print("  -",c["message"]) for c in d["closures_ahead"]]'

say "8. 在车道上禁止过渡；到达节点后采用绕行子快照"
$PY progress S-0001 1 on_lane >/dev/null
$PY adopt-reroute S-0001 2>&1 | sed 's/^/  /' || true
$PY progress S-0001 1 at_node >/dev/null
$PY adopt-reroute S-0001 | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  child:",d["snapshot_id"],d["version"]["version"]);print("  route:",[s.get("lane","seam") for s in d["route"]])'

say "9. 撤销补丁；历史子快照仍按封路版本可复算"
$PY revoke P-0001 "管制解除" | python3 -c 'import json,sys;print(" ",json.load(sys.stdin)["status"])'

say "10. 确定性复现原始快照"
$PY replay S-0001 | sed -n '1,9p' | sed 's/^/  /'

say "11. 离线车辆 VH-02 重连追赶（从事件 0 回放）"
$PY plan VH-02 A01/n B00/s >/dev/null
$PY sync VH-02 0 | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  replayed:",len(d["missed_events"]),"events; current:",d["current_version"]["version"])'

say "12. 监管审计：导入带序号/来源/摘要链的车辆运行事件"
python3 examples/make_audit_batch.py --vehicle VH-01 --import-key OPS-2026-10-03-001 --out /tmp/audit_batch.json
$PY audit-import /tmp/audit_batch.json | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  verdict:",d["verdict"],"· events:",d["accepted_events"],"· head:",d["chain_head_digest"][:26])'

say "13. 同一导入键再次提交 -> 返回首次判定（幂等）"
$PY audit-import /tmp/audit_batch.json | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  idempotent_replay:",d["idempotent_replay"],"· verdict:",d["verdict"])'

say "14. 重建序号 2 时刻的空间状态（版本描述/瓦片摘要/行驶步骤）"
$PY audit-reconstruct VH-01 2 | sed 's/^/  /'

say "15. 审计员确认检查点（序号 2，持久化供续算）"
$PY audit-checkpoint VH-01 2 "监管抽查确认" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(" ",d["checkpoint_id"],"· ctx_digest:",d["ctx_digest"][:26])'

say "16. 长序列从持久检查点续算（不再从头回放）"
$PY audit-reconstruct VH-01 4 | sed -n '1,3p' | sed 's/^/  /'

say "17. 在序号 4（管制生效后）再确认一个检查点供分支演算"
$PY audit-checkpoint VH-01 4 "分支基准" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(" ",d["checkpoint_id"],"·",d["ctx"]["version"]["version"])'

say "18. 隔离分支 A：从检查点演算一次道路管制撤销（P-0001）"
$PY audit-branch CP-0002 patch-revoke P-0001 | python3 -c 'import json,sys;d=json.load(sys.stdin);r=d["result"];print(" ",d["branch_id"],"· reachable:",r["reachable"],"·",r["version_after"],"· 存档",d["archive_id"]);print("  route:",[s.get("lane","seam") for s in r["route"]])'

say "19. 隔离分支 B：从检查点演算一次版本切换（north=1，管制保持）"
$PY audit-branch CP-0002 version-switch north=1 | python3 -c 'import json,sys;d=json.load(sys.stdin);r=d["result"];print(" ",d["branch_id"],"· reachable:",r["reachable"],"·",r["version_after"],"· 存档",d["archive_id"]);print("  route:",[s.get("lane","seam") for s in r["route"]])'

say "20. 导出审计档案（时刻重建 + 证据链）"
$PY audit-export reconstruction VH-01 4 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(" ",d["archive_id"],d["kind"],"· digest:",d["digest"][:26])'
$PY audit-export chain VH-01 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(" ",d["archive_id"],d["kind"],"· digest:",d["digest"][:26])'

say "21. 防篡改：摘要改写 / 序号跳跃 / 重复事件 / 父事件缺失 全部拒绝"
python3 - <<'EOF'
import json, os, sys
sys.path.insert(0, os.getcwd())
from audit import event_digest
b = json.load(open('/tmp/audit_batch.json')); evs = b['events']
def rechain(events, parent):
    out = []
    for e in events:
        e = dict(e); e['parent_digest'] = parent
        e['digest'] = event_digest(e); out.append(e); parent = e['digest']
    return out
# 摘要被改写：新序号 5，先算好摘要再篡改内容
t = dict(evs[-1], seq=5, kind='progress', payload={'step': 1, 'state': 'at_node'})
[t] = rechain([t], evs[-1]['digest'])
t['payload'] = {'step': 2, 'state': 'on_lane'}          # 篡改内容但不更新摘要
json.dump({'import_key': 'OPS-BAD-DIGEST', 'vehicle_id': 'VH-01', 'events': [t]},
          open('/tmp/audit_bad_digest.json', 'w'), ensure_ascii=False)
# 序号跳跃：5,6,8（摘要链合法，仅序号断档）
gap_evs = [dict(evs[-1]) for _ in range(3)]
for i, e in enumerate(gap_evs):
    e.update(seq=[5, 6, 8][i], kind='progress', payload={'step': 1, 'state': 'at_node'})
json.dump({'import_key': 'OPS-BAD-GAP', 'vehicle_id': 'VH-01',
           'events': rechain(gap_evs, evs[-1]['digest'])},
          open('/tmp/audit_bad_gap.json', 'w'), ensure_ascii=False)
# 重复事件：同一批事件换个导入键再投
dup = json.loads(json.dumps(b)); dup['import_key'] = 'OPS-BAD-DUP'
json.dump(dup, open('/tmp/audit_bad_dup.json', 'w'), ensure_ascii=False)
# 父事件缺失：父摘要指向不存在的来源
orphan = [dict(evs[-1], seq=5, kind='progress', payload={'step': 1, 'state': 'at_node'})]
json.dump({'import_key': 'OPS-BAD-ORPHAN', 'vehicle_id': 'VH-01',
           'events': rechain(orphan, 'sha256:' + '0' * 64)},
          open('/tmp/audit_bad_orphan.json', 'w'), ensure_ascii=False)
EOF
for f in digest gap dup orphan; do
  $PY audit-import /tmp/audit_bad_$f.json 2>&1 | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin); print(" ", d["error"]["code"], "-", d["error"]["message"])
except Exception: pass' || true
done

say "22. 审计全程只读：线上代际 / 车辆记录 / 原事件链未被改变"
$PY status | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  current_version:",d["current_version"])'
$PY events 100 | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  线上审计日志条数:",len(d["events"]),"（无 AUDIT_* 写入）")'

say "完成。启动界面: python3 server.py 8080 -> http://localhost:8080"
