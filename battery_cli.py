#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""程控模拟电池 CLI:直接对接后端服务(与 GUI/MCP 同一通道)。

用法:
    python battery_cli.py status            查看总线与各通道状态
    python battery_cli.py list-vendors      列出可选 CAN 卡(读 Can_Bridge.ini)
    python battery_cli.py open [vendor] [channel] [bitrate]
    python battery_cli.py close
    python battery_cli.py read [ch]         读取某通道(缺省全部)
    python battery_cli.py set <ch> <volts> [amps]      设电压+电流
    python battery_cli.py voltage <ch> <volts>
    python battery_cli.py current <ch> <amps>
    python battery_cli.py relay <ch> <1|0>
    python battery_cli.py raw [ch]          诊断: 打印含原始字节(raw)的通道状态
    python battery_cli.py monitor           实时打印轮询回读事件(监控模式)
"""
from __future__ import annotations

import sys
import time

from battery_server import ServiceClient
from battery_proto import fmt_bitrate

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HEAD = "通道 |  电压(V)  |   电流    | 继电器 | 温度(℃) | 状态"
SEP = "-" * len(HEAD)


def fmt_ch_row(c: dict) -> str:
    if not c.get("online"):
        return f" {c['ch']:>3}  |    ----   |   ----   |  ----  |   ---   | 离线"
    unit = c.get("unit", "A")
    if unit == "A":
        amps = f"{c['amps']:.3f} A"
    else:
        amps = f"{c['amps']:.3f} mA"
    return (f" {c['ch']:>3}  |  {c['volts']:7.3f}  | {amps:>8} | "
            f"{'闭合' if c['relay'] else '断开':^4} |  {c['temp']:>4}  | 在线")


def print_table(chans: list[dict]) -> None:
    print(HEAD)
    print(SEP)
    for c in chans:
        print(fmt_ch_row(c))


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cli = ServiceClient("cli")
    cmd = args[0]
    try:
        if cmd == "status":
            st = cli.request({"cmd": "status"})
            bus = st.get("bus") or {}
            if st.get("connected"):
                print(f"总线: 已连接 [{bus.get('vendor')}:{bus.get('channel')}] @ "
                      f"{fmt_bitrate(bus.get('bitrate'))} · {bus.get('scale')}")
            else:
                print("总线: 未打开")
            print(f"最近轮询: {st.get('last_poll') or '--'}  在线前端: {', '.join(st.get('clients') or []) or '-'}")
            chans = st.get("channels") or []
            online = sum(1 for c in chans if c.get("online"))
            print(f"通道在线: {online}/24")
            print_table(chans)
        elif cmd == "list-vendors":
            r = cli.request({"cmd": "list_vendors"})
            if not (r.get("vendors") or []):
                print("没有可用 CAN 卡:请确认 CAN_DLL/ 已部署厂商 dll 且 Can_Bridge.ini 配置正确。")
            for v in r.get("vendors") or []:
                print(v)
            print(f"默认: {r.get('default_vendor')} @ {r.get('default_channel')}")
        elif cmd == "open":
            # 缺省 vendor/channel 由后端读 Can_Bridge.ini 决定(不再有 sim 仿真)
            req = {}
            if len(args) > 1:
                req["vendor"] = args[1]
            if len(args) > 2:
                req["channel"] = args[2]
            if len(args) > 3:
                req["bitrate"] = int(args[3])
            print(cli.request({"cmd": "open", **req}))
        elif cmd == "close":
            print(cli.request({"cmd": "close"}))
        elif cmd == "read":
            if len(args) > 1:
                ch = int(args[1])
                r = cli.request({"cmd": "poll"})
                chans = r.get("channels") or []
                for c in chans:
                    if c.get("ch") == ch:
                        print_table([c])
                        break
            else:
                r = cli.request({"cmd": "poll"})
                print_table(r.get("channels") or [])
        elif cmd == "raw":
            """诊断输出: 每个在线通道打印 原始字节 + 解析中间值, 用于定标校准。"""
            r = cli.request({"cmd": "poll"})
            chans = r.get("channels") or []
            online = [c for c in chans if c.get("online")]
            print(f"在线 {len(online)}/24 · 格式: ch raw_v raw_i | volts(÷1000) amps(÷1000) | 原字节")
            for c in online:
                print(f"  ch{c['ch']:>2}: raw_v={c.get('raw_v')} raw_i={c.get('raw_i')} | "
                      f"V={c['volts']:+.3f} A={c['amps']:+.3f} | {c.get('raw_hex','')}")
        elif cmd == "set":
            ch, volts = int(args[1]), float(args[2])
            amps = float(args[3]) if len(args) > 3 else None
            req = {"cmd": "set", "ch": ch, "volts": volts}
            if amps is not None:
                req["amps"] = amps
            print(cli.request(req).get("text"))
        elif cmd == "voltage":
            print(cli.request({"cmd": "voltage", "ch": int(args[1]), "volts": float(args[2])}).get("text"))
        elif cmd == "current":
            print(cli.request({"cmd": "current", "ch": int(args[1]), "amps": float(args[2])}).get("text"))
        elif cmd == "relay":
            on = args[2] in ("1", "true", "on", "闭合")
            print(cli.request({"cmd": "relay", "ch": int(args[1]), "on": on}).get("text"))
        elif cmd == "monitor":
            print("监控模式:后端每 ~0.4s 广播一轮 24 通道状态(Ctrl+C 退出)")
            while True:
                ev = cli.poll_event(1.0)
                if ev is None:
                    continue
                if ev.get("ev") == "channels":
                    chans = ev.get("channels") or []
                    online = sum(1 for c in chans if c.get("online"))
                    print(f"\n[{time.strftime('%H:%M:%S')}] 在线 {online}/24")
                    print_table(chans)
                elif ev.get("ev") == "tx":
                    print(f"[TX:{ev.get('from')}] {ev.get('text')}")
                elif ev.get("ev") == "bus":
                    print(f"[总线] {ev.get('state')}")
        else:
            print(f"未知命令: {cmd}", file=sys.stderr)
            return 2
        return 0
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())