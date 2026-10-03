"""
Zero-dependency HTTP layer for the map release & route snapshot service.
Python stdlib only (http.server / json).

Endpoints (all JSON under /api):
  GET  /api/status
  GET  /api/events
  GET  /api/tiles
  GET  /api/candidates        POST /api/candidates
  POST /api/candidates/{id}/validate
  POST /api/candidates/{id}/publish
  GET  /api/releases
  GET  /api/corridors
  GET  /api/patches           POST /api/patches
  POST /api/patches/{id}/activate     /revoke
  POST /api/routes            (create pinned snapshot)
  GET  /api/snapshots
  GET  /api/snapshots/{id}
  POST /api/snapshots/{id}/progress
  POST /api/snapshots/{id}/adopt-reroute
  POST /api/snapshots/{id}/complete
  GET  /api/vehicles/{id}
  GET  /api/vehicles/{id}/sync?last_event_id=N
  GET  /api/view?mode=published|candidate|snapshot|audit&ref=...
Audit (read-only temporal evidence, /api/audit/*):
  POST /api/audit/imports        import hash-chained event batch (idempotent key)
  GET  /api/audit/imports        import verdicts
  GET  /api/audit/events?vehicle_id=&from_seq=&to_seq=
  GET  /api/audit/reconstruct?vehicle_id=&seq=
  POST /api/audit/checkpoints    confirm persistent checkpoint {vehicle_id, seq}
  GET  /api/audit/checkpoints?vehicle_id=
  POST /api/audit/branches       isolated what-if from a checkpoint
  GET  /api/audit/branches
  POST /api/audit/exports        produce a new audit archive
  GET  /api/audit/archives       · /api/audit/archives/{id}
  GET  /api/audit/sample?vehicle_id=   valid sample batch (no persistence)
Static UI served at /
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from audit import AuditEngine
from engine import DomainError, MapEngine, short

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mapstate.json")

ENGINE = MapEngine(DATA_PATH)
AUDIT = AuditEngine(ENGINE)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "HDMapTiles/1.0"

    def log_message(self, fmt, *args):  # concise stderr logs
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    # ------------------------------------------------ helpers ----------
    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data
        except Exception as e:
            raise DomainError("BAD_JSON", f"请求体不是合法 JSON: {e}", 400)

    def _guard(self, fn):
        try:
            return fn()
        except DomainError as e:
            self._send_json(e.to_body(), e.status)
        except FileNotFoundError as e:
            self._send_json({"error": {"code": "NOT_FOUND", "message": str(e)}}, 404)
        except Exception as e:  # never leave the API in an unexplainable state
            self._send_json({"error": {"code": "INTERNAL", "message": repr(e)}}, 500)

    # ------------------------------------------------ routing ----------
    def do_GET(self):
        self._guard(self._do_get)

    def do_POST(self):
        self._guard(self._do_post)

    def _do_get(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path.startswith("/api/"):
            return self._api_get(path, q)
        return self._serve_static(path)

    def _do_post(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        body = self._read_json()

        m = re.match(r"^/api/candidates/([^/]+)/(validate|publish)$", path)
        if m:
            cid, action = m.group(1), m.group(2)
            if action == "validate":
                return self._send_json(ENGINE.validate_candidate(cid))
            return self._send_json(ENGINE.publish_candidate(cid))
        m = re.match(r"^/api/patches/([^/]+)/(activate|revoke)$", path)
        if m:
            pid, action = m.group(1), m.group(2)
            if action == "activate":
                return self._send_json(ENGINE.activate_patch(pid))
            return self._send_json(ENGINE.revoke_patch(pid, body.get("reason", "")))
        m = re.match(r"^/api/snapshots/([^/]+)/(progress|adopt-reroute|complete)$", path)
        if m:
            sid, action = m.group(1), m.group(2)
            if action == "progress":
                return self._send_json(ENGINE.update_progress(sid, body))
            if action == "complete":
                return self._send_json(ENGINE.complete_snapshot(sid))
            return self._send_json(ENGINE.adopt_reroute(sid))

        table = {
            "/api/candidates": lambda: self._send_json(ENGINE.list_candidates()),
            "/api/patches": lambda: self._send_json(ENGINE.create_patch(body)),
            "/api/routes": lambda: self._send_json(ENGINE.create_route(body), 201),
            "/api/tiles": lambda: self._send_json(ENGINE.put_tile(body), 201),
            "/api/audit/checkpoints": lambda: self._send_json(
                AUDIT.create_checkpoint(body.get("vehicle_id"),
                                        int(body.get("seq")),
                                        body.get("note", ""),
                                        body.get("created_by", "auditor")), 201),
            "/api/audit/branches": lambda: self._send_json(
                AUDIT.create_branch(body.get("checkpoint_id"),
                                    body.get("scenario") or {},
                                    body.get("created_by", "auditor")), 201),
            "/api/audit/exports": lambda: self._send_json(
                AUDIT.export_archive(body.get("kind"),
                                     body.get("params") or {},
                                     body.get("created_by", "auditor")), 201),
        }
        if path == "/api/audit/imports":
            payload, status = AUDIT.import_batch(body)
            return self._send_json(payload, status)
        if path in table:
            return table[path]()
        raise DomainError("NOT_FOUND", f"无此端点: POST {path}", 404)

    def _api_get(self, path: str, q: dict):
        m = re.match(r"^/api/snapshots/([^/]+)$", path)
        if m:
            return self._send_json(ENGINE.get_snapshot(m.group(1)))
        m = re.match(r"^/api/vehicles/([^/]+)/sync$", path)
        if m:
            last = int(q.get("last_event_id", ["0"])[0])
            return self._send_json(ENGINE.vehicle_sync(m.group(1), last))
        m = re.match(r"^/api/vehicles/([^/]+)$", path)
        if m:
            return self._send_json(ENGINE.vehicle_snapshots(m.group(1)))
        m = re.match(r"^/api/audit/archives/([^/]+)$", path)
        if m:
            return self._send_json(AUDIT.get_archive(m.group(1)))

        table = {
            "/api/status": lambda: self._send_json(ENGINE.region_status()),
            "/api/events": lambda: self._send_json(
                {"events": ENGINE.list_events(int(q.get("limit", ["60"])[0]))}),
            "/api/tiles": lambda: self._send_json({"tiles": ENGINE.list_tiles()}),
            "/api/candidates": lambda: self._send_json({"candidates": ENGINE.list_candidates()}),
            "/api/releases": lambda: self._send_json({"releases": ENGINE.list_releases()}),
            "/api/corridors": lambda: self._send_json({"corridors": ENGINE.list_corridors()}),
            "/api/patches": lambda: self._send_json({"patches": ENGINE.list_patches()}),
            "/api/snapshots": lambda: self._send_json(self._all_snapshots()),
            "/api/view": lambda: self._send_json(self._view(q)),
            "/api/audit/imports": lambda: self._send_json(
                {"imports": AUDIT.list_imports()}),
            "/api/audit/events": lambda: self._send_json({"events": AUDIT.list_events(
                q.get("vehicle_id", ["VH-01"])[0],
                int(q.get("from_seq", ["1"])[0]),
                int(q.get("to_seq", ["1000000000"])[0]))}),
            "/api/audit/reconstruct": lambda: self._send_json(AUDIT.reconstruct(
                q.get("vehicle_id", [""])[0], int(q.get("seq", ["0"])[0]))),
            "/api/audit/checkpoints": lambda: self._send_json(
                {"checkpoints": AUDIT.list_checkpoints(q.get("vehicle_id", [None])[0])}),
            "/api/audit/branches": lambda: self._send_json(
                {"branches": AUDIT.list_branches()}),
            "/api/audit/archives": lambda: self._send_json(
                {"archives": AUDIT.list_archives()}),
            "/api/audit/sample": lambda: self._send_json(AUDIT.build_sample_batch(
                q.get("vehicle_id", ["VH-01"])[0],
                q.get("import_key", [None])[0])),
        }
        if path in table:
            return table[path]()
        raise DomainError("NOT_FOUND", f"无此端点: GET {path}", 404)

    def _view(self, q: dict):
        mode = q.get("mode", ["published"])[0]
        ref = q.get("ref", [None])[0]
        if mode == "audit":
            vid, _, seq = (ref or "").rpartition("@")
            if not vid or not seq.isdigit():
                raise DomainError("BAD_REQUEST",
                                  "审计视图 ref 格式为 <vehicle_id>@<seq>", 400)
            return AUDIT.describe_audit_view(vid, int(seq))
        return ENGINE.describe_view(mode, ref)

    def _all_snapshots(self) -> dict:
        out = []
        for vid, v in ENGINE.state["vehicles"].items():
            for sid in v["snapshots"]:
                snap = ENGINE.state["snapshots"][sid]
                out.append({
                    "snapshot_id": sid, "vehicle_id": vid,
                    "status": snap["status"], "version": snap["version"]["version"],
                    "from_node": snap["from_node"], "to_node": snap["to_node"],
                    "progress": snap["progress"],
                    "parent_snapshot_id": snap.get("parent_snapshot_id"),
                    "children": snap.get("children", []),
                    "steps": len(snap["route"]),
                    "tiles": [{"tile_id": t, "hash": short(h, 12)}
                              for t, h in sorted(snap["tiles"].items())],
                    "created_at": snap["created_at"]})
        return {"snapshots": sorted(out, key=lambda x: x["created_at"], reverse=True)}

    # ------------------------------------------------ static ------------
    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/")).replace("..", "")
        fp = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(fp):
            self._send_json({"error": {"code": "NOT_FOUND", "message": path}}, 404)
            return
        ext = os.path.splitext(fp)[1]
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(host: str = "0.0.0.0", port: int = 8080):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"HD-map tile service on http://{host}:{port}  (data: {DATA_PATH})")
    httpd.serve_forever()


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    main(port=p)
