#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程控模拟电池后端服务:独占持有 CAN 总线,向本机多个前端提供统一的控制与监控通道。

架构(与程控电阻一脉相承):
    ┌─────────┐  ┌─────────┐  ┌──────────┐
    │  GUI    │  │  CLI    │  │ MCP 服务 │  前端:控制接口 + 展示接口(先后无关)
    └────┬────┘  └────┬────┘  └────┬─────┘
         │   TCP 127.0.0.1:51920(JSON 行协议)│
    ┌────┴────────────┴────────────┴─────┐
    │          battery_server.py         │  后端:唯一持有 CAN 总线,轮询+广播
    └─────────────────┬──────────────────┘
                      │ 厂商 DLL 桥接(gcan/cx/zlg/zlg_eu,默认 100 kbps,参考 BMUTesterProject)
                  程控模拟电池总装(通道 1..24)

- 无论哪个前端先启动,都会尝试连接本服务;服务不存在时由前端自动拉起。
- CAN 总线由本服务唯一持有;周期轮询 24 通道(cmd=12 RTR),解析回读,广播给全部前端。
- 并发指令在服务端加锁串行化;多个前端并存互不抢占。
- 可选 CAN 卡由 CAN_DLL/Can_Bridge.ini 的 VendorList 决定(不含仿真器;sim 仅内部测试钩子)。

协议(每行一个 UTF-8 JSON;客户端请求可带 "id",服务响应回带同 id;广播事件无 id):
  客户端 → 服务:
    {"cmd":"hello","name":"gui"}               握手注册,返回 welcome + 状态快照
    {"cmd":"status"}                           查询状态(总线、轮询快照、最近事件)
    {"cmd":"list_vendors"}                     列出可选 CAN 卡(读 Can_Bridge.ini)
    {"cmd":"open","vendor":"cx","channel":"CAN1","bitrate":100000,"scale":"mv_ma"}
                                               打开 CAN 会话(vendor/channel 缺省读 INI)
    {"cmd":"close"}                            关闭 CAN 会话
    {"cmd":"set","ch":1,"volts":3.2,"amps":2.0}设电压+电流(合并 cmd=3),可选只设一项
    {"cmd":"voltage","ch":1,"volts":3.2}       设电压 cmd=0
    {"cmd":"current","ch":1,"amps":1.0}        设电流 cmd=1
    {"cmd":"relay","ch":1,"on":true}           输出继电器 cmd=9
    {"cmd":"unit","ch":1}                      电流档位切换 A⇋mA(cmd=2;设 mA 级小电流前先切档)
    (以上 5 个控制命令均支持批量:"chs":[1,2,3] 替代 "ch"——逐通道下发,
     但只做一次回读同步与广播,远快于逐通道单独请求;响应带 oks/fails 明细)
    {"cmd":"poll"}                             手动触发一轮轮询并返回最新 24 通道状态
    {"cmd":"shutdown"}                         停止服务(自动关闭总线/仿真器)
  服务 → 客户端:
    {"ev":"welcome"/"status", ...}             状态快照
    {"ev":"tx","from":"mcp","cmd":3,"ch":1,"text":"set ch=1 V=3.2 A=2.0"}  发送了某控制命令
    {"ev":"channels","from":"poll"}            24 通道最新状态表(轮询后广播)
    {"ev":"relay","ch":1,"on":true,"from":"mcp"} 继电器切换事件
    {"ev":"bus","state":"opened/closed/lost",...} 总线会话事件
    {"ev":"clients","count":2,"names":[...]}      在线前端变化
    {"ev":"bye"}                                  后端即将退出

