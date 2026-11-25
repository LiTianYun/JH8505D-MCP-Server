#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程控模拟电池 — CAN 协议编解码层(硬件无关)。

严格按《模拟电池CAN协议规范.md》实现:
- 链路:100 kbps,扩展帧(29-bit ID),双向。
- 帧 ID:  bit28..17 cmd(7) | bit16..14 page(3) | bit13..7 sourceAddr(7) | bit6..0 distAddr(7)
- 下行(主→从):dist=通道号 1..24, src=99(主), page=0
- 上行(从→主):dist=99,       src=通道号, page=0, cmd 与请求相同
- 数值:整型小端;电压 mV、电流 mA(文档 §6 自洽口径);另有 vx1 备选定标供实机校准。

本层只做字节 <-> 语义换算,不依赖任何 CAN 硬件/库。
"""
from __future__ import annotations

import math
import struct

# ---------------- 常量 ----------------

MASTER_ADDR = 99          # 主节点地址
CH_MIN, CH_MAX = 1, 24    # 单机通道 1..24
SEG2_OFFSET = 60          # 第二段总线从节点地址偏移(61..84),本层保留常量备用

V_MAX = 32.0              # 输出电压上限 V(约 0~32.0)
A_MAX = 10.0              # 输出电流上限 A(约 0~10.0)

# 命令字(§3)
CMD_SET_VOLTAGE = 0       # 设电压(3B)
CMD_SET_CURRENT = 1       # 设电流(3B)
CMD_UNIT_SWITCH = 2       # 电流单位切换(安培级 <-> 毫安级)
CMD_SET_BOTH = 3          # 设电压+电流(7B); 上行旧式参数应答(8B)
CMD_AUTO_ON = 4           # 自动上报使能
CMD_AUTO_OFF = 5          # 自动上报禁止
CMD_SEL_START = 6         # 选地址起始(多机编址)
CMD_SEL_END = 7           # 选地址结束(多机编址)
CMD_SEL_ADDR = 8          # 选地址(多机编址)
CMD_RELAY = 9             # 输出继电器(1B)
CMD_READ = 12             # 读参数(RTR 请求 → 8B 数据应答)

CMD_NAMES = {
    CMD_SET_VOLTAGE: "设电压",
    CMD_SET_CURRENT: "设电流",
    CMD_UNIT_SWITCH: "电流单位切换",
    CMD_SET_BOTH: "设参数/读应答",
    CMD_AUTO_ON: "自动上报使能",
    CMD_AUTO_OFF: "自动上报禁止",
    CMD_SEL_START: "选地址起始",
    CMD_SEL_END: "选地址结束",
    CMD_SEL_ADDR: "选地址",
    CMD_RELAY: "输出继电器",
    CMD_READ: "读参数",
}

# 量纲:raw 整数 -> 浮点工程值
SCALE_MV_MA = "mv_ma"      # 设备按 mV 解析:raw = 电压×1000(3.200V -> 3200),电流×1000
SCALE_VX100 = "vx100"      # 设备按 0.01V 解析:raw = 电压×100(3.20V -> 320),电流×100
SCALE_VX10 = "vx10"        # 设备按 0.1V 解析:raw = 电压×10 (3.20V -> 32),电流×10
SCALE_VX1 = "vx1"          # 设备按 1V 解析:raw = 电压×1,电流×1
SCALE_VX10000 = "vx10000"  # 实机(开发版固件)口径,逐条实测校准:
                           #   上行电压: raw = mV×10(÷10^4),24 位无符号 [31000↔3.100V, 420↔0.042V, 4V/5V 不再读负]
                           #   上行电流: raw = mA×10(÷10^4),与 bit0 档位位无关 [55↔5.5mA, 67↔6.7mA 空载]
                           #   下行电压: raw = mV(×1000)       [发 42 → 输出 0.042V]
                           #   下行电流: raw = mA(×1000)       [发 3000 → 3.000A,分辨率 1mA]
SCALES = (SCALE_MV_MA, SCALE_VX100, SCALE_VX10, SCALE_VX1, SCALE_VX10000)


# ---------------- 帧 ID ----------------

def make_id(cmd: int, dist: int, src: int = MASTER_ADDR, page: int = 0) -> int:
    """按 §2.2 公式组 29 位扩展帧 ID。"""
    return (
        (int(dist) & 0x7F)
        | ((int(src) & 0x7F) << 7)
        | ((int(page) & 0x07) << 14)
        | ((int(cmd) & 0x7F) << 17)
    )


def split_id(arb_id: int) -> dict:
    """解析扩展帧 ID 为 {cmd, page, src, dist}。"""
    return {
        "cmd": (arb_id >> 17) & 0x7F,
        "page": (arb_id >> 14) & 0x07,
        "src": (arb_id >> 7) & 0x7F,
        "dist": arb_id & 0x7F,
    }


def check_channel(ch: int) -> bool:
    return CH_MIN <= int(ch) <= CH_MAX


# ---------------- 定标 ----------------

class Scale:
    """raw 整数 <-> 工程浮点 的双向换算。

    mv_ma : 设备按 mV   解析 -> 电压 raw = V×1000(32.000V -> 32000), 电流 raw = mA(10.000A -> 10000)
    vx100 : 设备按 0.01V 解析 -> 电压 raw = V×100 (3.20V -> 320),    电流 raw = A×100
    vx10  : 设备按 0.1V  解析 -> 电压 raw = V×10  (3.20V -> 32),     电流 raw = A×10
    vx1   : 设备按 1V    解析 -> 电压 raw = V×1,                     电流 raw = A×1
    vx10000 : 实机口径,上下行不对称
              下行 raw = mV(电压×1000) / mA(电流×1000)
              上行 raw = mV×10(电压÷10^4) / mA×10(电流÷10^4)

    选哪个只取决于设备把 raw 当什么单位解析,与固件版本无关,不能用"偏差×10 就换某一档"推断。
    换定标后必须单通道闭环:设 3.10V -> 读回 3.10V 才算对上。
    """

    def __init__(self, name: str = SCALE_MV_MA):
        if name not in SCALES:
            raise ValueError(f"未知定标: {name},可选 {SCALES}")
        self.name = name

    # 下行:raw = 工程值 × factor;上行:工程值 = raw / factor。
    # vx10000(实机口径)不对称:下行电压/电流都按 mV/mA(×1000),上行电压/电流都 ÷10^4。
    _DOWN_V = {"mv_ma": 1000.0, "vx100": 100.0, "vx10": 10.0, "vx1": 1.0,
               "vx10000": 1000.0}
    _DOWN_I = {"mv_ma": 1000.0, "vx100": 100.0, "vx10": 10.0, "vx1": 1.0,
               "vx10000": 1000.0}
    _UP_V = {"mv_ma": 1000.0, "vx100": 100.0, "vx10": 10.0, "vx1": 1.0,
             "vx10000": 10000.0}
    _UP_I = {"mv_ma": 1000.0, "vx100": 100.0, "vx10": 10.0, "vx1": 1.0,
             "vx10000": 10000.0}

    # 上行电压 raw 的位宽与符号:(字节数, 是否有符号)。
    # 实机(vx10000)实测为 24 位无符号(≥3.2768V 超 16 位有符号会被误读成负数,
    # 如实测 4V→-2.554、5V→-1.554);其余定标按文档 §5.1 的 16 位有符号。
    _V_PARSE = {"vx10000": (3, False)}

    def _v_layout(self) -> tuple[int, bool]:
        return self._V_PARSE.get(self.name, (2, True))

    # ---- 下行:工程值 -> raw ----
    def voltage_raw(self, volts: float) -> int:
        # NaN/Inf(非法输入) -> 0,与越界截断行为一致(H9);负值统一截 0
        if not math.isfinite(volts):
            return 0
        volts = max(0.0, min(float(volts), V_MAX))
        return int(round(volts * self._DOWN_V[self.name]))

    def current_raw(self, amps: float) -> int:
        if not math.isfinite(amps):
            return 0
        amps = max(0.0, min(float(amps), A_MAX))
        return int(round(amps * self._DOWN_I[self.name]))

    # ---- 上行:raw -> 工程值 ----
    def voltage_of(self, raw: int) -> float:
        return raw / self._UP_V[self.name]

    def current_of(self, raw: int, unit_ma: bool = False) -> float:
        """电流 raw -> A。unit_ma=True 表示状态位 bit0 标记的毫安级。

        - mv_ma / vx10000:raw 本为 mA 级(mA / mA×10),直接除系数即得 A,
          bit0 只是档位标志,不参与换算(实机:bit0=0 时 raw 仍是 mA×10,如 55=5.5mA);
        - vx100 / vx10 / vx1:raw 以 A 级记,unit_ma=True 时 raw 实为 mA,需再 /1000。
        """
        if self.name in (SCALE_MV_MA, SCALE_VX10000):
            return raw / self._UP_I[self.name]
        val = raw / self._UP_I[self.name]
        if unit_ma:
            val = val / 1000.0
        return val


DEFAULT_SCALE = Scale()


# ---------------- 下行控制帧(§4) ----------------

def frame_set_voltage(ch: int, volts: float, scale: Scale | None = None) -> tuple[int, bytes]:
    """cmd=0,3 字节:电压 raw 小端,低位在前。返回 (arb_id, data)。"""
    raw = (scale or DEFAULT_SCALE).voltage_raw(volts)
    return make_id(CMD_SET_VOLTAGE, ch), raw.to_bytes(3, "little")


def frame_set_current(ch: int, amps: float, scale: Scale | None = None) -> tuple[int, bytes]:
    """cmd=1,3 字节:电流 raw 小端。"""
    raw = (scale or DEFAULT_SCALE).current_raw(amps)
    return make_id(CMD_SET_CURRENT, ch), raw.to_bytes(3, "little")


def frame_set_both(ch: int, volts: float, amps: float, scale: Scale | None = None) -> tuple[int, bytes]:
    """cmd=3,7 字节:Data[0..2] 电压 raw, Data[3..5] 电流 raw, Data[6]=0。"""
    sc = scale or DEFAULT_SCALE
    data = (sc.voltage_raw(volts).to_bytes(3, "little")
            + sc.current_raw(amps).to_bytes(3, "little")
            + b"\x00")
    return make_id(CMD_SET_BOTH, ch), data


def frame_relay(ch: int, closed: bool) -> tuple[int, bytes]:
    """cmd=9,1 字节:1=闭合(输出投入),0=断开。"""
    return make_id(CMD_RELAY, ch), bytes([1 if closed else 0])


def frame_unit_switch(ch: int) -> tuple[int, bytes]:
    """cmd=2:电流单位切换(安培级 <-> 毫安级),无数据。"""
    return make_id(CMD_UNIT_SWITCH, ch), b""


def frame_auto_report(ch: int, enable: bool) -> tuple[int, bytes]:
    """cmd=4/5:自动上报使能/禁止,无数据。"""
    cmd = CMD_AUTO_ON if enable else CMD_AUTO_OFF
    return make_id(cmd, ch), b""


def frame_read_request(ch: int, cmd: int = CMD_READ, dlc: int = 8) -> tuple[int, bytes | None, int]:
    """cmd=12(或 3)读参数:RTR 远程帧,无数据。返回 (arb_id, None, dlc)。"""
    return make_id(cmd, ch), None, dlc


# ---------------- 上行应答解析(§5) ----------------

def _signed(raw: int, bits: int) -> int:
    """补码转有符号:raw 为 bits 位无符号值。"""
    sign = 1 << (bits - 1)
    return raw - (1 << bits) if raw & sign else raw


def parse_param_frame(arb_id: int, data: bytes, scale: Scale | None = None) -> dict | None:
    """解析上行参数应答(8 字节,cmd=12 或 cmd=3)。

    布局(§5.1/5.2):
        Data[0..1] = 电压 raw(小端 16 bit)
        Data[3..5] = 电流 raw(小端 24 bit)
        Data[6]    = 状态: bit0 电流量纲(0=A 级 / 1=mA 级), bit1 输出继电器(1=闭合)
        Data[7]    = 温度(℃ 整数; cmd3 旧式应答可能无,置 None)

    负值支持:电压/电流的 raw 若为补码负值(如 -3.1V、-1.2A 放电/反向),
    自动符号扩展为有符号后再换算;返回的 raw_v/raw_i 也带符号。
    返回 None 表示不是有效参数应答。
    """
    info = split_id(arb_id)
    if info["cmd"] not in (CMD_READ, CMD_SET_BOTH) or info["dist"] != MASTER_ADDR:
        return None
    if len(data) < 7:
        return None
    sc = scale or DEFAULT_SCALE
    nb_v, signed_v = sc._v_layout()
    raw_v = int.from_bytes(data[0:nb_v], "little")
    if signed_v:
        raw_v = _signed(raw_v, nb_v * 8)
    raw_i = _signed(int.from_bytes(data[3:6], "little"), 24)
    status = data[6]
    unit_ma = bool(status & 0x01)
    relay = bool((status >> 1) & 0x01)
    temp = data[7] if len(data) >= 8 else None
    volts = sc.voltage_of(raw_v)
    amps = sc.current_of(raw_i, unit_ma)
    # 物理范围钳制(审查 H4):24 位无符号满量程或噪声帧会算出荒谬值
    # (如 vx10000 下 0xFFFFFF → +1677V)。超物理上限视为坏帧,不产出伪读数。
    v_max_ok = V_MAX * 1.5          # 允许少量过冲(放电反向用负值由定标决定)
    a_max_ok = A_MAX * 1.5
    if not (-v_max_ok <= volts <= v_max_ok) or not (-a_max_ok <= amps <= a_max_ok):
        return None
    if temp is not None and not (-40 <= temp <= 150):
        temp = None                 # 温漂噪声不计
    return {
        "ch": info["src"],
        "volts": volts,
        "amps": amps,
        "raw_v": raw_v,
        "raw_i": raw_i,
        "relay": relay,
        "unit_ma": unit_ma,
        "temp": temp,
    }


def build_param_data(volts: float, amps: float, relay: bool, temp: int,
                     unit_ma: bool = False, scale: Scale | None = None) -> bytes:
    """构造设备侧参数应答 8 字节(模拟器与测试用)。按上行(_UP)口径组帧。"""
    sc = scale or DEFAULT_SCALE
    nb_v, _ = sc._v_layout()
    raw_v = int(round(volts * sc._UP_V[sc.name])) & ((1 << (nb_v * 8)) - 1)
    fi = sc._UP_I[sc.name]
    if unit_ma and sc.name not in (SCALE_MV_MA, SCALE_VX10000):
        fi *= 1000.0          # 毫安级:raw 以 mA 记(mv_ma/vx10000 本为 mA 口径,不再乘)
    raw_i = int(round(amps * fi)) & 0xFFFFFF
    status = (1 if unit_ma else 0) | (2 if relay else 0)
    return (raw_v.to_bytes(nb_v, "little")
            + b"\x00" * (3 - nb_v)       # 电压占满 Data[0..2],不足补零
            + raw_i.to_bytes(3, "little")
            + bytes([status, int(temp) & 0xFF]))


# ---------------- 展示辅助 ----------------

def fmt_id(arb_id: int) -> str:
    info = split_id(arb_id)
    return (f"ID={arb_id:08X} cmd={info['cmd']}({CMD_NAMES.get(info['cmd'], '?')}) "
            f"src={info['src']} dist={info['dist']} page={info['page']}")


def fmt_bitrate(bitrate: int) -> str:
    """波特率数值 -> 人类可读字符串:100000 -> '100 kHz',2500000 -> '2.5 MHz'。"""
    b = int(bitrate)
    if b >= 1_000_000 and b % 1000 == 0:
        m = b / 1_000_000.0
        return f"{m:g} MHz"
    if b >= 1000 and b % 1000 == 0:
        return f"{b // 1000} kHz"
    return str(b)


def fmt_bytes(data: bytes | None) -> str:
    return data.hex(" ").upper() if data else "-"


if __name__ == "__main__":
    # 自检:与文档示例逐条对照
    sc = Scale(SCALE_MV_MA)
    assert sc.voltage_raw(32.0) == 32000 and sc.voltage_raw(3.2) == 3200
    assert sc.current_raw(10.0) == 10000 and sc.current_raw(1.0) == 1000

    aid, data = frame_set_voltage(1, 32.0)
    assert data == bytes.fromhex("00 7D 00"), data.hex()
    aid, data = frame_set_voltage(1, 3.2)
    assert data == bytes.fromhex("80 0C 00"), data.hex()
    aid, data = frame_set_current(2, 10.0)
    assert data == bytes.fromhex("10 27 00"), data.hex()
    aid, data = frame_set_current(2, 1.0)
    assert data == bytes.fromhex("E8 03 00"), data.hex()

    # ID 往返:下行 cmd=12 RTR 到通道 3
    down = make_id(CMD_READ, dist=3, src=MASTER_ADDR)
    assert split_id(down) == {"cmd": 12, "page": 0, "src": 99, "dist": 3}
    # 上行应答:src=3 -> dist=99
    up = make_id(CMD_READ, dist=MASTER_ADDR, src=3)
    assert split_id(up) == {"cmd": 12, "page": 0, "src": 3, "dist": 99}

    # 应答解析:32.0 V / 10.0 A / 闭合 / 25℃, A 级
    pkt = build_param_data(32.0, 10.0, relay=True, temp=25)
    parsed = parse_param_frame(up, pkt)
    assert parsed["ch"] == 3 and abs(parsed["volts"] - 32.0) < 1e-9
    assert abs(parsed["amps"] - 10.0) < 1e-9 and parsed["relay"] and parsed["temp"] == 25

    # 备选定标口径:vx100(设 3.1V -> raw 310;读 raw 310 -> 3.1V)
    sc100 = Scale(SCALE_VX100)
    assert sc100.voltage_raw(3.1) == 310 and sc100.current_raw(1.2) == 120
    assert abs(sc100.voltage_of(310) - 3.1) < 1e-9 and abs(sc100.current_of(120) - 1.2) < 1e-9
    # vx10(0.1V)
    sc10 = Scale(SCALE_VX10)
    assert sc10.voltage_raw(3.1) == 31 and abs(sc10.voltage_of(31) - 3.1) < 1e-9

    # 负值补码:构造 -3.1V(16bit 补码) / -1.2A(24bit 补码) 应答
    def signed(raw, bits):
        sign = 1 << (bits - 1)
        return raw - (1 << bits) if raw & sign else raw
    raw_v_neg = (-3100) & 0xFFFF
    raw_i_neg = (-1200) & 0xFFFFFF
    data_neg = (raw_v_neg.to_bytes(2, "little") + b"\x00"
                + raw_i_neg.to_bytes(3, "little") + bytes([0b10, 25]))
    pn = parse_param_frame(make_id(CMD_READ, dist=MASTER_ADDR, src=5), data_neg)
    assert abs(pn["volts"] - (-3.1)) < 1e-9 and abs(pn["amps"] - (-1.2)) < 1e-9
    assert signed(raw_v_neg, 16) == -3100 and signed(raw_i_neg, 24) == -1200

    print("battery_proto 自检通过 ✓")
