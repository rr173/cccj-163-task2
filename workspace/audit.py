"""
Regulatory audit evidence chain — read-only temporal evidence queries.

Adds an append-only, hash-chained evidence log beside the live map service.

Invariants
----------
1. Operators import batches of vehicle-operation events; every event carries
   a sequence number, a source and a content digest over
   {seq, source, kind, ts, payload, parent_digest}. Batches are validated
   atomically: sequence gaps, duplicate events, rewritten digests and
   missing parents are all rejected, and a rejected batch stores nothing
   but its verdict.
2. Import keys are idempotent: re-submitting the same key returns the first
   verdict (accept or reject), whatever the new payload contains.
3. Auditors reconstruct the spatial state at any confirmed moment (version
   descriptor, tile digests, driving steps), confirm persistent checkpoints
   so long chains can resume, and open isolated branches from a checkpoint
   to play out exactly one what-if: a version switch or a road-control
   revocation.
4. Queries, branches and exports only ever create NEW audit records under
   state["audit"]. They never touch live region generations, vehicle
   records, snapshots or the operations event log.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from engine import (DomainError, MapEngine, build_graph, dijkstra, node_key,
                    sha256, short, utcnow_iso)

EVENT_KINDS = ("snapshot_pinned", "progress", "version_adopted", "patch_notice")


def blank_audit() -> dict:
    return {
        "events": {},        # vehicle_id -> [event, ...] (hash-chained, append-only)
        "imports": {},       # import_key -> first verdict (idempotent)
        "checkpoints": {},   # checkpoint_id -> confirmed reconstruction
        "branches": {},      # branch_id -> isolated what-if computation
        "archives": [],      # append-only audit archives (query/branch/export output)
        "seq": {"checkpoint": 0, "branch": 0, "archive": 0},
    }


def event_digest(ev: dict) -> str:
    """Digest over exactly the fields an operator signs off on."""
    return sha256({
        "seq": ev["seq"], "source": ev["source"], "kind": ev["kind"],
        "ts": ev["ts"], "payload": ev["payload"],
        "parent_digest": ev["parent_digest"],
    })


class AuditEngine:
    """Read-only temporal evidence layer over a MapEngine (never mutates it)."""

    def __init__(self, engine: MapEngine):
        self.e = engine

    @staticmethod
    def _audit(s: dict) -> dict:
        return s.setdefault("audit", blank_audit())

    @staticmethod
    def _blank_ctx() -> dict:
        return {"snapshot_id": None, "version": None, "tiles": {}, "route": [],
                "to_node": None, "progress": {"step": 0, "state": "at_node"},
                "closures": {}}

    # ------------------------------------------------------- import ---------
    def import_batch(self, body: dict) -> tuple[dict, int]:
        """Validate + append a batch atomically; idempotent per import_key.

        Returns (payload, http_status). Both accept and reject verdicts are
        persisted under the import key; a resubmission replays the first one.
        """
        key = body.get("import_key")
        vid = body.get("vehicle_id")
        events = body.get("events")
        if not key or not isinstance(key, str):
            raise DomainError("BAD_REQUEST", "import_key 必填（用于幂等判定）", 400)
        if not vid or not isinstance(vid, str):
            raise DomainError("BAD_REQUEST", "vehicle_id 必填", 400)
        if not isinstance(events, list) or not events:
            raise DomainError("BAD_REQUEST", "events 必须是非空数组", 400)

        def accept_or_replay(s):
            a = self._audit(s)
            if key in a["imports"]:
                return "replay", copy.deepcopy(a["imports"][key])
            stored = self._validate_events(s, vid, events)  # raises DomainError
            a["events"].setdefault(vid, []).extend(stored)
            verdict = {"verdict": "accepted", "import_key": key, "vehicle_id": vid,
                       "accepted_events": len(stored),
                       "first_seq": stored[0]["seq"], "last_seq": stored[-1]["seq"],
                       "chain_head_digest": stored[-1]["digest"],
                       "received_at": utcnow_iso(), "http_status": 201}
            a["imports"][key] = verdict
            return "fresh", copy.deepcopy(verdict)

        try:
            kind, verdict = self.e.commit(accept_or_replay)
        except DomainError as err:
            def record_rejection(s):
                a = self._audit(s)
                if key in a["imports"]:
                    return "replay", copy.deepcopy(a["imports"][key])
                verdict = {"verdict": "rejected", "import_key": key, "vehicle_id": vid,
                           "received_at": utcnow_iso(),
                           "error": {"code": err.code, "message": err.message,
                                     "details": err.details},
                           "http_status": err.status}
                a["imports"][key] = verdict
                return "fresh", copy.deepcopy(verdict)
            kind, verdict = self.e.commit(record_rejection)

        payload = {"import_key": key, "idempotent_replay": kind == "replay", **verdict}
        payload.pop("http_status", None)
        return payload, verdict.get("http_status", 200)

    def _validate_events(self, s: dict, vid: str, events: list) -> list[dict]:
        a = self._audit(s)
        chain = a["events"].get(vid, [])
        existing_seqs = {e["seq"] for e in chain}
        existing_digests = {e["digest"] for e in chain}
        cp_by_seq = {c["seq"]: c for c in a["checkpoints"].values()
                     if c["vehicle_id"] == vid}
        stored: list[dict] = []
        prev_seq: Optional[int] = None
        prev_digest: Optional[str] = None
        for raw in events:
            ev = self._normalize_event(raw)
            seq = ev["seq"]
            if seq in existing_seqs or any(e["seq"] == seq for e in stored):
                raise DomainError(
                    "AUDIT_DUPLICATE",
                    f"序号 {seq} 已存在于车辆 {vid} 的证据链（重复事件）", 409,
                    {"seq": seq})
            if ev["digest"] in existing_digests or \
                    any(e["digest"] == ev["digest"] for e in stored):
                raise DomainError(
                    "AUDIT_DUPLICATE",
                    f"事件摘要 {short(ev['digest'], 16)} 已在链上（重复事件）", 409,
                    {"seq": seq, "digest": ev["digest"]})
            actual = event_digest(ev)
            if actual != ev["digest"]:
                raise DomainError(
                    "AUDIT_DIGEST_MISMATCH",
                    f"序号 {seq} 摘要校验失败：摘要与内容不符，事件可能被改写", 422,
                    {"seq": seq, "expected": actual, "provided": ev["digest"]})
            if prev_seq is not None:
                if seq != prev_seq + 1:
                    raise DomainError(
                        "AUDIT_SEQ_GAP",
                        f"序号跳跃：{prev_seq} 之后出现 {seq}，批次内序号必须连续", 422,
                        {"expected": prev_seq + 1, "got": seq})
                if ev["parent_digest"] != prev_digest:
                    raise DomainError(
                        "AUDIT_PARENT_MISSING",
                        f"序号 {seq} 的父摘要与前一事件 {prev_seq} 不符（父事件缺失）",
                        422, {"seq": seq, "expected_parent": prev_digest,
                              "provided_parent": ev["parent_digest"]})
            else:
                # first event of the batch: anchor to chain head or a confirmed
                # checkpoint (long sequences resume from persisted checkpoints)
                if seq == 1 and not chain:
                    expected_parent = None
                elif chain and chain[-1]["seq"] == seq - 1:
                    expected_parent = chain[-1]["digest"]
                elif (seq - 1) in cp_by_seq:
                    expected_parent = cp_by_seq[seq - 1]["last_digest"]
                else:
                    raise DomainError(
                        "AUDIT_PARENT_MISSING",
                        f"无法定位序号 {seq - 1} 的前序事件或已确认检查点"
                        f"（父事件缺失）", 422,
                        {"seq": seq, "needed_parent_seq": seq - 1})
                if ev["parent_digest"] != expected_parent:
                    raise DomainError(
                        "AUDIT_PARENT_MISSING",
                        f"序号 {seq} 声明的父摘要与链上第 {seq - 1} 号不符"
                        f"（父事件缺失）", 422,
                        {"seq": seq, "expected_parent": expected_parent,
                         "provided_parent": ev["parent_digest"]})
            self._validate_payload(ev)
            stored.append(ev)
            prev_seq, prev_digest = seq, ev["digest"]
        return stored

    @staticmethod
    def _normalize_event(raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise DomainError("AUDIT_SCHEMA", "事件必须是 JSON 对象", 400)
        for f in ("seq", "source", "kind", "ts", "payload", "digest",
                  "parent_digest"):
            if f not in raw:
                raise DomainError("AUDIT_SCHEMA", f"事件缺少字段: {f}", 400,
                                  {"missing": f})
        ev = {f: raw[f] for f in ("seq", "source", "kind", "ts", "payload",
                                  "digest", "parent_digest")}
        if not isinstance(ev["seq"], int) or ev["seq"] < 1:
            raise DomainError("AUDIT_SCHEMA", "seq 必须是 ≥1 的整数", 400)
        if not isinstance(ev["source"], str) or not ev["source"]:
            raise DomainError("AUDIT_SCHEMA", "source 必须是非空字符串", 400)
        if ev["kind"] not in EVENT_KINDS:
            raise DomainError("AUDIT_SCHEMA",
                              f"kind 仅支持 {'/'.join(EVENT_KINDS)}", 400)
        if not isinstance(ev["ts"], str):
            raise DomainError("AUDIT_SCHEMA", "ts 必须是 ISO 时间字符串", 400)
        if not isinstance(ev["payload"], dict):
            raise DomainError("AUDIT_SCHEMA", "payload 必须是对象", 400)
        if not isinstance(ev["digest"], str) or \
                not ev["digest"].startswith("sha256:"):
            raise DomainError("AUDIT_SCHEMA", "digest 必须是 sha256: 前缀字符串", 400)
        if ev["parent_digest"] is not None and not isinstance(ev["parent_digest"], str):
            raise DomainError("AUDIT_SCHEMA", "parent_digest 必须是字符串或 null", 400)
        return ev

    @staticmethod
    def _validate_payload(ev: dict) -> None:
        p, kind = ev["payload"], ev["kind"]
        if kind in ("snapshot_pinned", "version_adopted"):
            v = p.get("version")
            if not isinstance(v, dict) or not isinstance(v.get("version"), str):
                raise DomainError("AUDIT_SCHEMA",
                                  f"序号 {ev['seq']}：payload.version.version 必填", 400)
            if not isinstance(p.get("tiles"), dict) or \
                    not isinstance(p.get("route"), list):
                raise DomainError("AUDIT_SCHEMA",
                                  f"序号 {ev['seq']}：payload.tiles/route 必填", 400)
            if p.get("closures") is not None and not isinstance(p["closures"], dict):
                raise DomainError("AUDIT_SCHEMA",
                                  f"序号 {ev['seq']}：payload.closures 必须是对象", 400)
        elif kind == "progress":
            if not isinstance(p.get("step"), int) or p["step"] < 0 or \
                    p.get("state") not in ("at_node", "on_lane"):
                raise DomainError(
                    "AUDIT_SCHEMA",
                    f"序号 {ev['seq']}：progress 需要 step≥0 与 at_node/on_lane", 400)
        elif kind == "patch_notice":
            if not isinstance(p.get("patch_id"), str) or \
                    p.get("action") not in ("activated", "revoked"):
                raise DomainError(
                    "AUDIT_SCHEMA",
                    f"序号 {ev['seq']}：patch_notice 需要 patch_id 与 "
                    f"activated/revoked", 400)
            if p.get("closures") is not None and not isinstance(p["closures"], dict):
                raise DomainError("AUDIT_SCHEMA",
                                  f"序号 {ev['seq']}：closures 必须是对象", 400)

    # -------------------------------------------------- reconstruction ------
    def _apply(self, s: dict, ctx: dict, ev: dict) -> None:
        kind, p = ev["kind"], ev["payload"]
        if kind in ("snapshot_pinned", "version_adopted"):
            ctx["snapshot_id"] = p.get("snapshot_id")
            ctx["version"] = copy.deepcopy(p["version"])
            ctx["tiles"] = dict(p["tiles"])
            ctx["route"] = copy.deepcopy(p["route"])
            ctx["to_node"] = p.get("to_node") or (
                node_key(p["route"][-1]["to_tile"], p["route"][-1]["to_node"])
                if p["route"] else None)
            ctx["progress"] = {"step": 0, "state": "at_node"}
            ctx["closures"] = copy.deepcopy(p.get("closures") or {})
        elif kind == "progress":
            ctx["progress"] = {"step": p["step"], "state": p["state"]}
        elif kind == "patch_notice":
            if p["action"] == "activated":
                closures = p.get("closures") or self._closures_from_patch(s, p["patch_id"])
                for tid, lanes in closures.items():
                    for lid, info in lanes.items():
                        ctx["closures"].setdefault(tid, {})[lid] = {
                            "closed": True, "patch_id": p["patch_id"],
                            "reason": (info or {}).get("reason", "")}
            else:  # revoked
                for tid in list(ctx["closures"]):
                    lanes = ctx["closures"][tid]
                    for lid in list(lanes):
                        if lanes[lid].get("patch_id") == p["patch_id"]:
                            del lanes[lid]
                    if not lanes:
                        del ctx["closures"][tid]

    @staticmethod
    def _closures_from_patch(s: dict, patch_id: str) -> dict:
        p = s["patches"].get(patch_id)
        if not p:
            return {}
        return {tid: {lid: {"reason": p.get("reason", "")} for lid in lids}
                for tid, lids in p["scope"]["tiles"].items()}

    @staticmethod
    def _position(ctx: dict) -> Optional[str]:
        route = ctx.get("route") or []
        prog = ctx.get("progress") or {"step": 0, "state": "at_node"}
        i = prog.get("step", 0)
        if not route or i >= len(route):
            return ctx.get("to_node")
        st = route[i]
        if prog.get("state") == "on_lane":
            # safe-transition rule: the vehicle finishes the current lane first
            return node_key(st["to_tile"], st["to_node"])
        return node_key(st["from_tile"], st["from_node"])

    def _replay_to(self, s: dict, vid: str, seq: int):
        a = self._audit(s)
        chain = a["events"].get(vid, [])
        if not chain:
            raise DomainError("NOT_FOUND", f"车辆 {vid} 没有审计证据链", 404)
        if seq < 1 or seq > chain[-1]["seq"]:
            raise DomainError(
                "AUDIT_RANGE",
                f"序号 {seq} 超出证据链范围 1..{chain[-1]['seq']}", 404,
                {"head_seq": chain[-1]["seq"]})
        cps = [c for c in a["checkpoints"].values()
               if c["vehicle_id"] == vid and c["seq"] <= seq]
        cp = max(cps, key=lambda c: c["seq"], default=None)
        base_seq = cp["seq"] if cp else 0
        ctx = copy.deepcopy(cp["ctx"]) if cp else self._blank_ctx()
        applied = 0
        for ev in chain:
            if base_seq < ev["seq"] <= seq:
                self._apply(s, ctx, ev)
                applied += 1
        return ctx, cp, applied, chain

    def reconstruct(self, vid: str, seq: int) -> dict:
        """Read-only spatial state at a moment: version, tile digests, steps."""
        with self.e.lock:
            s = self.e.state
            ctx, cp, applied, chain = self._replay_to(s, vid, seq)
            head = next(e for e in chain if e["seq"] == seq)
            return {
                "vehicle_id": vid, "seq": seq,
                "resumed_from_checkpoint": cp["checkpoint_id"] if cp else None,
                "checkpoint_seq": cp["seq"] if cp else None,
                "applied_events": applied,
                "event_digest": head["digest"],
                "state": {
                    "snapshot_id": ctx["snapshot_id"],
                    "version": ctx["version"],
                    "tiles": ctx["tiles"],
                    "tile_status": {tid: {"hash": h, "stored": h in s["tiles"]}
                                    for tid, h in ctx["tiles"].items()},
                    "closures": ctx["closures"],
                    "route": ctx["route"],
                    "progress": ctx["progress"],
                    "position": self._position(ctx),
                    "to_node": ctx["to_node"],
                },
            }

    # ---------------------------------------------------- checkpoints -------
    def create_checkpoint(self, vid: str, seq: int, note: str = "",
                          created_by: str = "auditor") -> dict:
        def m(s):
            a = self._audit(s)
            for c in a["checkpoints"].values():
                if c["vehicle_id"] == vid and c["seq"] == seq:
                    out = copy.deepcopy(c)
                    out["already_confirmed"] = True
                    return out
            ctx, _cp, _applied, chain = self._replay_to(s, vid, seq)
            a["seq"]["checkpoint"] += 1
            cid = f"CP-{a['seq']['checkpoint']:04d}"
            last_digest = next(e["digest"] for e in chain if e["seq"] == seq)
            cp = {"checkpoint_id": cid, "vehicle_id": vid, "seq": seq,
                  "note": note, "created_by": created_by,
                  "created_at": utcnow_iso(),
                  "last_digest": last_digest,
                  "ctx": ctx, "ctx_digest": sha256(ctx)}
            a["checkpoints"][cid] = cp
            out = copy.deepcopy(cp)
            out["already_confirmed"] = False
            return out
        return self.e.commit(m)

    # -------------------------------------------------------- branches ------
    def create_branch(self, checkpoint_id: str, scenario: dict,
                      created_by: str = "auditor") -> dict:
        stype = (scenario or {}).get("type")
        if stype not in ("version_switch", "patch_revoke"):
            raise DomainError("BAD_REQUEST",
                              "scenario.type 仅支持 version_switch / patch_revoke", 400)

        def m(s):
            a = self._audit(s)
            cp = a["checkpoints"].get(checkpoint_id)
            if not cp:
                raise DomainError("NOT_FOUND",
                                  f"检查点 {checkpoint_id} 不存在", 404)
            ctx = cp["ctx"]
            if not ctx.get("route") or not ctx.get("to_node"):
                raise DomainError("AUDIT_NO_CONTEXT",
                                  "检查点时刻没有行驶上下文，无法演算分支", 422)
            pos = self._position(ctx)
            prog = ctx.get("progress") or {"step": 0, "state": "at_node"}
            i = prog.get("step", 0)
            remaining = ctx["route"][i:] if prog.get("state") != "on_lane" \
                else ctx["route"][i + 1:]

            if stype == "version_switch":
                tiles, version_str, patches_desc, overlays = \
                    self._resolve_version_switch(s, ctx, scenario)
            else:
                tiles, version_str, patches_desc, overlays = \
                    self._resolve_patch_revoke(s, ctx, scenario)

            missing = [tid for tid, h in tiles.items() if h not in s["tiles"]]
            if missing:
                raise DomainError("AUDIT_TILE_MISSING",
                                  f"分支演算所需瓦片内容不在库中: {missing}", 422,
                                  {"missing": missing})
            tile_objs = [s["tiles"][h] for h in tiles.values()]
            g = build_graph(tile_objs, overlays)
            steps = dijkstra(g, pos, ctx["to_node"])
            a["seq"]["branch"] += 1
            bid = f"BR-{a['seq']['branch']:04d}"
            branch = {
                "branch_id": bid, "checkpoint_id": checkpoint_id,
                "vehicle_id": cp["vehicle_id"], "base_seq": cp["seq"],
                "scenario": scenario, "created_by": created_by,
                "created_at": utcnow_iso(),
                "result": {
                    "position": pos, "to_node": ctx["to_node"],
                    "version_before": (ctx.get("version") or {}).get("version"),
                    "version_after": version_str,
                    "tiles": tiles, "patches": patches_desc,
                    "reachable": steps is not None,
                    "route": steps or [],
                    "steps": len(steps or []),
                    "route_changed": (steps or []) != remaining,
                },
            }
            a["branches"][bid] = branch
            archive = self._archive_locked(a, "branch_result",
                                           {"branch_id": bid},
                                           copy.deepcopy(branch), created_by)
            out = copy.deepcopy(branch)
            out["archive_id"] = archive["archive_id"]
            return out
        return self.e.commit(m)

    def _resolve_version_switch(self, s: dict, ctx: dict, scenario: dict):
        target = (scenario.get("to") or {}).get("regions") or {}
        if not target:
            raise DomainError("BAD_REQUEST",
                              "version_switch 需要 to.regions，如 {\"north\": 1}", 400)
        ctx_base = (ctx.get("version") or {}).get("base") or {}
        merged = {r: b["generation"] for r, b in ctx_base.items()}
        merged.update({r: int(g) for r, g in target.items()})
        tiles: dict[str, str] = {}
        base_desc = {}
        for region, gen in sorted(merged.items()):
            rel = next((r for r in s["releases"]
                        if region in r["regions"]
                        and r["generations"].get(region) == gen), None)
            if not rel:
                raise DomainError(
                    "AUDIT_VERSION_UNKNOWN",
                    f"区域 {region} 不存在第 {gen} 代发布，无法演算版本切换", 404,
                    {"region": region, "generation": gen})
            base_desc[region] = {"release_id": rel["release_id"], "generation": gen}
            for tid, h in rel["tiles"].items():
                if s["tiles"][h]["region"] == region:
                    tiles[tid] = h
        keep = scenario.get("patches")
        if keep is None:
            # road controls in effect at the checkpoint carry over by default
            keep = [p.get("patch_id") for p in
                    (ctx.get("version") or {}).get("patches", [])]
        overlays, patches_desc = self._overlays_for_patches(s, keep)
        gen_part = "/".join(f"{r}={b['generation']}" for r, b in base_desc.items())
        version_str = gen_part + (
            "+" + ",".join(f"{p['patch_id']}[p{p['priority']}]"
                           for p in patches_desc) if patches_desc else "")
        return tiles, version_str, patches_desc, overlays

    def _resolve_patch_revoke(self, s: dict, ctx: dict, scenario: dict):
        pid = scenario.get("patch_id")
        if not pid:
            raise DomainError("BAD_REQUEST", "patch_revoke 需要 patch_id", 400)
        closures = ctx.get("closures") or {}
        ver_patches = (ctx.get("version") or {}).get("patches", [])
        hit = any(info.get("patch_id") == pid
                  for lanes in closures.values() for info in lanes.values()) or \
            any(p.get("patch_id") == pid for p in ver_patches)
        if not hit:
            raise DomainError(
                "AUDIT_NOT_APPLICABLE",
                f"检查点时刻并未适用道路管制 {pid}，无需演算撤销", 422,
                {"patch_id": pid})
        # the what-if reroute uses the FULL tile set pinned by the checkpoint
        # version (same semantics as a live reroute), not just the used subset
        ctx_base = (ctx.get("version") or {}).get("base") or {}
        tiles: dict[str, str] = {}
        if ctx_base:
            for region, b in sorted(ctx_base.items()):
                rel = next((r for r in s["releases"]
                            if region in r["regions"]
                            and r["generations"].get(region) == b["generation"]), None)
                if not rel:
                    raise DomainError(
                        "AUDIT_VERSION_UNKNOWN",
                        f"区域 {region} 第 {b['generation']} 代发布已不可考，无法演算",
                        404, {"region": region, "generation": b["generation"]})
                for tid, h in rel["tiles"].items():
                    if s["tiles"][h]["region"] == region:
                        tiles[tid] = h
        else:
            tiles = dict(ctx["tiles"])
        keep = [p.get("patch_id") for p in ver_patches if p.get("patch_id") != pid]
        overlays, patches_desc = self._overlays_for_patches(s, keep)
        version_str = (ctx.get("version") or {}).get("version", "")
        if "+" in version_str:
            gen_part, patch_part = version_str.split("+", 1)
            toks = [t for t in patch_part.split(",") if not t.startswith(pid + "[")]
            version_str = gen_part + ("+" + ",".join(toks) if toks else "")
        return tiles, version_str, patches_desc, overlays

    def _overlays_for_patches(self, s: dict, patch_ids: list):
        overlays: dict[str, dict] = {}
        desc = []
        for pid in patch_ids:
            p = s["patches"].get(pid)
            if not p:
                raise DomainError("NOT_FOUND", f"补丁 {pid} 不存在", 404)
            for tid, lids in p["scope"]["tiles"].items():
                for lid in lids:
                    overlays.setdefault(tid, {})[lid] = {
                        "closed": True, "patch_id": pid,
                        "reason": p.get("reason", "")}
            desc.append({"patch_id": pid, "priority": p.get("priority", 0),
                         "reason": p.get("reason", "")})
        desc.sort(key=lambda p: -p["priority"])
        return overlays, desc

    # -------------------------------------------------------- archives ------
    def _archive_locked(self, a: dict, kind: str, params: dict,
                        content: Any, created_by: str) -> dict:
        a["seq"]["archive"] += 1
        rec = {"archive_id": f"AR-{a['seq']['archive']:04d}",
               "kind": kind, "params": params, "created_by": created_by,
               "created_at": utcnow_iso(),
               "digest": sha256(content), "content": content}
        a["archives"].append(rec)
        return rec

    def export_archive(self, kind: str, params: dict,
                       created_by: str = "auditor") -> dict:
        params = params or {}
        if kind == "reconstruction":
            content = self.reconstruct(params.get("vehicle_id"),
                                       int(params.get("seq")))
        elif kind == "branch":
            bid = params.get("branch_id")
            with self.e.lock:
                branch = self._audit(self.e.state)["branches"].get(bid)
            if not branch:
                raise DomainError("NOT_FOUND", f"分支 {bid} 不存在", 404)
            content = copy.deepcopy(branch)
        elif kind == "chain":
            vid = params.get("vehicle_id")
            frm, to = int(params.get("from_seq", 1)), int(params.get("to_seq", 10**9))
            with self.e.lock:
                chain = self._audit(self.e.state)["events"].get(vid, [])
            if not chain:
                raise DomainError("NOT_FOUND", f"车辆 {vid} 没有审计证据链", 404)
            to = min(to, chain[-1]["seq"])
            evs = [e for e in chain if frm <= e["seq"] <= to]
            content = {"vehicle_id": vid, "from_seq": frm, "to_seq": to,
                       "events": evs,
                       "head_digest": evs[-1]["digest"] if evs else None}
        else:
            raise DomainError("BAD_REQUEST",
                              "kind 仅支持 reconstruction / branch / chain", 400)

        def m(s):
            return copy.deepcopy(
                self._archive_locked(self._audit(s), kind, params, content, created_by))
        return self.e.commit(m)

    # ----------------------------------------------------------- lists ------
    def list_imports(self) -> list[dict]:
        with self.e.lock:
            return sorted((copy.deepcopy(v) for v in
                           self._audit(self.e.state)["imports"].values()),
                          key=lambda x: x["received_at"], reverse=True)

    def list_events(self, vid: str, frm: int = 1, to: int = 10**9) -> list[dict]:
        with self.e.lock:
            chain = self._audit(self.e.state)["events"].get(vid, [])
            return [copy.deepcopy(e) for e in chain if frm <= e["seq"] <= to]

    def list_checkpoints(self, vid: Optional[str] = None) -> list[dict]:
        with self.e.lock:
            cps = self._audit(self.e.state)["checkpoints"].values()
            out = []
            for c in cps:
                if vid and c["vehicle_id"] != vid:
                    continue
                slim = {k: c[k] for k in ("checkpoint_id", "vehicle_id", "seq",
                                          "note", "created_by", "created_at",
                                          "last_digest", "ctx_digest")}
                slim["version"] = (c["ctx"].get("version") or {}).get("version")
                out.append(slim)
            return sorted(out, key=lambda c: c["created_at"])

    def list_branches(self) -> list[dict]:
        with self.e.lock:
            return sorted((copy.deepcopy(b) for b in
                           self._audit(self.e.state)["branches"].values()),
                          key=lambda b: b["created_at"], reverse=True)

    def list_archives(self) -> list[dict]:
        with self.e.lock:
            return [{k: r[k] for k in ("archive_id", "kind", "params",
                                       "created_by", "created_at", "digest")}
                    for r in reversed(self._audit(self.e.state)["archives"])]

    def get_archive(self, aid: str) -> dict:
        with self.e.lock:
            for r in self._audit(self.e.state)["archives"]:
                if r["archive_id"] == aid:
                    return copy.deepcopy(r)
        raise DomainError("NOT_FOUND", f"档案 {aid} 不存在", 404)

    # ------------------------------------------------------------- view -----
    def describe_audit_view(self, vid: str, seq: int) -> dict:
        """Map payload for the reconstructed moment (review page)."""
        rec = self.reconstruct(vid, seq)
        st = rec["state"]
        with self.e.lock:
            s = self.e.state
            tile_map, missing = {}, []
            for tid, h in st["tiles"].items():
                t = s["tiles"].get(h)
                if t:
                    tile_map[tid] = t
                else:
                    missing.append({"tile_id": tid, "hash": h})
            route = {"steps": st["route"], "progress": st["progress"],
                     "status": "evidence",
                     "from_node": (node_key(st["route"][0]["from_tile"],
                                            st["route"][0]["from_node"])
                                   if st["route"] else None),
                     "to_node": st["to_node"], "parent_snapshot_id": None}
            vlabel = (st["version"] or {}).get("version") or "（无版本证据）"
            view = self.e._render_view(
                s, tile_map, st["closures"], [], "audit", f"{vid}@{seq}",
                f"审计重建 {vid} @ 序号 {seq} · {vlabel}", route=route)
            view["missing_tiles"] = missing
            view["reconstruction"] = {
                "vehicle_id": vid, "seq": seq,
                "resumed_from_checkpoint": rec["resumed_from_checkpoint"],
                "applied_events": rec["applied_events"],
                "position": st["position"],
                "version": st["version"],
                "tile_status": st["tile_status"],
                "closures": st["closures"],
                "progress": st["progress"],
            }
            return view

    # ----------------------------------------------------------- sample -----
    def build_sample_batch(self, vid: str, import_key: Optional[str] = None) -> dict:
        """A valid evidence batch mirroring the vehicle's live snapshot chain
        (used by the demo generator and the UI 'fill sample' button)."""
        with self.e.lock:
            s = self.e.state
            v = s["vehicles"].get(vid)
            if not v or not v["snapshots"]:
                raise DomainError("AUDIT_NO_SNAPSHOT",
                                  f"车辆 {vid} 还没有路线快照，无法生成示例证据", 422)
            events: list[dict] = []
            parent: Optional[str] = None

            def emit(kind: str, payload: dict):
                nonlocal parent
                ev = {"seq": len(events) + 1, "source": f"obu/{vid}",
                      "kind": kind, "ts": utcnow_iso(), "payload": payload,
                      "parent_digest": parent}
                ev["digest"] = event_digest(ev)
                events.append(ev)
                parent = ev["digest"]

            prev_patches: set[str] = set()
            for idx, sid in enumerate(v["snapshots"]):
                snap = s["snapshots"][sid]
                payload = {"snapshot_id": sid, "version": snap["version"],
                           "tiles": snap["tiles"], "route": snap["route"],
                           "to_node": snap["to_node"], "closures": snap["overlays"]}
                if idx == 0:
                    emit("snapshot_pinned", payload)
                    if snap["route"]:
                        emit("progress", {"step": 1, "state": "at_node"})
                else:
                    cur_patches = {p["patch_id"]
                                   for p in snap["version"]["patches"]}
                    for pid in sorted(cur_patches - prev_patches):
                        p = s["patches"].get(pid)
                        closures = {t: {l: {"reason": (p or {}).get("reason", "")}
                                        for l in ls}
                                    for t, ls in
                                    (p["scope"]["tiles"] if p else {}).items()}
                        emit("patch_notice", {"patch_id": pid,
                                              "action": "activated",
                                              "closures": closures})
                    emit("version_adopted", payload)
                    prev_patches = cur_patches
            return {"import_key": import_key
                    or f"OPS-{vid}-{utcnow_iso()[:10]}",
                    "vehicle_id": vid, "events": events}
