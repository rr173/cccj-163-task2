#!/usr/bin/env python3
"""
route_replay.py — deterministic route reproduction / offline catch-up tool.

Replays a pinned snapshot: same version descriptor, same tile hashes, same
lane-level path. Also drives the full lifecycle from the command line.

Usage:
  ./route_replay.py status
  ./route_replay.py plan <vehicle> <from tile/node> <to tile/node>
  ./route_replay.py replay <snapshot_id>
  ./route_replay.py progress <snapshot_id> <step> at_node|on_lane
  ./route_replay.py advisory <snapshot_id>
  ./route_replay.py adopt-reroute <snapshot_id>
  ./route_replay.py complete <snapshot_id>
  ./route_replay.py sync <vehicle> [last_event_id]
  ./route_replay.py patch <tile_id> <lane_id> "<reason>" [priority]
  ./route_replay.py activate|revoke <patch_id>
  ./route_replay.py validate|publish <candidate_id>
  ./route_replay.py events [limit]

Audit (read-only temporal evidence):
  ./route_replay.py audit-import <batch.json>
  ./route_replay.py audit-events <vehicle> [from_seq] [to_seq]
  ./route_replay.py audit-reconstruct <vehicle> <seq>
  ./route_replay.py audit-checkpoint <vehicle> <seq> [note...]
  ./route_replay.py audit-checkpoints <vehicle>
  ./route_replay.py audit-branch <checkpoint> version-switch <region=gen[,region=gen...]>
  ./route_replay.py audit-branch <checkpoint> patch-revoke <patch_id>
  ./route_replay.py audit-branches
  ./route_replay.py audit-export reconstruction <vehicle> <seq>
  ./route_replay.py audit-export branch <branch_id>
  ./route_replay.py audit-export chain <vehicle> [from_seq] [to_seq]
  ./route_replay.py audit-archives

Runs directly against the local store (no HTTP needed). Add --http URL to
talk to a running server instead.
"""

from __future__ import annotations

import json
import sys

from audit import AuditEngine
from engine import MapEngine, short

ENGINE = MapEngine("data/mapstate.json")
AUDIT = AuditEngine(ENGINE)


