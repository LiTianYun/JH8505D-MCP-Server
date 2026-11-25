#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程控模拟电池 — CAN 总线适配 + 内置模拟电池仿真器(内部测试用)。

两层职责:
1. CanBus:总线驱动封装。三种后端:
   - vendor="sim":内置 24 通道模拟电池仿真器(协议级,纯内存,仅内部测试/单元验证用,不作为可选卡)
   - vendor=gcan/cx/zlg/zlg_eu:厂商 DLL 桥接(移植自 BMUTesterProject src/can/bridge/,
     运行时加载 ECanVci64.dll / ControlCAN.dll / zlgcan.dll,见 battery_vci.py/battery_zlg.py)
   - vendor=canalystii/pcan/kvaser/vector/socketcan...:python-can 后端(需 pip install python-can)
   可选 CAN 卡列表由 CAN_DLL/Can_Bridge.ini 的 VendorList 决定(不含 sim),与 BMU 一致。
2. SimBattery:对《模拟电池CAN协议规范.md》的完整实现模型——每个通道是一台"模拟电池",
   解析下行帧(设电压/设电流/合并/继电器/量纲/自动上报/读参数 RTR)并按 §5 回上行应答;
   带加载/卸载、电压纹波、温升等动态行为,供单元测试与离线联调。

