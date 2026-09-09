#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
充放电模拟(SOC 驱动电压)· 曲线与状态机(纯逻辑,无 Qt 依赖)。

设计:
- 两种电池 OCV-SOC 分段曲线(三元 NCM / 磷酸铁锂 LFP),SOC→开路电压(V)。
- 每个运行通道一个 CellSim:
  · 模拟电流模式:按面板设定电流 + 「开始充电/开始放电」按钮的方向积分容量;
  · 实际电流模式:按设备回读电流积分,方向由符号自动判定(负=充电、正=放电),
    电流落入死区(≈空载)时保持 SOC 不变并等待,面板只配置「限流」。
- 推 SOC → 查表得目标电压 → 由调用方(GUI)下发到设备。
- 达到截止 SOC 自动停止(阶段=done),电压保持截止点。

用途:模拟电流模式便于测试 BMU 电压采集;实际电流模式便于测试 BMU SOC 计算。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# ---------------- OCV-SOC 曲线(SOC% -> OCV V) ----------------
# 三元锂 NCM:典型 3.0V(0%)~4.2V(100%) 非线性
NCM_OCV = [
    (0, 3.000), (5, 3.400), (10, 3.600), (20, 3.700), (30, 3.760),
    (40, 3.800), (50, 3.840), (60, 3.880), (70, 3.930), (80, 4.000),
    (90, 4.120), (95, 4.180), (100, 4.200),
]
# 磷酸铁锂 LFP:平台区平坦,两端陡
LFP_OCV = [
    (0, 2.500), (5, 2.900), (10, 3.200), (20, 3.300), (30, 3.320),
    (40, 3.330), (50, 3.340), (60, 3.350), (70, 3.360), (80, 3.400),
    (90, 3.500), (95, 3.600), (100, 3.650),
]
CURVES = {"NCM 三元": NCM_OCV, "LFP 磷酸铁锂": LFP_OCV}

# 实际电流模式的电流死区(A):回读电流绝对值小于此值视为"空载/方向待定",不积分。
REAL_DEADBAND_A = 0.005

# ---------------- SOC 仿真单元 ----------------
@dataclass
class CellSim:
    ch: int
    curve: list = field(default_factory=lambda: NCM_OCV)
    rated_ah: float = 1.0           # 额定容量 Ah
    soc_start: float = 0.0          # 起始 SOC%
    soc_end: float = 100.0          # 截止 SOC%
    current: float = 1.0            # 目标电流 A(模拟模式)/ 限流 A(实际电流模式)
    charge: bool | None = True      # True=充电(SOC↑) False=放电(SOC↓) None=按回读电流自动判定
    mode: str = "sim"               # 'sim' 模拟电流 / 'real' 实际电流
    soc: float = 0.0                # 当前 SOC%
    ah: float = 0.0                 # 累计容量 Ah
    running: bool = False
    done: bool = False
    last_t: float = 0.0
    dir_amp: float = 0.0            # 实际电流模式:最近一次用于积分的回读电流(A,带符号)
    stop_reason: str = ""           # 'target'=达目标 SOC / 'limit'=到物理满/空

    def start(self, now: float) -> None:
        self.soc = self.soc_start
        self.ah = self.rated_ah * self.soc_start / 100.0      # Ah
        self.running = True
        self.done = False
        self.stop_reason = ""
        self.last_t = now
        self.dir_amp = 0.0

    def stop(self) -> None:
        self.running = False

    @property
    def direction(self) -> str:
        """当前方向。实际电流模式按回读符号判定:负=充电、正=放电(用户约定)。"""
        if self.mode == "real":
            if abs(self.dir_amp) < REAL_DEADBAND_A:
                return "待定"
            return "充电" if self.dir_amp < 0 else "放电"
        return "充电" if self.charge else "放电"

    def step(self, dt: float, readback_amp: float | None = None) -> float:
        """前进 dt 秒。返回当前目标电压(V);达到截止置 done。

        readback_amp: 实际电流模式取设备回读(A,负=充/正=放);模拟模式用 self.current。
        实际电流模式下回读缺失或落在死区内时保持 SOC 不变(方向待定,继续等待)。
        """
        if not self.running:
            return self.voltage()
        if self.mode == "real":
            if readback_amp is None or abs(float(readback_amp)) < REAL_DEADBAND_A:
                return self.voltage()
            self.dir_amp = float(readback_amp)
            i = -self.dir_amp                       # 负为充:充时 i>0(SOC↑)
        else:
            i = abs(self.current) if self.charge else -abs(self.current)
        prev_soc = self.soc
        self.ah += i * dt / 3600.0
        self.soc = self.ah / max(self.rated_ah, 1e-9) * 100.0
        # 截止判定:本步「越过」目标才停——起始值恰好等于目标时不会立刻截止
        # (历史 bug:起=100/止=100 按放电 → 第一步就判达截止,放电根本跑不起来)。
        # 方向与目标相反时(实际电流由外部设备决定)不假造 SOC,一路跟到物理满/空。
        reached = ((i > 0 and prev_soc < self.soc_end <= self.soc)
                   or (i < 0 and prev_soc > self.soc_end >= self.soc))
        at_limit = (i > 0 and self.soc >= 100.0) or (i < 0 and self.soc <= 0.0)
        if reached or at_limit:
            if reached:
                self.soc = self.soc_end
            self.soc = max(0.0, min(100.0, self.soc))
            self.ah = self.rated_ah * self.soc / 100.0
            self.running = False
            self.done = True
            self.stop_reason = "target" if reached else "limit"
        return self.voltage()

    def voltage(self) -> float:
        """查表 SOC→电压,线性插值。SOC 越界钳制。"""
        soc = max(0.0, min(100.0, self.soc))
        pts = self.curve
        if soc <= pts[0][0]:
            return pts[0][1]
        for i in range(1, len(pts)):
            x0, y0 = pts[i - 1]
            x1, y1 = pts[i]
            if soc <= x1:
                return y0 + (y1 - y0) * (soc - x0) / (x1 - x0)
        return pts[-1][1]


