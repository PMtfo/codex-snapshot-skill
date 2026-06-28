#!/usr/bin/env python3
"""Minimal JSON-RPC client for `codex app-server` (newline-delimited stdio).

Usage:
  codex-appserver-rpc.py detect
  codex-appserver-rpc.py import <migration_items_json_file>
  codex-appserver-rpc.py list
"""
import json
import subprocess
import sys
import threading
import time

CODEX = "/Applications/Codex.app/Contents/Resources/codex"


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            [CODEX, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._id = 0
        self._responses = {}
        self._notifs = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    self._responses[msg["id"]] = msg
            else:
                self._notifs.append(msg)

    def call(self, method, params=None, timeout=30):
        self._id += 1
        rid = self._id
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if rid in self._responses:
                    return self._responses.pop(rid)
            time.sleep(0.05)
        raise TimeoutError(f"{method} timed out")

    def notify(self, method, params=None):
        req = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "detect"
    c = Client()
    try:
        init = c.call("initialize", {
            "clientInfo": {"name": "ccc-syn-bridge", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        print("INIT:", json.dumps(init.get("result", init), ensure_ascii=False)[:300])
        try:
            c.notify("initialized", {})
        except Exception:
            pass

        if action == "detect":
            res = c.call("externalAgentConfig/detect", {"includeHome": True}, timeout=60)
            out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/detect.json"
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(res.get("result", res), fh, ensure_ascii=False)
            print("DETECT written to", out)
        elif action == "list":
            res = c.call("thread/list", {}, timeout=60)
            r = res.get("result", res)
            txt = json.dumps(r, ensure_ascii=False)
            print("THREAD/LIST len:", len(txt))
            print(txt[:3000])
        elif action == "import":
            items = json.load(open(sys.argv[2]))
            res = c.call("externalAgentConfig/import", {"migrationItems": items, "source": "claude"}, timeout=120)
            print("IMPORT:", json.dumps(res, ensure_ascii=False, indent=2)[:3000])
            # wait for completion notifications
            time.sleep(8)
            for n in c._notifs[-20:]:
                m = n.get("method", "")
                if "import" in m:
                    print("NOTIF", m, json.dumps(n.get("params", {}), ensure_ascii=False)[:1500])
        else:
            print("unknown action", action)
    finally:
        c.close()


if __name__ == "__main__":
    main()