def j(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_status(_):
    j(ENGINE.region_status())


def cmd_plan(a):
    j(ENGINE.create_route({"vehicle_id": a[0], "from_node": a[1], "to_node": a[2]}))


def cmd_replay(a):
    snap = ENGINE.get_snapshot(a[0])
    sid = snap["snapshot_id"]
    print(f"== 路线复现 {sid} ==")
    print(f"车辆:      {snap['vehicle_id']}")
    print(f"版本:      {snap['version']['version']}")
    print(f"版本哈希:  {snap['version']['descriptor_hash']}")
    print(f"固定瓦片:  " + ", ".join(
        f"{t}={short(h, 12)}" for t, h in sorted(snap["tiles"].items())))
    if snap["version"]["patches"]:
        print("活动补丁:  " + ", ".join(p["patch_id"] for p in snap["version"]["patches"]))
    print(f"状态:      {snap['status']}  进度: step={snap['progress']['step']} "
          f"{snap['progress']['state']}")
    print("路线（逐边）:")
    for i, st in enumerate(snap["route"]):
        if st["kind"] == "seam":
            print(f"  [{i:2d}] 接缝 {st['from_tile']}:{st['from_node']} → "
                  f"{st['to_tile']}:{st['to_node']}  ({st.get('via_port')})")
        else:
            closed = snap["overlays"].get(st["tile"], {}).get(st["lane"], {}).get("closed")
            print(f"  [{i:2d}] 车道 {st['tile']}/{st['lane']}  "
                  f"{st['from_node']}→{st['to_node']} @{st['speed']}"
                  f"{' [补丁封闭]' if closed else ''}")
    # determinism assertion: re-resolving does not change the pinned descriptor
    cur = ENGINE.resolve_effective()
    if cur["descriptor_hash"] != snap["version"]["descriptor_hash"]:
        print(f"\n⚠ 当前线上版本 {cur['version']} 与快照不同——这是允许的："
              f"复现仍严格使用快照固定的瓦片集合")
    else:
        print("\n✓ 当前线上版本与快照一致")
    if snap["advisory"]["closures_ahead"] or snap["advisory"]["notes"]:
        print("\n预警:")
        for n in snap["advisory"]["notes"] + snap["advisory"]["closures_ahead"]:
            print(f"  · [{n['level']}] {n['message']}")


def cmd_progress(a):
    j(ENGINE.update_progress(a[0], {"step": int(a[1]), "state": a[2]}))


def cmd_advisory(a):
    j(ENGINE.get_snapshot(a[0])["advisory"])


def cmd_adopt(a):
    j(ENGINE.adopt_reroute(a[0]))


def cmd_complete(a):
    j(ENGINE.complete_snapshot(a[0]))


def cmd_sync(a):
    last = int(a[1]) if len(a) > 1 else 0
    j(ENGINE.vehicle_sync(a[0], last))


def cmd_patch(a):
    j(ENGINE.create_patch({
        "scope": {"tiles": {a[0]: [a[1]]}}, "reason": a[2],
        "priority": int(a[3]) if len(a) > 3 else 100}))


def make_patch_state_cmd(action: str):
    def cmd(a):
        pid = a[0]
        if action == "activate":
            j(ENGINE.activate_patch(pid))
        else:
            j(ENGINE.revoke_patch(pid, a[1] if len(a) > 1 else ""))
    return cmd


def make_candidate_cmd(action: str):
    def cmd(a):
        cid = a[0]
        if action == "validate":
            j(ENGINE.validate_candidate(cid))
        else:
            j(ENGINE.publish_candidate(cid))
    return cmd


def cmd_events(a):
    j({"events": ENGINE.list_events(int(a[0]) if a else 60)})


# ------------------------------------------------------------- audit --------
def cmd_audit_import(a):
    with open(a[0], "r", encoding="utf-8") as f:
        body = json.load(f)
    payload, status = AUDIT.import_batch(body)
    out = sys.stdout if payload["verdict"] == "accepted" else sys.stderr
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=out)
    if payload["verdict"] != "accepted":
        raise SystemExit(1)


def cmd_audit_events(a):
    vid = a[0]
    frm, to = int(a[1]) if len(a) > 1 else 1, int(a[2]) if len(a) > 2 else 10**9
    j({"events": AUDIT.list_events(vid, frm, to)})


def cmd_audit_reconstruct(a):
    rec = AUDIT.reconstruct(a[0], int(a[1]))
    st = rec["state"]
    print(f"== 审计时刻重建 {rec['vehicle_id']} @ 序号 {rec['seq']} ==")
    if rec["resumed_from_checkpoint"]:
        print(f"续算自:    {rec['resumed_from_checkpoint']}（检查点序号 "
              f"{rec['checkpoint_seq']}），应用 {rec['applied_events']} 条事件")
    else:
        print(f"续算自:    链首（从头回放 {rec['applied_events']} 条事件）")
    print(f"事件摘要:  {rec['event_digest']}")
    v = st["version"] or {}
    print(f"版本:      {v.get('version', '（无版本证据）')}")
    if v.get("descriptor_hash"):
        print(f"版本哈希:  {v['descriptor_hash']}")
    print("瓦片摘要:  " + (", ".join(
        f"{t}={short(ts['hash'], 12)}{'' if ts['stored'] else ' [内容缺失]'}"
        for t, ts in sorted(st["tile_status"].items())) or "（无）"))
    closures = [(t, l, i.get("patch_id")) for t, ls in st["closures"].items()
                for l, i in ls.items()]
    if closures:
        print("道路管制:  " + ", ".join(f"{t}/{l}({p})" for t, l, p in closures))
    print(f"位置:      {st['position']}  进度: step={st['progress']['step']}"
          f"/{len(st['route'])} {st['progress']['state']}  目的地: {st['to_node']}")
    print("行驶步骤（逐边）:")
    cur = st["progress"]["step"]
    for i, s in enumerate(st["route"]):
        mark = "✓" if i < cur else ("▶" if i == cur else " ")
        if s["kind"] == "seam":
            print(f"  [{i:2d}] {mark} 接缝 {s['from_tile']}:{s['from_node']} → "
                  f"{s['to_tile']}:{s['to_node']}  ({s.get('via_port')})")
        else:
            closed = st["closures"].get(s["tile"], {}).get(s["lane"], {}).get("closed")
            print(f"  [{i:2d}] {mark} 车道 {s['tile']}/{s['lane']}  "
                  f"{s['from_node']}→{s['to_node']} @{s['speed']}"
                  f"{' [管制封闭]' if closed else ''}")