def scale_curve(curve: list, vmin: float, vmax: float) -> list:
    """把 OCV-SOC 曲线的 0%/100% 端点线性映射到 [vmin, vmax](保持曲线形状)。

    面板的「最低电压/最高电压」即 0%/100% SOC 位置:只做仿射变换,
    中间各点(平台、陡区)的相对形状不变,因此 NCM/LFP 的非线性仍然有效。
    vmin/vmax 非法(反序、非有限值)时原样返回,由调用方校验。
    """
    if not curve:
        return []
    lo, hi = float(curve[0][1]), float(curve[-1][1])
    if not (math.isfinite(vmin) and math.isfinite(vmax)) or vmax <= vmin or hi <= lo:
        return list(curve)
    k = (vmax - vmin) / (hi - lo)
    return [(s, vmin + (v - lo) * k) for s, v in curve]


def target_from_soc(soc: float, curve: list) -> float:
    """独立查表工具(测试用):SOC% → OCV V。"""
    soc = max(0.0, min(100.0, soc))
    if soc <= curve[0][0]:
        return curve[0][1]
    for i in range(1, len(curve)):
        x0, y0 = curve[i - 1]
        x1, y1 = curve[i]
        if soc <= x1:
            return y0 + (y1 - y0) * (soc - x0) / (x1 - x0)
    return curve[-1][1]


def soc_from_voltage(volts: float, curve: list) -> float:
    """按当前电压反查 SOC%(OCV 曲线反向插值, 需求:起始 SOC 估值)。

    电压超出曲线范围时钳制到端点 SOC(0% / 100%)。
    """
    pts = curve
    v0, v1 = pts[0][1], pts[-1][1]
    if volts <= v0:
        return pts[0][0]
    if volts >= v1:
        return pts[-1][0]
    for i in range(1, len(pts)):
        y0, s0 = pts[i - 1][1], pts[i - 1][0]
        y1, s1 = pts[i][1], pts[i][0]
        if volts <= y1:
            if y1 <= y0:
                return s0
            return s0 + (s1 - s0) * (volts - y0) / (y1 - y0)
    return pts[-1][0]