依赖:python-can(仅 python-can 后端需要);sim 与厂商 DLL 桥接均无需第三方库。
"""
from __future__ import annotations

import os
import random
import threading
import time
from collections import deque

from battery_proto import (
    CH_MAX, CH_MIN, CMD_AUTO_OFF, CMD_AUTO_ON, CMD_READ, CMD_RELAY,
    CMD_SEL_ADDR, CMD_SEL_END, CMD_SEL_START, CMD_SET_BOTH, CMD_SET_CURRENT,
    CMD_SET_VOLTAGE, CMD_UNIT_SWITCH, MASTER_ADDR, SEG2_OFFSET, split_id,
    Scale, make_id,
)

SIM_VENDOR = "sim"
HARDWARE_VENDORS = ("canalystii", "pcan", "kvaser", "vector", "socketcan",
                    "gs_usb", "cantact", "neousys", "slcan", "iscan", "ixxat",
                    "usb2can", "systec", "etas", "seeedstudio", "robotell",
                    "serial", "udp_multicast", "virtual")

# 厂商 DLL 桥接(移植自 BMUTesterProject: 运行时加载厂商 CAN DLL, 见 battery_vci/battery_zlg)
#   gcan   —— 广成 ECanVci64.dll(VCI, 无前缀, 设备类型 4)
#   cx     —— 创芯科技 ControlCAN.dll(VCI, 带 VCI_ 前缀, 设备类型 4)
#   zlg    —— 周立功 zlgcan.dll(ZCAN, USBCAN-II, 设备类型 4)
#   zlg_eu —— 周立功 zlgcan.dll(ZCAN, USBCAN-E-U/2E-U, 设备类型 21)
DLL_VENDORS = ("gcan", "cx", "zlg", "zlg_eu")

# sim 仅保留为内部测试钩子,不出现在任何可选列表/默认值中
VALID_VENDORS = (SIM_VENDOR, *DLL_VENDORS, *HARDWARE_VENDORS)

# 量纲位特殊编码:cmd=2 切换量纲用;cmd=3 下行 Data[6]=0 保留
UNIT_A = 0          # 安培级
UNIT_MA = 1         # 毫安级

DLL_BITRATES = (5_000, 10_000, 20_000, 40_000, 50_000, 80_000,
                100_000, 125_000, 200_000, 250_000, 400_000, 500_000,
                666_000, 800_000, 1_000_000)


def available_vendors() -> list[str]:
    """用户可选的 CAN 卡列表(仅 INI VendorList 中已部署 dll 的厂商, 小写)。

    语义与 BMU 主界面 CANname 下拉一致:只列 Can_Bridge.ini 配置且 dll 在的卡;
    不含仿真器, 避免影响真实判断。
    """
    from battery_vci import configured_vendors
    return configured_vendors()


def default_vendor() -> str:
    """默认厂商(小写):INI SelVendor(最近使用)/Vendor, 无则回退第一个可用卡。"""
    from battery_vci import default_vendor as _ini_default
    v = _ini_default()
    if v in DLL_VENDORS:
        return v
    av = available_vendors()
    return av[0] if av else "gcan"


def default_channel() -> str:
    """默认通道:CAN1/CAN2(来自 INI SelChannel 或 CAN1)。"""
    from battery_vci import default_channel as _ini_ch
    return _ini_ch()


def default_scale() -> str:
    """默认定标(来自 Can_Bridge.ini [CAN] Scale;缺失或非法直接报错,不回退)。"""
    from battery_vci import default_scale as _ini_scale
    return _ini_scale()


class CanBusError(Exception):
    pass


def _make_dll_bridge(vendor: str):
    """创建厂商 DLL 桥接实例(gcan/cx -> VciDllBridge; zlg/zlg_eu -> ZlgDllBridge)。"""
    if vendor in ("gcan", "cx"):
        from battery_vci import VciDllBridge
        vname = "GCAN" if vendor == "gcan" else "CX"
        return VciDllBridge(vname)
    from battery_zlg import ZlgDllBridge
    return ZlgDllBridge("ZLG" if vendor == "zlg" else "ZLG_EU")


class _DllAdapter:
    """把 gcan/cx/zlg/zlg_eu 桥接统一成 CanBus 的内部收发接口。"""

    def __init__(self, vendor: str, channel: str, bitrate: int):
        self.vendor = vendor
        self.channel = channel          # "CAN1"/"CAN2"(BMU 工程通道命名)
        self.bitrate = bitrate
        self.bridge = _make_dll_bridge(vendor)
        if not self.bridge.dll_ready:
            raise CanBusError(self.bridge.last_hint)
        self._lock = threading.Lock()

    def open(self) -> dict:
        ret, hint = self.bridge.open_device(self.channel, self.bitrate)
        if ret != 0:
            raise CanBusError(hint or f"{self.vendor} 打开失败(ret={ret})")
        return {
            "vendor": self.vendor,
            "channel": self.channel,
            "bitrate": self.bitrate,
            "bridge": self.bridge.vendor,
            "dll": self.bridge.dll_name,
        }

    def close(self) -> None:
        try:
            self.bridge.close_device()
        except Exception:
            pass

    def send(self, arb_id: int, data: bytes | None, is_remote: bool, dlc: int,
             timeout: float = 0.5) -> None:
        with self._lock:
            ret, n = self.bridge.write_frame(arb_id, data or b"", is_ext=True,
                                             is_remote=is_remote, dlc=dlc)
        if ret < 0:
            raise CanBusError(f"{self.vendor} 发送失败(ret={ret})")

    def restart(self) -> bool:
        """复位/重启通道以从 bus-off 恢复。返回是否成功。

        优先 ResetCAN→ClearBuffer→InitCAN→StartCAN(保持原配置);厂商 dll 未导出
        复位函数时退化为"关闭设备→按原参数重开"。
        """
        with self._lock:
            try:
                if self.bridge.restart_channel() == 0:
                    return True
            except Exception:
                pass
            try:
                self.bridge.close_device()
            except Exception:
                pass
            try:
                ret, _hint = self.bridge.open_device(self.channel, self.bitrate)
                return ret == 0
            except Exception:
                return False

    def recv(self, timeout: float | None = None) -> dict | None:
        ms = int((timeout if timeout is not None else 0.05) * 1000)
        with self._lock:
            ret, aid, data, is_ext = self.bridge.read_frame(timeout_ms=ms)
        if ret == -2:                      # 超时:无帧,正常返回 None
            return None
        if ret < 0:                        # 硬件/驱动错误(bus-off 等) → 抛,让上层感知
            raise CanBusError(f"{self.vendor} 接收失败(ret={ret})")
        return {"arb_id": aid, "data": data, "is_remote": False, "dlc": len(data)}


class CanBus:
    """CAN 总线会话封装。open() 后持有总线;收发统一带锁串行化。

    三种后端:
      - vendor="sim"      内置 24 通道模拟电池(协议级仿真, 零硬件, 离线演示)
      - vendor=gcan/cx/zlg/zlg_eu   厂商 DLL 桥接(移植 BMUTesterProject, 运行时加载
                             ECanVci64.dll / ControlCAN.dll / zlgcan.dll, 默认 100k)
      - vendor=python-can 后端      真实 CAN 卡(需 pip install python-can)
    """

    def __init__(self, vendor: str = SIM_VENDOR, channel: str = "sim0",
                 bitrate: int = 100_000, scale: Scale | None = None,
                 poll_timeout: float = 0.05):
        if vendor not in VALID_VENDORS:
            raise CanBusError(
                f"未知 vendor: {vendor};可用 {SIM_VENDOR}(内置模拟), "
                f"DLL 桥接 {list(DLL_VENDORS)}(BMUTesterProject 同款: gcan/cx/zlg/zlg_eu), "
                f"或 python-can 后端: " + ", ".join(HARDWARE_VENDORS))
        # DLL 桥接 channel 用 CAN1/CAN2(BMU 工程命名);sim 用 sim0;python-can 用卡通道号
        if vendor in DLL_VENDORS:
            channel = channel or "CAN1"
            if channel.upper() not in ("CAN1", "CAN2"):
                channel = "CAN1"
            channel = channel.upper()
        elif vendor == SIM_VENDOR:
            channel = channel or "sim0"
        self.vendor = vendor
        self.channel = channel
        self.bitrate = bitrate
        self.scale = scale or Scale()
        self.poll_timeout = poll_timeout
        self.bus = None          # python-can Bus(仅真实硬件)
        self.dll: _DllAdapter | None = None   # 厂商 DLL 桥接
        self.sim: SimBattery | None = None
        self._rx_lock = threading.Lock()
        self._opened = False

    # ---------- 会话 ----------

    @property
    def is_sim(self) -> bool:
        return self.vendor == SIM_VENDOR

    def open(self) -> dict:
        """打开会话;返回描述信息。失败抛 CanBusError。"""
        if self._opened:
            return self.info()
        if self.is_sim:
            self.sim = SimBattery(scale=self.scale)
            self.sim.start()
            self._opened = True
            return self.info()
        if self.vendor in DLL_VENDORS:
            self.dll = _DllAdapter(self.vendor, self.channel, self.bitrate)
            extra = self.dll.open()
            self._opened = True
            return self.info(extra)
        try:
            import can  # 延迟导入:sim/dll 模式无需 python-can
        except ImportError:
            raise CanBusError(
                "缺少 python-can:真实 CAN 卡需要 `pip install python-can`,"
                "或使用内置模拟电池(vendor=sim)离线联调,"
                "或使用厂商 DLL 桥接(gcan/cx/zlg)。")
        try:
            bus = can.Bus(interface=self.vendor, channel=self.channel,
                          bitrate=self.bitrate, receive_own_messages=False)
        except Exception as ex:
            raise CanBusError(f"无法打开 CAN 通道 [{self.vendor}:{self.channel}]: {ex}")
        self.bus = bus
        self._opened = True
        return self.info()

    def close(self) -> None:
        if self.sim is not None:
            self.sim.stop()
            self.sim = None
        if self.dll is not None:
            self.dll.close()
            self.dll = None
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
            self.bus = None
        self._opened = False

    def restart(self) -> bool:
        """尝试复位通道以从 bus-off 等故障恢复(保持 vendor/channel/bitrate)。

        厂商 DLL 走 ResetCAN/ClearBuffer 或重开设备;python-can 后端尽力调用
        Bus.reset();内置仿真器无总线故障,直接返回 True。
        """
        if not self._opened:
            return False
        if self.sim is not None:
            return True
        if self.dll is not None:
            return self.dll.restart()
        fn = getattr(self.bus, "reset", None)
        if callable(fn):
            try:
                fn()
                return True
            except Exception:
                return False
        return False

    def info(self, extra: dict | None = None) -> dict:
        return {
            "vendor": self.vendor,
            "channel": self.channel,
            "bitrate": self.bitrate,
            "scale": self.scale.name,
            "sim": self.is_sim,
            **(extra or {}),
        }

    # ---------- 收发 ----------

    def send(self, arb_id: int, data: bytes | None = None,
             is_remote: bool = False, dlc: int = 8, timeout: float = 0.5) -> None:
        if not self._opened:
            raise CanBusError("CAN 会话未打开")
        if self.is_sim:
            self.sim.inject(arb_id, data or b"", is_remote, dlc)
            return
        if self.dll is not None:
            self.dll.send(arb_id, data, is_remote, dlc, timeout)
            return
        import can
        msg = can.Message(arbitration_id=arb_id & 0x1FFFFFFF,
                          is_extended_id=True,
                          data=data or b"",
                          is_remote_frame=is_remote,
                          dlc=(0 if is_remote else len(data or b"")))
        with self._rx_lock:  # 与 recv 分离:python-can 发送线程安全,锁主要防并发 send
            self.bus.send(msg, timeout=timeout)

    def recv(self, timeout: float | None = None) -> dict | None:
        """收一帧。返回 {arb_id, data, is_remote, dlc} 或 None。"""
        if not self._opened:
            return None
        if self.is_sim:
            return self.sim.poll(timeout if timeout is not None else self.poll_timeout)
        if self.dll is not None:
            return self.dll.recv(timeout)
        import can
        msg = self.bus.recv(timeout=timeout if timeout is not None else self.poll_timeout)
        if msg is None:
            return None
        return {"arb_id": msg.arbitration_id, "data": bytes(msg.data),
                "is_remote": msg.is_remote_frame, "dlc": msg.dlc}


# ---------------------------------------------------------------------------
# 模拟电池:24 通道协议级仿真
# ---------------------------------------------------------------------------

class _Cell:
    """单个通道(一台模拟电池)的动态状态。"""

    def __init__(self, ch: int, scale: Scale):
        self.ch = ch
        self.scale = scale
        # 设定值(raw,下行口径 = 设备收到的原始字节整数)
        self.set_v_raw = 0
        self.set_i_raw = 0
        # 目标工程值(V/A):由下行 raw 按 _DOWN 系数换算,是设备物理输出目标
        self.target_v = 0.0
        self.target_i = 0.0
        # 输出工程值(V/A,tick 叠加纹波后 = 设备实测物理量)
        self.out_v = 0.0
        self.out_i = 0.0
        self.relay = False
        self.unit_ma = False          # 电流量纲:False=安培级
        self.temp = 25                # ℃
        self.auto_report = False
        # 动态仿真
        self._phase = random.uniform(0, 6.28)
        self._last_t = time.monotonic()
        self._baseline_temp = 25.0
        # 最近一次读参数请求的原始应答帧(供上行注入)
        self.last_ack_raw: dict | None = None

    # ---- 下行 raw ↔ 工程值(镜像实机定标方向) ----
    def apply_down_v(self, raw: int) -> None:
        """收到下行电压 raw → 记录 set raw 与目标工程值(V)。"""
        self.set_v_raw = raw
        self.target_v = raw / self.scale._DOWN_V[self.scale.name]

    def apply_down_i(self, raw: int) -> None:
        """收到下行电流 raw → 记录 set raw 与目标工程值(A)。"""
        self.set_i_raw = raw
        self.target_i = raw / self.scale._DOWN_I[self.scale.name]

    def ack_v_raw(self) -> int:
        """上行应答电压 raw = 工程值 × 上行系数(实机:mV×10 → _UP_V)。"""
        return int(round(self.out_v * self.scale._UP_V[self.scale.name]))

    def ack_i_raw(self) -> int:
        """上行应答电流 raw = 工程值 × 上行系数。unit_ma 档:设备以 mA 上报时
        _UP_I 已含 mA 系数(实机 mA×10),此处直接换算。"""
        return int(round(self.out_i * self.scale._UP_I[self.scale.name]))

    def tick(self) -> None:
        """周期演化:纹波 + 温升,刷新输出工程值。

        物理规则:继电器闭合才"输出"设定值(带小纹波);断开时输出恒为 0,
        但设定值保留——重新闭合即恢复输出。
        """
        now = time.monotonic()
        dt = min(now - self._last_t, 2.0)
        self._last_t = now
        self._phase += dt * 1.3
        if self.relay:
            # 电压/电流小纹波(±0.15% 满量程量级),制造"活数据"效果
            if self.target_v:
                self.out_v = max(0.0, self.target_v * (1.0 + 0.0015 * (random.random() - 0.5)))
            if self.target_i:
                self.out_i = max(0.0, self.target_i * (1.0 + 0.0018 * (random.random() - 0.5)))
            # 温升:近似正比于输出功率(V×A),上限 60℃,带弛豫
            p = self.target_v * self.target_i  # V*A ≈ W
            target = 25.0 + min(p * 0.35, 35.0)
        else:
            self.out_v = self.out_i = 0.0
            target = 25.0
        self._baseline_temp += (target - self._baseline_temp) * min(dt * 0.05, 0.2)
        self.temp = max(20, int(self._baseline_temp + random.uniform(-0.6, 0.6)))


class SimBattery:
    """24 通道模拟电池设备(挂在 CanBus 的 sim 总线上)。

    主节点(后端)向通道 ch 发任意下行帧 -> 本节点解析并按协议回上行帧;
    同时后台自动上报(通道开启自动上报时,每 ~0.5s 主动发一帧参数应答)。
    """

    def __init__(self, scale: Scale | None = None):
        self.scale = scale or Scale()
        self.channels = {ch: _Cell(ch, self.scale) for ch in range(CH_MIN, CH_MAX + 1)}
        self.addr_offset = 0            # 第二段总线偏移,保留
        self._inbox: deque = deque(maxlen=512)   # 有界队列,防无人 poll 时无限增长
        self._cv = threading.Condition()
        self._run = False
        self._thread: threading.Thread | None = None
        self.frames_tx = 0
        self.frames_rx = 0
        self._last_report = 0.0                    # 单调时钟:上次自动上报时刻

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._run:
            return
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ---------- 主节点 -> 仿真器 ----------

    def inject(self, arb_id: int, data: bytes, is_remote: bool, dlc: int) -> None:
        """CanBus.send 走这里:仿真器按 §2/§3 决定是否回上行帧。"""
        self.frames_rx += 1
        self._handle(arb_id, data, is_remote)

    def _handle(self, arb_id: int, data: bytes, is_remote: bool) -> None:
        info = split_id(arb_id)
        cmd = info["cmd"]
        dist = info["dist"]
        # 第二段总线偏移(61..84)映射回本地 1..24
        local = dist - SEG2_OFFSET if SEG2_OFFSET < dist <= SEG2_OFFSET + CH_MAX else dist
        # 多机编址命令无通道性(作用于全部)
        if cmd in (CMD_SEL_START, CMD_SEL_END, CMD_SEL_ADDR):
            return
        cell = self.channels.get(local)
        if cell is None:
            return
        # 只应答发给"本机"(dist=通道号即本机地址;或 src=主 的下行)的上行
        is_down = (info["src"] == MASTER_ADDR)
        if not is_down:
            return

        if is_remote:
            # §4.5:RTR 读参数请求 -> 回同 cmd 8B 数据应答(§5.1/§5.2 旧式)
            if cmd in (CMD_READ, CMD_SET_BOTH) and info["dist"] == local:
                ack = self._ack_frame(cell, cmd=cmd)
                cell.last_ack_raw = ack
                self._emit(ack)
            return

        if cmd == CMD_SET_VOLTAGE and len(data) >= 3:
            raw = int.from_bytes(data[0:3], "little") & 0xFFFFFF
            cell.apply_down_v(raw)
            cell.out_v = cell.target_v
        elif cmd == CMD_SET_CURRENT and len(data) >= 3:
            raw = int.from_bytes(data[0:3], "little") & 0xFFFFFF
            cell.apply_down_i(raw)
            cell.out_i = cell.target_i
        elif cmd == CMD_SET_BOTH and len(data) >= 7:
            # 电压位宽随定标(vx10000 24 位/其余 16 位),与下行组帧一致(H14)
            nb_v = cell.scale._v_layout()[0]
            v = int.from_bytes(data[0:nb_v], "little")
            i = int.from_bytes(data[3:6], "little")
            cell.apply_down_v(v)
            cell.apply_down_i(i)
            cell.out_v, cell.out_i = cell.target_v, cell.target_i
            # 注:cmd=3 下行设值不回帧;设备旧版以 cmd=3 数据帧应答读请求(见 RTR 分支)
        elif cmd == CMD_RELAY and len(data) >= 1:
            cell.relay = bool(data[0] & 1)
            # 继电器动作即时反映到输出;tick 会持续维持
            if cell.relay:
                cell.out_v, cell.out_i = cell.target_v, cell.target_i
            else:
                cell.out_v = cell.out_i = 0.0
        elif cmd == CMD_UNIT_SWITCH:
            cell.unit_ma = not cell.unit_ma
        elif cmd == CMD_AUTO_ON:
            cell.auto_report = True
        elif cmd == CMD_AUTO_OFF:
            cell.auto_report = False

    def _ack_frame(self, cell: _Cell, cmd: int = CMD_READ) -> dict:
        """按设备物理输出组上行应答帧(实机口径:工程值 × 上行系数)。

        电压 raw 位宽随定标(vx10000 为 24 位无符号,其余 16 位);量纲位如实反映。
        cmd 默认 CMD_READ(12);RTR 请求若为 cmd=3 则回显 cmd=3(§5.2 旧式兼容)。
        """
        raw_v = cell.ack_v_raw()
        raw_i = cell.ack_i_raw()
        status = (1 if cell.unit_ma else 0) | (2 if cell.relay else 0)
        # 电压位宽:Scale._v_layout() = (字节数, 有符号);仿真按无符号截取
        nb_v = cell.scale._v_layout()[0]
        mask_v = (1 << (nb_v * 8)) - 1
        data = (int(raw_v & mask_v).to_bytes(nb_v, "little")
                + b"\x00" * (3 - nb_v)
                + int(raw_i & 0xFFFFFF).to_bytes(3, "little")
                + bytes([status, max(20, min(cell.temp, 127)) & 0xFF]))
        return {"arb_id": make_id(cmd, dist=MASTER_ADDR, src=cell.ch),
                "data": data, "is_remote": False, "dlc": 8}

    def _emit(self, frame: dict) -> None:
        with self._cv:
            self._inbox.append(frame)   # deque(maxlen) 自动丢最旧,防无限增长
            self.frames_tx += 1
            self._cv.notify_all()

    # ---------- 主节点轮询读取 ----------

    def poll(self, timeout: float = 0.05) -> dict | None:
        """被 CanBus.recv 调用;返回仿真器产生的上行帧。"""
        with self._cv:
            if not self._inbox:
                if not self._cv.wait(timeout):
                    return None
            if not self._inbox:
                return None
            return self._inbox.popleft()

    # ---------- 后台演化 ----------

    def _loop(self) -> None:
        while self._run:
            now = time.monotonic()
            for cell in self.channels.values():
                cell.tick()
            # 自动上报:开着的通道每 0.5s 主动上报一次(单调时钟节拍)
            with self._cv:
                if now - self._last_report >= 0.5:
                    self._last_report = now
                    for ch in self.channels.values():
                        if ch.auto_report:
                            self._inbox.append(self._ack_frame(ch))
                            self.frames_tx += 1
                self._cv.notify_all()
            time.sleep(0.05)


# ---------------- 配置文件读写(共享) ----------------

if __name__ == "__main__":
    bus = CanBus(vendor=SIM_VENDOR)
    print("open:", bus.open())
    # 发一条设电压 RTR 读参数 -> 应回一帧
    bus.send(make_id(CMD_READ, dist=1, src=MASTER_ADDR), is_remote=True, dlc=8)
    f = bus.recv(timeout=1.0)
    print("read-back:", f)
    bus.send(*__import__("battery_proto").frame_set_voltage(1, 3.2))
    bus.send(*__import__("battery_proto").frame_set_both(2, 12.5, 2.0))
    bus.send(make_id(CMD_RELAY, dist=2, src=MASTER_ADDR), data=b"\x01")
    time.sleep(0.15)
    bus.send(make_id(CMD_READ, dist=2, src=MASTER_ADDR), is_remote=True, dlc=8)
    f = bus.recv(timeout=1.0)
    print("ch2 after set:", f)
    bus.close()
    print("sim ok")
