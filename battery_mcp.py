#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程控模拟电池 MCP 服务器(stdio 传输):把模拟电池能力以标准 MCP 工具暴露给 AI。

定位:AI 前端。所有工具调用都通过本机 TCP 转发给后端服务 battery_server.py
(后端不存在时自动拉起),因此:
- AI 的每次设定/回读都会被 GUI / CLI 等其他前端实时看到(channels/tx 广播);
- CAN 总线由后端唯一持有并周期轮询 24 通道,AI、GUI、CLI 并存互不抢占;
- 本进程可随时被宿主启停,不影响后端与其他前端。

暴露的工具:
    list_vendors()                     列出可选真实 CAN 卡(读 CAN_DLL/Can_Bridge.ini)
    open_bus(vendor?, channel?, bitrate?, scale?)
                                       打开 CAN 会话;vendor/channel 缺省读 Can_Bridge.ini
    close_bus()                        关闭 CAN 会话
    read_all_channels()                轮询并返回 24 通道最新状态(电压/电流/继电器/温度/在线)
    read_channel(ch)                   读取单个通道状态
    set_voltage(ch, volts)             设定通道输出电压(0~32 V)
    set_current(ch, amps)              设定通道输出电流(0~10 A)
    set_output(ch, volts?, amps?)      同时设定电压与电流(可选其一)
    set_relay(ch, on)                  控制输出继电器闭合/断开(接入/脱离负载)
    get_status()                       查询总线连接、轮询时间、在线前端与最近事件

协议实现:手写最小 MCP/JSON-RPC(initialize / tools/list / tools/call / ping),
不引入任何第三方依赖;输入输出走 stdin/stdout 二进制字节流,规避 Windows 控制台编码问题。

注册示例(DSH / Claude 系 MCP client 配置,把 `<repo>` 换成实际克隆路径):
    {
      "mcpServers": {
        "programmable-battery": {
          "command": "python",
          "args": ["<repo>/battery_mcp.py"]
        }
      }
    }