def cmd_audit_checkpoint(a):
    j(AUDIT.create_checkpoint(a[0], int(a[1]), " ".join(a[2:]) if len(a) > 2 else ""))


def cmd_audit_checkpoints(a):
    j({"checkpoints": AUDIT.list_checkpoints(a[0] if a else None)})


def cmd_audit_branch(a):
    cp, mode = a[0], a[1]
    if mode == "version-switch":
        regions = {}
        for part in a[2].split(","):
            r, _, g = part.partition("=")
            regions[r.strip()] = int(g)
        scenario = {"type": "version_switch", "to": {"regions": regions}}
    elif mode == "patch-revoke":
        scenario = {"type": "patch_revoke", "patch_id": a[2]}
    else:
        raise SystemExit("分支类型仅支持 version-switch / patch-revoke")
    j(AUDIT.create_branch(cp, scenario))


def cmd_audit_branches(_):
    j({"branches": AUDIT.list_branches()})


def cmd_audit_export(a):
    kind = a[0]
    if kind == "reconstruction":
        params = {"vehicle_id": a[1], "seq": int(a[2])}
    elif kind == "branch":
        params = {"branch_id": a[1]}
    elif kind == "chain":
        params = {"vehicle_id": a[1],
                  "from_seq": int(a[2]) if len(a) > 2 else 1,
                  "to_seq": int(a[3]) if len(a) > 3 else 10**9}
    else:
        raise SystemExit("导出类型仅支持 reconstruction / branch / chain")
    j(AUDIT.export_archive(kind, params))


def cmd_audit_archives(_):
    j({"archives": AUDIT.list_archives()})


CMDS = {
    "status": (cmd_status, 0),
    "plan": (cmd_plan, 3),
    "replay": (cmd_replay, 1),
    "progress": (cmd_progress, 3),
    "advisory": (cmd_advisory, 1),
    "adopt-reroute": (cmd_adopt, 1),
    "complete": (cmd_complete, 1),
    "sync": (cmd_sync, (1, 2)),
    "patch": (cmd_patch, (3, 4)),
    "activate": (make_patch_state_cmd("activate"), 1),
    "revoke": (make_patch_state_cmd("revoke"), (1, 2)),
    "validate": (make_candidate_cmd("validate"), 1),
    "publish": (make_candidate_cmd("publish"), 1),
    "events": (cmd_events, (0, 1)),
    "audit-import": (cmd_audit_import, 1),
    "audit-events": (cmd_audit_events, (1, 3)),
    "audit-reconstruct": (cmd_audit_reconstruct, 2),
    "audit-checkpoint": (cmd_audit_checkpoint, (2, 99)),
    "audit-checkpoints": (cmd_audit_checkpoints, (0, 1)),
    "audit-branch": (cmd_audit_branch, (2, 3)),
    "audit-branches": (cmd_audit_branches, 0),
    "audit-export": (cmd_audit_export, (2, 4)),
    "audit-archives": (cmd_audit_archives, 0),
}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    fn, argc = CMDS[argv[0]]
    args = argv[1:]
    if isinstance(argc, tuple):
        ok = argc[0] <= len(args) <= argc[1]
    else:
        ok = len(args) == argc
    if not ok:
        print(f"参数数量错误（{argv[0]} 需要 {argc} 个参数）\n\n{__doc__}")
        return 2
    try:
        fn(args)
        return 0
    except Exception as e:
        code = getattr(e, "code", "ERROR")
        status = getattr(e, "status", 500)
        print(f"✗ [{status} {code}] {e}", file=sys.stderr)
        details = getattr(e, "details", None)
        if details:
            print(json.dumps(details, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
