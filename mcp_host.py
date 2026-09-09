#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP host driver:speaks MCP/JSON-RPC over stdio to battery_mcp.py.

用法:
    python mcp_host.py list-vendors
    python mcp_host.py open [vendor] [channel]
    python mcp_host.py read [ch]            # 无 ch = 全 24 通道
    python mcp_host.py voltage <ch> <volts>
    python mcp_host.py current <ch> <amps>
    python mcp_host.py set <ch> [volts] [amps]
    python mcp_host.py relay <ch> <1|0>
    python mcp_host.py status
    python mcp_host.py close
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# MCP 服务器脚本:默认与本文件同目录(battery_mcp.py),可用环境变量覆盖
MCP = os.environ.get("BATTERY_MCP", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "battery_mcp.py"))


class MCPHost:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, MCP],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._id = 0
        self._send({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {}, "clientInfo": {"name": "mcp-host", "version": "1.0"}}})
        self._recv()

    def _next(self):
        self._id += 1
        return self._id

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _recv(self, timeout: float = 20.0):
        import threading

        def _read():
            return self.proc.stdout.readline()

        t = threading.Thread(target=lambda: res.update(line=_read()), daemon=True)
        res: dict = {}
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError("MCP 响应超时")
        line = res.get("line")
        if not line:
            err = self.proc.stderr.read().decode("utf-8", errors="replace")
            raise OSError(f"MCP 进程退出: {err}")
        msg = json.loads(line.decode("utf-8", errors="replace"))
        if "error" in msg:
            raise RuntimeError(f"MCP 错误: {msg['error']}")
        return msg

    def call(self, name: str, args: dict | None = None) -> str:
        self._send({"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                    "params": {"name": name, "arguments": args or {}}})
        msg = self._recv()
        result = msg.get("result", {})
        content = result.get("content") or []
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        is_err = result.get("isError", False)
        return ("[ERR] " if is_err else "") + "\n".join(texts)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    host = MCPHost()
    try:
        cmd = args[0]
        if cmd == "list-vendors":
            print(host.call("list_vendors"))
        elif cmd == "open":
            kw = {}
            if len(args) > 1:
                kw["vendor"] = args[1]
            if len(args) > 2:
                kw["channel"] = args[2]
            print(host.call("open_bus", kw))
        elif cmd == "read":
            if len(args) > 1:
                print(host.call("read_channel", {"ch": int(args[1])}))
            else:
                print(host.call("read_all_channels"))
        elif cmd == "voltage":
            print(host.call("set_voltage", {"ch": int(args[1]), "volts": float(args[2])}))
        elif cmd == "current":
            print(host.call("set_current", {"ch": int(args[1]), "amps": float(args[2])}))
        elif cmd == "set":
            kw = {"ch": int(args[1])}
            if len(args) > 2:
                kw["volts"] = float(args[2])
            if len(args) > 3:
                kw["amps"] = float(args[3])
            print(host.call("set_output", kw))
        elif cmd == "relay":
            print(host.call("set_relay", {"ch": int(args[1]), "on": args[2] in ("1", "true", "on", "闭合")}))
        elif cmd == "status":
            print(host.call("get_status"))
        elif cmd == "close":
            print(host.call("close_bus"))
        else:
            print(f"未知命令: {cmd}", file=sys.stderr)
            return 2
        return 0
    finally:
        host.close()


if __name__ == "__main__":
    sys.exit(main())