依赖:python-can(仅真实 CAN 卡需要;sim 模式零第三方依赖)。
用法:python battery_server.py [TCP端口]
"""
from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from battery_bus import (
    CanBus, CanBusError, DLL_VENDORS, available_vendors,
    default_channel, default_scale, default_vendor,
)
from battery_proto import (
    CH_MAX, CH_MIN, CMD_READ, MASTER_ADDR, Scale, check_channel, make_id,
    parse_param_frame,
    frame_set_voltage, frame_set_current, frame_set_both, frame_relay,
    frame_unit_switch,
)
from battery_offset import load_offsets

DEFAULT_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 51920
POLL_INTERVAL = 0.1        # 轮询周期(秒):实测单通道应答~2.6ms,单轮~55-70ms → 0.1s 周期接近上限
POLL_GAP = 0.002           # 轮询帧间隔 ≈ 2ms(实测应答 2.6ms 可支撑;原 4ms 保守值)
MAX_WAIT = 10.0
HISTORY_LEN = 300          # 内存中保留的最近事件条数
OFFLINE_ROUNDS = 6         # 连续 N 轮未应答判离线(§5.3)

# 后端协议版本:每次改动"请求/响应字段语义"就 +1(如 quiet 静默、chs 批量、proto 自身)。
# 端口只能被一个进程占用,旧后端进程会继续为新前端服务——前端按此字段判断自己连到的
# 是不是过旧的后端,避免"新界面 + 旧后端"静默忽略新字段(实测踩坑:quiet 被忽略导致
# SOC 自动下发仍然逐条刷 TX 日志)。
PROTO_VERSION = 2

SERVER_PATH = Path(__file__).with_name("battery_server.py")


def server_addr() -> tuple[str, int]:
    """解析后端地址:环境变量 BATTERY_SERVER=host:port 可覆盖,默认本机 51920。"""
    raw = os.environ.get("BATTERY_SERVER", "")
    if ":" in raw:
        host, _, port = raw.rpartition(":")
        try:
            return host or DEFAULT_HOST, int(port)
        except ValueError:
            pass
    return DEFAULT_HOST, DEFAULT_TCP_PORT


def _spawn_flags() -> int:
    if os.name != "nt":
        return 0
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    return DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def probe(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_server(host: str | None = None, port: int | None = None, wait: float = 3.0):
    """确保后端在运行;返回 (host, port) 或 None。"""
    h, p = (host, port) if host and port else server_addr()
    if probe(h, p):
        return (h, p)
    if h not in ("127.0.0.1", "localhost"):
        return None
    try:
        subprocess.Popen(
            [sys.executable, str(SERVER_PATH), str(p)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_spawn_flags(), start_new_session=(os.name != "nt"),
        )
    except OSError:
        return None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if probe(h, p):
            return (h, p)
        time.sleep(0.15)
    return None


class ServiceClient:
    """后端客户端:请求-响应(按 id 关联)+ 事件订阅。"""

    def __init__(self, name: str, host: str | None = None, port: int | None = None):
        addr = ensure_server() if host is None and port is None else (host, port)
        if not addr:
            raise ConnectionError(f"无法连接或启动后端服务 ({host}, {port})")
        self.host, self.port = addr
        self.name = name
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.settimeout(None)
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, dict | None]] = {}
        self.events: queue.Queue[dict] = queue.Queue()
        self._alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        self.request({"cmd": "hello", "name": name})

    def request(self, obj: dict, timeout: float = 15.0) -> dict:
        with self._send_lock:
            self._next_id += 1
            rid = self._next_id
            obj = {**obj, "id": rid}
            self.sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        done = threading.Event()
        holder: list[dict | None] = [None]
        self._pending[rid] = (done, holder)
        if not done.wait(timeout):
            self._pending.pop(rid, None)
            raise TimeoutError(f"后端响应超时: {obj.get('cmd')}")
        resp = holder[0]
        if resp is None:
            raise ConnectionError("后端连接已断开")
        return resp

    def poll_event(self, timeout: float = 0.05) -> dict | None:
        try:
            return self.events.get(timeout=timeout)
        except Exception:
            return None

    def _reader(self) -> None:
        buf = b""
        try:
            while self._alive:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    rid = msg.pop("id", None)
                    if rid is not None and rid in self._pending:
                        done, holder = self._pending.pop(rid)
                        holder[0] = msg
                        done.set()
                    else:
                        self.events.put(msg)
        except OSError:
            pass
        finally:
            self._alive = False
            for done, holder in list(self._pending.values()):
                done.set()

    def close(self) -> None:
        self._alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class _ClientBox:
    """一个在线前端:独立发送队列 + 写线程。"""

    def __init__(self, sock: socket.socket, name: str):
        self.sock = sock
        self.name = name
        self.alive = True
        self._queue: list[dict] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def send_obj(self, obj: dict) -> None:
        with self._cv:
            self._queue.append(obj)
            self._cv.notify()

    def send_now(self, obj: dict) -> None:
        with self._cv:
            try:
                self.sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                self.alive = False

    def _pump(self) -> None:
        while True:
            with self._cv:
                while self.alive and not self._queue:
                    self._cv.wait()
                if not self.alive and not self._queue:
                    return
                obj = self._queue.pop(0)
            try:
                self.sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                self.alive = False


class BatteryServer:
    """后端主体:管理 CAN 会话、串行化指令、周期轮询、向全部前端广播。"""

    def __init__(self, tcp_port: int):
        self.tcp_port = tcp_port
        self.cond = threading.Condition(threading.RLock())
        # 总线 IO 互斥:串行化 poll 线程与客户端线程的"发+等应答"事务,
        # 避免并发收发导致应答被错抢/配对错乱(审查 S1)。与 self.cond 独立,
        # cond 管状态与历史,此锁只保护 CanBus 收发临界区。
        self._bus_io_lock = threading.Lock()
        self.clients: list[_ClientBox] = []
        self.bus: CanBus | None = None
        self.bus_info: dict | None = None
        # 24 通道回读状态:ch -> dict(缺省 None)
        self.channels: dict[int, dict | None] = {ch: None for ch in range(CH_MIN, CH_MAX + 1)}
        self.online_rounds: dict[int, int] = {ch: 0 for ch in range(CH_MIN, CH_MAX + 1)}
        self.history: deque[tuple] = deque(maxlen=HISTORY_LEN)
        self.poll_running = False
        self.poll_thread: threading.Thread | None = None
        self.last_poll_ts: float | None = None
        self.stop_requested = False
        self._bus_recover_cool = 0.0   # 通道复位冷却(避免连续失败反复复位)

    # ---------- 状态与广播 ----------

    def _status_locked(self) -> dict:
        return {
            "connected": self.bus is not None,
            "proto": PROTO_VERSION,      # 前端据此识别"端口被旧后端进程占用"
            "bus": self.bus_info,
            "last_poll": time.strftime("%H:%M:%S", time.localtime(self.last_poll_ts))
            if self.last_poll_ts else None,
            "clients": [c.name for c in self.clients],
            "channels": self._channels_summary(),
            "history": [{"time": time.strftime("%H:%M:%S", time.localtime(ts)),
                         "kind": k, "text": t}
                        for ts, k, t in list(self.history)[-20:]],
        }

    def _channels_summary(self) -> list[dict]:
        rows = []
        for ch in range(CH_MIN, CH_MAX + 1):
            st = self.channels.get(ch)
            if st is None:
                rows.append({"ch": ch, "online": False})
            else:
                rows.append({**st, "online": True})
        return rows

    def _fmt_channel_row(self, c: dict) -> str:
        if not c.get("online"):
            return f"  通道 {c['ch']:>2}  —— 离线"
        unit = c.get("unit", "A")
        amps = f"{c['amps']:.3f} A" if unit != "mA" else f"{c['amps']:.3f} mA"
        raw = f" raw[{c.get('raw_v')}/{c.get('raw_i')} {c.get('raw_hex', '')}]"
        return (f"  通道 {c['ch']:>2}  电压 {c['volts']:+.3f} V · 电流 {amps} · "
                f"继电器 {'闭合' if c['relay'] else '断开'} · 温度 {c.get('temp', '--')} ℃" + raw)

    def _record(self, kind: str, text: str) -> None:
        self.history.append((time.time(), kind, text))

    def broadcast(self, obj: dict) -> None:
        with self.cond:
            dead = [c for c in self.clients if not c.alive]
            for c in dead:
                self.clients.remove(c)
            changed = bool(dead)
            for c in list(self.clients):
                c.send_obj(obj)
            if changed:
                names = [c.name for c in self.clients]
                for c in self.clients:
                    c.send_obj({"ev": "clients", "count": len(names), "names": names})

    # ---------- 总线会话与轮询 ----------

    def _close_locked(self, reason: str = "closed") -> None:
        self.poll_running = False
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
            info = self.bus_info or {}
            self.bus = None
            self.bus_info = None
            self.channels = {ch: None for ch in range(CH_MIN, CH_MAX + 1)}
            self.online_rounds = {ch: 0 for ch in range(CH_MIN, CH_MAX + 1)}
            self.last_poll_ts = None
            self._record("bus", f"关闭 {info.get('vendor')}:{info.get('channel')}")
            self.broadcast({"ev": "bus", "state": reason, **info})

    def _poll_loop(self) -> None:
        """周期轮询 24 通道(cmd=12 RTR),解析回读并广播;含 §5.3 离线判定。"""
        while True:
            with self.cond:
                if not self.poll_running or self.bus is None:
                    return
            ts = time.time()
            try:
                self.do_poll_round()
            except Exception as ex:
                with self.cond:
                    self._record("error", f"轮询异常: {ex}")
            self.last_poll_ts = ts
            # 广播最新整表
            self.broadcast({"ev": "channels", "from": "poll", "channels": self._channels_summary()})
            time.sleep(POLL_INTERVAL)

    def do_poll_round(self) -> None:
        """一轮:依次向 1..24 发 RTR,读应答并写回 self.channels。

        整体持有 _bus_io_lock,与 handle_control 的发送互斥(单一总线所有者),
        避免轮询 RTR 与控制帧的应答被并发 recv 抢读错配(审查 S1)。
        """
        with self.cond:
            if self.bus is None:
                return
            bus = self.bus
            scale = bus.scale  # 用当前会话定标解析(不是默认 mv_ma!)
        with self._bus_io_lock:
            rx_err = False
            for ch in range(CH_MIN, CH_MAX + 1):
                try:
                    self._poll_send(bus, ch)
                except CanBusError as ex:
                    with self.cond:
                        self._record("error", f"轮询 ch{ch}: {ex}")
                    # 发送失败(多为 bus-off):先自动复位通道再重发本通道,
                    # 成功则本轮继续;仍失败才放弃本轮,由下一轮重试。
                    if not self._recover_bus():
                        return
                    try:
                        self._poll_send(bus, ch)
                    except CanBusError as ex2:
                        with self.cond:
                            self._record("error", f"轮询 ch{ch} 复位后仍失败: {ex2}")
                        return
                # 应在一帧内回;允许少量重试窗口
                deadline = time.monotonic() + 0.05
                got = False
                while time.monotonic() < deadline:
                    try:
                        f = bus.recv(timeout=0.05)
                    except CanBusError as ex:
                        # 接收侧硬件错误(如 bus-off):本轮按"未应答"处理,
                        # 每轮只记一条,避免 24 通道刷屏。
                        if not rx_err:
                            rx_err = True
                            with self.cond:
                                self._record("error", f"轮询接收失败: {ex}")
                        f = None
                    if f is None:
                        break
                    parsed = parse_param_frame(f["arb_id"], f["data"], scale)
                    if parsed is None:
                        continue
                    parsed["raw_hex"] = f["data"].hex(" ").upper()  # 原始回读字节(诊断/定标用)
                    got = True
                    self._apply_channel(parsed)
                    break  # 每通道只等本通道应答
                if not got:
                    with self.cond:
                        self.online_rounds[ch] += 1
                        if self.online_rounds[ch] >= OFFLINE_ROUNDS \
                           and self.channels.get(ch) is not None:
                            self.channels[ch] = None
                            self._record("offline", f"ch{ch} 连续 {OFFLINE_ROUNDS} 轮未应答,判通信中断")
                time.sleep(POLL_GAP)

    def _apply_channel(self, parsed: dict) -> None:
        ch = parsed["ch"]
        with self.cond:
            self.online_rounds[ch] = 0
            self.channels[ch] = parsed

    # ---------- 总线故障恢复 ----------

    def _recover_bus(self) -> bool:
        """复位 CAN 通道以从 bus-off 恢复(向无应答通道发送后最常见的卡死)。

        返回是否恢复成功。带 1s 冷却:同一轮内多处失败只复位一次,避免反复
        ResetCAN 打断正在进行的收发。调用方须持有 _bus_io_lock(总线事务内)。
        """
        now = time.monotonic()
        if now < self._bus_recover_cool:
            return False
        self._bus_recover_cool = now + 1.0
        with self.cond:
            bus = self.bus
        if bus is None:
            return False
        try:
            ok = bool(bus.restart())
        except Exception:
            ok = False
        if ok:
            with self.cond:
                self._record("bus", "通道已自动复位(bus-off 恢复)")
        return ok

    @staticmethod
    def _poll_send(bus: CanBus, ch: int) -> None:
        bus.send(make_id(CMD_READ, dist=ch, src=MASTER_ADDR), is_remote=True, dlc=8)

    # ---------- 指令处理 ----------

    def dispatch(self, box: _ClientBox, req: dict) -> dict | None:
        cmd = req.get("cmd")
        if cmd == "hello":
            box.name = str(req.get("name") or f"{box.sock.getpeername()}")
            with self.cond:
                self.clients.append(box)
                resp = {"ev": "welcome", **self._status_locked()}
                self._record("clients", f"{box.name} 上线")
                names = [c.name for c in self.clients]
                for c in self.clients:
                    if c is not box:
                        c.send_obj({"ev": "clients", "count": len(names), "names": names})
            return resp
        if cmd == "status":
            with self.cond:
                return {"ev": "status", **self._status_locked()}
        if cmd == "list_vendors":
            av = available_vendors()   # 仅 INI 配置且 dll 已部署的真实 CAN 卡
            return {"ok": True,
                    "vendors": av,
                    "groups": {"dll": [v for v in DLL_VENDORS if v in av]},
                    "default_vendor": default_vendor(),
                    "default_channel": default_channel(),
                    "default_scale": default_scale(),
                    "note": "可选 CAN 卡由 CAN_DLL/Can_Bridge.ini 的 VendorList 决定"
                            "(与 BMUTesterProject 一致), 仅列出 dll 已部署的厂商;"
                            "默认波特率 100 kbps;定标见 ini [CAN] Scale(mv_ma/vx100/vx10/vx1)"}
        if cmd == "open":
            return self.handle_open(req)
        if cmd == "close":
            with self.cond:
                self._close_locked()
            return {"ok": True}
        if cmd in ("set", "voltage", "current", "relay", "unit"):
            return self.handle_control(box, req)
        if cmd == "poll":
            try:
                self.do_poll_round()
            except Exception as ex:
                return {"ok": False, "error": f"轮询失败: {ex}"}
            with self.cond:
                self.last_poll_ts = time.time()
            return {"ok": True, "channels": self._channels_summary()}
        return {"ok": False, "error": f"未知指令: {cmd}"}

    def handle_open(self, req: dict) -> dict:
        # 默认取 INI 配置的真实 CAN 卡(与 BMUTesterProject 一致);不支持 sim 仿真
        vendor = str(req.get("vendor") or default_vendor()).strip().lower()
        channel = str(req.get("channel") or default_channel()).strip()
        try:
            bitrate = int(req.get("bitrate", 100_000))
        except (TypeError, ValueError):
            return {"ok": False, "error": "波特率必须是整数"}
        scale_name = str(req.get("scale") or req.get("scale_name") or default_scale())
        try:
            scale = Scale(scale_name)
        except ValueError as ex:
            scale = Scale()
            # 软兼容:非法定标回退默认
        if vendor == "sim":
            return {"ok": False,
                    "error": "仿真器(sim)已从可选总线移除,请选择真实 CAN 卡: " + ", ".join(available_vendors())}
        if vendor not in DLL_VENDORS:
            return {"ok": False,
                    "error": f"不支持的 CAN 卡: {vendor};可选(INI VendorList): {', '.join(available_vendors())}"}
        if vendor not in available_vendors():
            return {"ok": False,
                    "error": f"{vendor} 的 dll 未部署: 请确认 CAN_DLL/{vendor.upper()}/ 目录完整"}
        with self.cond:
            if self.bus is not None:
                self._close_locked()
            try:
                bus = CanBus(vendor=vendor, channel=channel, bitrate=bitrate, scale=scale)
                info = bus.open()
            except CanBusError as ex:
                return {"ok": False, "error": str(ex)}
            self.bus = bus
            self.bus_info = info
            self.poll_running = True
            self._record("bus", f"打开 {vendor}:{channel} @ {bitrate} ({scale.name})")
            self.broadcast({"ev": "bus", "state": "opened", **info})
            self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self.poll_thread.start()
        return {"ok": True, **info}

    def handle_control(self, box: _ClientBox, req: dict) -> dict:
        """set/voltage/current/relay:单通道 "ch" 或多通道批量 "chs":[...]。

        批量模式逐通道组帧下发(每帧连续 2 次,间隔 5ms,§7),全部完成后只做
        一次回读同步与广播——避免逐通道请求各自触发整轮 24 通道轮询的等待,
        24 通道批量从 ~10s 量级降到亚秒级。响应带 oks/fails 明细。
        """
        cmd = req.get("cmd")
        # quiet:自动下发(如 GUI 的充放电模拟每秒调压)不产生 TX 广播与历史记录,
        # 否则前端日志会被逐条刷屏。仅静默成功日志,失败仍照常上报。
        quiet = bool(req.get("quiet"))
        raw = req.get("chs")
        if raw is None:
            raw = [req.get("ch")]
        elif not isinstance(raw, (list, tuple)):
            raw = [raw]
        try:
            chs = [int(c) for c in raw]
        except (TypeError, ValueError):
            return {"ok": False, "error": "ch(s) 必须是 1..24 的整数"}
        if not chs:
            return {"ok": False, "error": "未指定通道(ch 或 chs)"}
        for ch in chs:
            if not check_channel(ch):
                return {"ok": False, "error": f"ch 超出范围 1..{CH_MAX}: {ch}"}
        with self.cond:
            if self.bus is None:
                return {"ok": False, "error": "CAN 会话未打开,请先 open(vendor=...) 指定总线"}
            scale = self.bus.scale
            bus = self.bus

        # 参数先整体校验,避免发到一半才失败
        try:
            # 电压偏移表(本机校准,mV)。每次取最新,便于 GUI 改后即时生效
            off = load_offsets()
            if cmd == "voltage":
                volts = float(req.get("volts"))
                # 补偿:设定值 + 通道偏移(mV→V)
                def mk(ch):
                    return frame_set_voltage(ch, volts + off.get(ch, 0) / 1000.0, scale)

                def txt(ch):
                    s = f"设电压 ch{ch} = {volts:g} V"
                    if off.get(ch):
                        s += f"(含偏移 {off[ch]:+d}mV)"
                    return s
            elif cmd == "current":
                amps = float(req.get("amps"))
                mk = lambda ch: frame_set_current(ch, amps, scale)
                txt = lambda ch: f"设电流 ch{ch} = {amps:g} A"
            elif cmd == "relay":
                on = bool(req.get("on"))
                mk = lambda ch: frame_relay(ch, on)
                txt = lambda ch: f"{'闭合' if on else '断开'}输出继电器 ch{ch}"
            elif cmd == "unit":
                # 电流档位切换(cmd=2):A⇋mA。vx10000 口径下设 mA 级电流前需先切档。
                mk = lambda ch: frame_unit_switch(ch)
                txt = lambda ch: f"切换电流档位 ch{ch}"
            else:  # set
                volts = req.get("volts")
                amps = req.get("amps")
                if volts is None and amps is None:
                    return {"ok": False, "error": "set 需提供 volts 和/或 amps"}

                def mk(ch):
                    dv = float(volts) if volts is not None else 0
                    if volts is not None:
                        dv += off.get(ch, 0) / 1000.0          # 电压偏移补偿
                    return frame_set_both(ch, dv,
                                          float(amps) if amps is not None else 0, scale)

                def txt(ch):
                    parts = [f"ch{ch}"]
                    if volts is not None:
                        parts.append(f"V={float(volts):g}")
                        if off.get(ch):
                            parts.append(f"(含偏移{off[ch]:+d}mV)")
                    if amps is not None:
                        parts.append(f"A={float(amps):g}")
                    return "设参数 " + " ".join(parts)
        except (TypeError, ValueError) as ex:
            return {"ok": False, "error": f"参数错误: {ex}"}

        def tx(arb_id: int, data: bytes) -> None:
            for _ in range(2):  # §7:连续发送 2 次增强抗丢帧
                bus.send(arb_id, data)
                time.sleep(0.005)

        oks: list[int] = []
        fails: list[dict] = []
        # 批量发送整体持总线 IO 锁,与轮询互斥(单一总线所有者,见 S1)
        with self._bus_io_lock:
            for i, ch in enumerate(chs):
                frame = None
                try:
                    frame = mk(ch)
                    tx(*frame)
                except (CanBusError, ValueError, TypeError) as ex:
                    # 发送失败常见于目标通道无应答 → 控制器重传至 bus-off,此后
                    # 所有发送都失败。先尝试自动复位通道并重发一次本通道;仍失败
                    # 则立即中止剩余通道,不再继续写一条已卡死的总线。
                    if (frame is not None and isinstance(ex, CanBusError)
                            and self._recover_bus()):
                        try:
                            tx(*frame)
                            oks.append(ch)
                            with self.cond:
                                if not quiet:
                                    self._record("tx", txt(ch))
                            continue
                        except (CanBusError, ValueError, TypeError) as ex2:
                            ex = ex2
                    fails.append({"ch": ch, "error": str(ex)})
                    with self.cond:
                        self._record("error", f"ch{ch} 发送失败: {ex}")
                        # 立即判离线:前端据此不再向该通道下发(SOC 模拟等)
                        self.channels[ch] = None
                        self.online_rounds[ch] = 0
                    for ch2 in chs[i + 1:]:
                        fails.append({"ch": ch2, "error": "前序发送失败,总线已中止(请检查通道在线状态或重开总线)"})
                    break
                oks.append(ch)
                with self.cond:
                    if not quiet:
                        self._record("tx", txt(ch))

        # 汇总广播(批量也只广播一次);quiet(如 SOC 自动下发)不产生前端 TX 日志
        if len(chs) == 1:
            summary = txt(chs[0]) if oks else (fails[0]["error"] if fails else "")
        else:
            summary = (f"批量 {cmd} → {len(oks)}/{len(chs)} 通道"
                       + (f"(失败: {[f['ch'] for f in fails]})" if fails else ""))
        if not quiet:
            self.broadcast({"ev": "tx", "from": box.name, "cmd": cmd,
                            "ch": chs[0] if len(chs) == 1 else None,
                            "chs": chs, "text": summary})
        if cmd == "relay" and oks:
            # 批量完成后做一次回读同步让 GUI 快速看到继电器状态。
            # do_poll_round 内部持有 _bus_io_lock,与后台周期轮询互斥串行,
            # 不会双重并发轮询。
            try:
                self.do_poll_round()
            except Exception:
                pass
            self.broadcast({"ev": "channels", "from": "relay", "channels": self._channels_summary()})
        if fails and not oks:
            return {"ok": False, "error": fails[0]["error"], "fails": fails}
        return {"ok": not fails, "text": summary, "chs": oks, "fails": fails,
                "ch": chs[0] if len(chs) == 1 else None}

    # ---------- 服务生命周期 ----------

    def serve_forever(self) -> None:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lsock.bind((DEFAULT_HOST, self.tcp_port))
        except OSError:
            print(f"端口 {self.tcp_port} 已被占用,后端服务应已在运行,本次退出。")
            return
        lsock.listen(8)
        print(f"程控模拟电池后端已启动:{DEFAULT_HOST}:{self.tcp_port}")
        try:
            while not self.stop_requested:
                lsock.settimeout(0.5)
                try:
                    sock, addr = lsock.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._client_loop, args=(sock, addr), daemon=True).start()
        finally:
            lsock.close()
            with self.cond:
                for c in list(self.clients):
                    c.send_now({"ev": "bye"})
                    c.alive = False
                    try:
                        c.sock.close()
                    except OSError:
                        pass
                self.clients.clear()
                self._close_locked()
            print("程控模拟电池后端已退出。")

    def _client_loop(self, sock: socket.socket, addr) -> None:
        box = _ClientBox(sock, name=f"{addr[0]}:{addr[1]}")
        f = sock.makefile("r", encoding="utf-8", newline="\n")
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                rid = req.get("id")
                cmd = req.get("cmd")
                if cmd == "shutdown":
                    self.stop_requested = True
                    ack = {"ok": True}
                    if rid is not None:
                        ack["id"] = rid
                    box.send_now(ack)
                    break
                resp = self.dispatch(box, req)
                if resp is not None:
                    if rid is not None:
                        resp = {**resp, "id": rid}
                    box.send_obj(resp)
                if self.stop_requested:
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
            with self.cond:
                if box in self.clients:
                    self.clients.remove(box)
                    self._record("clients", f"{box.name} 下线")
                names = [c.name for c in self.clients]
                for c in self.clients:
                    c.send_obj({"ev": "clients", "count": len(names), "names": names})


def main() -> None:
    try:
        tcp_port = int(sys.argv[1]) if len(sys.argv) > 1 else server_addr()[1]
    except ValueError:
        tcp_port = DEFAULT_TCP_PORT
    BatteryServer(tcp_port).serve_forever()


if __name__ == "__main__":
    main()