用法:由 MCP 宿主自动拉起,无需手动运行。
"""
from __future__ import annotations

import json
import sys
import traceback

from battery_server import ServiceClient, ensure_server, server_addr
from battery_proto import fmt_bitrate

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "programmable-battery", "version": "1.0.0"}


def _jsonschema(*, required: list[str], props: dict) -> dict:
    return {"type": "object", "properties": props, "required": required}


CH_DESC = "通道号 1..24(对应一台模拟电池)"
V_DESC = "目标电压,单位 V(约 0~32.0)"
A_DESC = "目标电流,单位 A(约 0~10.0)"

TOOLS = [
    {
        "name": "list_vendors",
        "description": "列出当前可用的真实 CAN 卡(由 CAN_DLL/Can_Bridge.ini 的 VendorList 决定,"
                       "仅含 dll 已部署的厂商:gcan 广成 ECanVci64.dll / cx 创芯 ControlCAN.dll / "
                       "zlg 周立功 zlgcan.dll / zlg_eu),默认波特率 100 kbps。"
                       "与 BMUTesterProject 同款配置;不含仿真器。",
        "inputSchema": _jsonschema(required=[], props={}),
    },
    {
        "name": "open_bus",
        "description": "打开模拟电池的 CAN 总线会话(真实 CAN 卡)。vendor 从 list_vendors 的结果中选"
                       "(默认 INI 的 SelVendor/Vendor,如 gcan/cx/zlg/zlg_eu);channel 用 CAN1/CAN2;"
                       "波特率默认 100 kbps。厂商 dll 需已部署在脚本同级 CAN_DLL/<厂商>/。"
                       "打开后后端自动周期轮询全部 24 通道。",
        "inputSchema": _jsonschema(
            required=[],
            props={
                "vendor": {"type": "string", "description": "CAN 卡厂商,见 list_vendors;默认读 Can_Bridge.ini"},
                "channel": {"type": "string", "description": "通道,CAN1/CAN2;默认读 Can_Bridge.ini"},
                "bitrate": {"type": "integer", "description": "波特率,默认 100000(即 100 kHz)"},
                "scale": {"type": "string", "description": "定标口径,默认 mv_ma(mV/mA);实机校准可选 vx1"},
            },
        ),
    },
    {
        "name": "close_bus",
        "description": "关闭当前 CAN 总线会话(停止轮询,释放后端持有的总线)。",
        "inputSchema": _jsonschema(required=[], props={}),
    },
    {
        "name": "read_all_channels",
        "description": "读取全部 24 通道实时状态(每通道:电压 V、电流 A、电流量纲 A/mA、"
                       "输出继电器、温度 ℃、在线状态)。会先触发一轮轮询保证数据新鲜。",
        "inputSchema": _jsonschema(required=[], props={}),
    },
    {
        "name": "read_channel",
        "description": "读取单个通道的实时状态(电压/电流/继电器/温度/在线)。",
        "inputSchema": _jsonschema(
            required=["ch"],
            props={"ch": {"type": "integer", "minimum": 1, "maximum": 24, "description": CH_DESC}},
        ),
    },
    {
        "name": "set_voltage",
        "description": f"设定某一通道输出电压。{V_DESC}",
        "inputSchema": _jsonschema(
            required=["ch", "volts"],
            props={
                "ch": {"type": "integer", "minimum": 1, "maximum": 24, "description": CH_DESC},
                "volts": {"type": "number", "minimum": 0, "maximum": 32, "description": V_DESC},
            },
        ),
    },
    {
        "name": "set_current",
        "description": f"设定某一通道输出电流。{A_DESC}",
        "inputSchema": _jsonschema(
            required=["ch", "amps"],
            props={
                "ch": {"type": "integer", "minimum": 1, "maximum": 24, "description": CH_DESC},
                "amps": {"type": "number", "minimum": 0, "maximum": 10, "description": A_DESC},
            },
        ),
    },
    {
        "name": "set_output",
        "description": "同时设定某一通道的输出电压与电流(可只给其一,缺省保持原值)。"
                       "注意:设定值仅在输出继电器闭合时才会真正输出。",
        "inputSchema": _jsonschema(
            required=["ch"],
            props={
                "ch": {"type": "integer", "minimum": 1, "maximum": 24, "description": CH_DESC},
                "volts": {"type": "number", "minimum": 0, "maximum": 32, "description": V_DESC + "(可选)"},
                "amps": {"type": "number", "minimum": 0, "maximum": 10, "description": A_DESC + "(可选)"},
            },
        ),
    },
    {
        "name": "set_relay",
        "description": "控制某一通道的输出继电器:on=true 闭合(输出投入/接入负载,设备按设定值输出),"
                       "on=false 断开(输出关断/脱离负载,电压电流归零)。",
        "inputSchema": _jsonschema(
            required=["ch", "on"],
            props={
                "ch": {"type": "integer", "minimum": 1, "maximum": 24, "description": CH_DESC},
                "on": {"type": "boolean", "description": "true=闭合(输出), false=断开(关断)"},
            },
        ),
    },
    {
        "name": "get_status",
        "description": "查询后端状态:总线连接情况(vendor/channel/定标)、最近轮询时间、"
                       "在线前端列表与最近事件。排查问题时先用它。",
        "inputSchema": _jsonschema(required=[], props={}),
    },
]


class McpBackend:
    """到后端服务的连接管理:懒连接 + 断线重连 + 会话参数记忆。"""

    def __init__(self) -> None:
        self.cli: ServiceClient | None = None
        self.last_bus: dict | None = None   # 记忆最近一次成功打开的会话参数

    def connect(self) -> ServiceClient:
        if self.cli is not None and self.cli._alive:
            return self.cli
        try:
            self.cli.close()
        except Exception:
            pass
        host, port = server_addr()
        if ensure_server(host, port) is None:
            raise ConnectionError(f"无法连接或启动后端服务 {host}:{port}")
        self.cli = ServiceClient("mcp")
        return self.cli

    def request(self, obj: dict, timeout: float = 15.0) -> dict:
        return self.connect().request(obj, timeout=timeout)

    def ensure_open(self) -> None:
        """无打开会话时自动重开(用记忆参数或后端按 Can_Bridge.ini 默认);失败抛指引错误。"""
        st = self.request({"cmd": "status"})
        if st.get("connected"):
            return
        params = dict(self.last_bus or {})   # 无记忆 -> 空参数, 由后端读 INI 选默认真实卡
        r = self.request({"cmd": "open", **params})
        if r.get("ok"):
            self.last_bus = params
            return
        raise RuntimeError(
            "CAN 总线未打开,自动重开失败: " + str(r.get("error", ""))
            + "。请先用 list_vendors 确认可用 CAN 卡,再 open_bus(vendor=...)。"
        )


BACKEND = McpBackend()


def _fmt_channel(c: dict) -> str:
    if not c.get("online"):
        return f"  通道 {c['ch']:>2}  —— 离线"
    unit = c.get("unit", "A")
    amps = f"{c['amps']} {unit}" if c.get("unit") != "A" else f"{c['amps']:.3f} A"
    return (f"  通道 {c['ch']:>2}  电压 {c['volts']:.3f} V · 电流 {amps} · "
            f"继电器 {'闭合' if c['relay'] else '断开'} · 温度 {c.get('temp', '--')} ℃ · 在线")


def _fmt_status(st: dict) -> str:
    bus = st.get("bus") or {}
    if st.get("connected"):
        head = (f"总线: 已连接 [{bus.get('vendor')}:{bus.get('channel')}] @ "
                f"{fmt_bitrate(bus.get('bitrate'))} · 定标 {bus.get('scale')}")
    else:
        head = "总线: 未打开(可用 open_bus 打开;默认读 Can_Bridge.ini 选真实 CAN 卡)"
    lines = [
        head,
        f"最近轮询: {st.get('last_poll') or '--'}",
        f"在线前端: {', '.join(st.get('clients') or []) or '仅本服务'}",
    ]
    chans = st.get("channels") or []
    online = sum(1 for c in chans if c.get("online"))
    lines.append(f"通道在线: {online}/24")
    for c in chans[:6]:
        lines.append(_fmt_channel(c))
    if len(chans) > 6:
        lines.append(f"  ...(共 {len(chans)} 通道,其余略)")
    hist = st.get("history") or []
    if hist:
        lines.append("最近事件:")
        lines += [f"  [{h['time']}] {h['kind']} | {h['text']}" for h in hist[-8:]]
    return "\n".join(lines)


def _check_ch(ch) -> tuple[int | None, str | None]:
    try:
        ch = int(ch)
    except (TypeError, ValueError):
        return None, "参数错误: ch 必须是 1..24 的整数"
    if not 1 <= ch <= 24:
        return None, f"参数错误: ch 超出范围 1..24(收到 {ch})"
    return ch, None


def _read_refresh() -> dict:
    """触发一轮轮询并返回 24 通道状态;未开会话则先自动开。"""
    BACKEND.ensure_open()
    r = BACKEND.request({"cmd": "poll"})
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "轮询失败"))
    return r.get("channels") or []


def _find_channel(chans: list[dict], ch: int) -> dict:
    for c in chans:
        if c.get("ch") == ch:
            return c
    return {"ch": ch, "online": False}


def call_tool(name: str, args: dict) -> tuple[str, bool]:
    """执行一个工具,返回 (给模型看的文本, 是否为错误)。"""
    args = args or {}

    if name == "list_vendors":
        r = BACKEND.request({"cmd": "list_vendors"})
        vendors = r.get("vendors") or []
        if not vendors:
            return ("没有可用的 CAN 卡:请确认脚本同级 CAN_DLL/ 下已部署厂商 dll"
                    "且 Can_Bridge.ini 配置正确(参照 BMUTesterProject)。"), True
        lines = [f"可选真实 CAN 卡(来自 CAN_DLL/Can_Bridge.ini,默认波特率 100 kbps):"]
        for v in vendors:
            lines.append(f"- {v}" + ("  (广成 ECanVci64.dll)" if v == "gcan" else
                                     "  (创芯 ControlCAN.dll)" if v == "cx" else
                                     "  (周立功 zlgcan.dll)" if v == "zlg" else
                                     "  (周立功 zlgcan.dll,USBCAN-E-U/2E-U)"))
        lines.append(f"默认: {r.get('default_vendor')} @ {r.get('default_channel')}")
        return "\n".join(lines), False

    if name == "open_bus":
        vendor = str(args.get("vendor") or "").strip()
        channel = str(args.get("channel") or "").strip()
        params: dict = {"bitrate": int(args.get("bitrate", 100000))}
        if vendor:
            params["vendor"] = vendor
        if channel:
            params["channel"] = channel
        if args.get("scale"):
            params["scale"] = str(args["scale"])
        # vendor/channel 缺省时由后端按 Can_Bridge.ini 决定(不传即默认)
        r = BACKEND.request({"cmd": "open", **params})
        if not r.get("ok"):
            return r.get("error", "打开总线失败"), True
        BACKEND.last_bus = params
        return (f"✓ 总线已打开: vendor={r.get('vendor')} channel={r.get('channel')} "
                f"bitrate={fmt_bitrate(r.get('bitrate'))} scale={r.get('scale')} · 真实 CAN 卡\n"
                f"后端已开始周期轮询 24 通道。"), False

    if name == "close_bus":
        r = BACKEND.request({"cmd": "close"})
        return ("✓ 总线会话已关闭", False) if r.get("ok") else (r.get("error", "关闭失败"), True)

    if name == "read_all_channels":
        chans = _read_refresh()
        if not chans:
            return "总线上暂无通道状态(总线未打开?)", True
        online = sum(1 for c in chans if c.get("online"))
        lines = [f"共 {len(chans)} 通道,在线 {online}/24:", ""]
        lines += [_fmt_channel(c) for c in chans]
        return "\n".join(lines), False

    if name == "read_channel":
        ch, err = _check_ch(args.get("ch"))
        if err:
            return err, True
        chans = _read_refresh()
        c = _find_channel(chans, ch)
        return _fmt_channel(c), False

    if name == "set_voltage":
        ch, err = _check_ch(args.get("ch"))
        if err:
            return err, True
        volts = args.get("volts")
        if not isinstance(volts, (int, float)) or isinstance(volts, bool):
            return f"参数错误: volts 必须是数字(V),收到 {volts!r}", True
        volts = float(volts)
        if not 0 <= volts <= 32:
            return f"参数错误: volts 超出范围 0~32 V,收到 {volts}", True
        BACKEND.ensure_open()
        r = BACKEND.request({"cmd": "voltage", "ch": ch, "volts": volts})
        if not r.get("ok"):
            return r.get("error", "发送失败"), True
        return f"✓ 已设通道 {ch} 电压 = {volts:g} V。注意: 继电器闭合才真正输出。", False

    if name == "set_current":
        ch, err = _check_ch(args.get("ch"))
        if err:
            return err, True
        amps = args.get("amps")
        if not isinstance(amps, (int, float)) or isinstance(amps, bool):
            return f"参数错误: amps 必须是数字(A),收到 {amps!r}", True
        amps = float(amps)
        if not 0 <= amps <= 10:
            return f"参数错误: amps 超出范围 0~10 A,收到 {amps}", True
        BACKEND.ensure_open()
        r = BACKEND.request({"cmd": "current", "ch": ch, "amps": amps})
        if not r.get("ok"):
            return r.get("error", "发送失败"), True
        return f"✓ 已设通道 {ch} 电流 = {amps:g} A。注意: 继电器闭合才真正输出。", False

    if name == "set_output":
        ch, err = _check_ch(args.get("ch"))
        if err:
            return err, True
        volts, amps = args.get("volts"), args.get("amps")
        req: dict = {"cmd": "set", "ch": ch}
        if volts is not None:
            if not isinstance(volts, (int, float)) or isinstance(volts, bool) or not 0 <= float(volts) <= 32:
                return f"参数错误: volts 须为 0~32 的数字(V),收到 {volts!r}", True
            req["volts"] = float(volts)
        if amps is not None:
            if not isinstance(amps, (int, float)) or isinstance(amps, bool) or not 0 <= float(amps) <= 10:
                return f"参数错误: amps 须为 0~10 的数字(A),收到 {amps!r}", True
            req["amps"] = float(amps)
        if "volts" not in req and "amps" not in req:
            return "参数错误: set_output 至少需要 volts 或 amps 之一", True
        BACKEND.ensure_open()
        r = BACKEND.request(req)
        if not r.get("ok"):
            return r.get("error", "发送失败"), True
        return f"✓ {r.get('text', '已下发')}。注意: 继电器闭合才真正输出。", False

    if name == "set_relay":
        ch, err = _check_ch(args.get("ch"))
        if err:
            return err, True
        on = bool(args.get("on"))
        BACKEND.ensure_open()
        r = BACKEND.request({"cmd": "relay", "ch": ch, "on": on})
        if not r.get("ok"):
            return r.get("error", "发送失败"), True
        act = "闭合(输出投入)" if on else "断开(输出关断)"
        return f"✓ 通道 {ch} 输出继电器已{act}。", False

    if name == "get_status":
        st = BACKEND.request({"cmd": "status"})
        return _fmt_status(st), False

    return f"未知工具: {name}", True


# ---------------- MCP / JSON-RPC over stdio ----------------


def _write(obj: dict) -> None:
    try:
        sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        raise SystemExit(0)


def handle(msg: dict) -> None:
    method = msg.get("method")
    if msg.get("id") is None:
        return
    if method == "initialize":
        version = msg.get("params", {}).get("protocolVersion") or PROTOCOL_VERSION
        _write({"jsonrpc": "2.0", "id": msg.get("id"), "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }})
    elif method == "tools/list":
        _write({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text, is_error = call_tool(name, args)
        except Exception as ex:
            text = f"工具执行异常: {ex}\n{traceback.format_exc()}"
            is_error = True
        _write({"jsonrpc": "2.0", "id": msg.get("id"), "result": {
            "content": [{"type": "text", "text": text}],
            "isError": bool(is_error),
        }})
    elif method == "ping":
        _write({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})
    else:
        _write({"jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32601, "message": f"方法不存在: {method}"}})


def main() -> None:
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "JSON 解析失败"}})
            continue
        handle(msg)


if __name__ == "__main__":
    main()