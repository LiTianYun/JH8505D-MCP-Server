#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程控模拟电池控制台 GUI(PySide6 · 毛玻璃拟态版):连接管理 + 24 通道卡片矩阵实时监控 + 多选控制。

文件分工:
- battery_gui.ui     界面布局(Qt Designer 标准文件,可可视化编辑;自定义控件为 promoted)
- battery_gui_ui.py  设计 Token / QSS / 玻璃自绘控件(GlassPanel / FeaturedPanel / ChannelCard)
- 本文件             业务逻辑:后端接入、事件泵、批量控制、日志(按类型着色)

架构:GUI / CLI / MCP 都是后端服务 battery_server.py 的前端;后端唯一持有真实 CAN 卡
(厂商 DLL 桥接,与 BMUTesterProject 一致),周期轮询 24 通道并广播回读结果。因此:
- 本窗口能实时看到 CLI、AI(MCP) 对任意通道的每一次操作与设备回读(监控能力);
- 关闭本窗口不影响后端与 AI 继续工作;重开窗口即重新接入监控;
- 多前端并存互不抢占。

依赖:PySide6 >= 6.4 + 厂商 DLL(见 battery_vci/battery_zlg)。
用法:python battery_gui.py            # 交互界面
     python battery_gui.py --shot x.png  # 离屏渲染截图(注入演示数据,调试用)
"""
from __future__ import annotations

import sys
import re
import math
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QFile
from PySide6.QtGui import QPainter, QPixmap, QTextCursor
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
                               QComboBox, QLineEdit, QPlainTextEdit,
                               QMessageBox, QInputDialog, QVBoxLayout,
                               QTabWidget)

import battery_gui_ui as ui
import battery_offset as boff
import battery_soc as bsoc
from battery_server import (PROTO_VERSION, ServiceClient, ensure_server, probe,
                            server_addr)
from battery_proto import CH_MAX, CH_MIN, V_MAX, A_MAX, fmt_bitrate

UI_FILE = Path(__file__).with_name("battery_gui.ui")

# 波特率下拉标签 -> bps 数值(标签需与 battery_gui.ui 中 cb_baud 的项一致)
BAUD_MAP = {"100 kHz": 100000, "125 kHz": 125000, "250 kHz": 250000,
            "500 kHz": 500000, "1 MHz": 1000000}

# 本前端要求的后端协议版本:端口只能被一个进程占用,旧后端进程会继续为新
# 界面服务(实测:quiet 被忽略→SOC 自动下发仍逐条刷 TX 日志)。低于此版本
# 一律提示重启后端,不静默容忍。
REQUIRED_PROTO = PROTO_VERSION
# SOC 自动下发回声过滤:"设参数 ch17 V=3.10 A=1"(后端广播的 TX 文本)
_RE_SET_CH = re.compile(r"设参数\s+ch(\d+)")


# ================================================================ 线程桥(结果回 GUI 线程)
class _Bridge(QObject):
    done = Signal(object, object)      # (result, callback)


# ================================================================ 主窗口
class BatteryWindow(QWidget):
    """自绘毛玻璃背景的顶层窗口;内容全部来自 battery_gui.ui。"""

    def __init__(self):
        super().__init__()
        self.svc: ServiceClient | None = None
        self._busy_count = 0       # 操作互斥计数(busy property 由它派生)
        self.bus_open = False
        self.channel_states: dict[int, dict] = {}
        self.selected: set[int] = set()
        self._scale_name = ""          # 当前总线定标(诊断显示用)
        self._diag_logged = False      # 每次接入后端后,首帧回读打印一次 raw 诊断
        self._proto_warned = False     # 后端版本过旧只提示一次(避免重复刷屏)
        self._soc_quiet_until: dict[int, float] = {}   # ch -> 静音截止(单调时钟)
        self._soc_dir_text_cache = ""                  # 方向汇总标签缓存(防重复写控件)
        # 充放电模拟状态:必须在 _wire_ui() 之前就绪(接线时会刷新曲线/方向汇总)
        self._soc_sims: dict[int, bsoc.CellSim] = {}
        self._blur_bg = QPixmap()
        self._clear_bg = QPixmap()
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._on_thread_done)
        self._workers: set[threading.Thread] = set()   # 在途 worker,close 时等待

        self._load_ui()
        self._wire_ui()
        self._set_ctrl_enabled(False)

        # 定时器:事件泵 + 后端看门狗 + 首次自动连接
        self._drain_timer = QTimer(self)
        self._drain_timer.timeout.connect(self._drain_loop)
        self._drain_timer.start(150)
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._watch_backend)
        self._watch_timer.start(2000)
        QTimer.singleShot(200, self.connect_service)
        # 卡片迷你趋势图采样:0.1s/点(后端提速后可达 ~10Hz 轮询)
        self._spark_timer = QTimer(self)
        self._spark_timer.timeout.connect(self._sample_sparks)
        self._spark_timer.start(100)
        # 充放电模拟驱动:1s 一步积分容量并按 SOC 下发电压(0.5s 会过度刷帧)
        self._soc_timer = QTimer(self)
        self._soc_timer.timeout.connect(self._soc_tick)
        self._soc_timer.start(1000)

    # ---------------- 加载 .ui 与接线
    def _load_ui(self) -> None:
        loader = QUiLoader()
        loader.registerCustomWidget(ui.GlassPanel)
        loader.registerCustomWidget(ui.FeaturedPanel)
        loader.registerCustomWidget(ui.ChannelCard)
        loader.registerCustomWidget(ui.SocCurve)
        f = QFile(str(UI_FILE))
        f.open(QFile.ReadOnly)
        try:
            self.ui = loader.load(f, self)
        finally:
            f.close()
        if self.ui is None:
            raise RuntimeError(f"加载界面文件失败: {UI_FILE}")
        self.setWindowTitle(self.ui.windowTitle())
        self.resize(self.ui.width(), self.ui.height())
        self.setMinimumSize(self.ui.minimumSize())
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(self.ui)
        self.ui.setStyleSheet(ui.qss_text())

    def _w(self, name: str, cls=QWidget):
        w = self.ui.findChild(cls, name)
        if w is None:
            raise RuntimeError(f"battery_gui.ui 缺少控件: {name}")
        return w

    def _wire_ui(self) -> None:
        """按 objectName 取控件、连信号(改名需同步 battery_gui.ui)。"""
        # 控件引用
        self.ed_addr = self._w("ed_addr", QLineEdit)
        self.btn_svc = self._w("btn_svc", QPushButton)
        self.btn_backend = self._w("btn_backend", QPushButton)   # 启动/停止后端一体
        self.lbl_svc = self._w("lbl_svc", QLabel)
        self.lbl_clients = self._w("lbl_clients", QLabel)
        self.cb_vendor = self._w("cb_vendor", QComboBox)
        self.ed_channel = self._w("ed_channel", QComboBox)   # CAN1/CAN2 下拉
        self.cb_baud = self._w("cb_baud", QComboBox)
        self.btn_bus = self._w("btn_bus", QPushButton)           # 打开/关闭总线一体
        self.lbl_bus = self._w("lbl_bus", QLabel)
        self.lbl_sel = self._w("lbl_sel", QLabel)
        self.ed_volts = self._w("ed_volts", QLineEdit)
        self.ed_amps = self._w("ed_amps", QLineEdit)
        self.btn_set = self._w("btn_set", QPushButton)
        self.btn_relay_on = self._w("btn_relay_on", QPushButton)
        self.btn_relay_off = self._w("btn_relay_off", QPushButton)
        self.log_text = self._w("log_text", QPlainTextEdit)

        # 字体(衬线标题 / 等宽日志 · 回退链在 ui 模块)
        self._w("lblTitle", QLabel).setFont(ui.make_font(ui.SERIF, 20, ui.QFont.DemiBold))
        self._w("lblControlTitle", QLabel).setFont(ui.make_font(ui.SERIF, 17, ui.QFont.DemiBold))
        self._w("lblCardsTitle", QLabel).setFont(ui.make_font(ui.SANS, 13, ui.QFont.DemiBold))
        for name in ("lblSubtitle", "lblCardsHint", "lbl_clients", "lbl_sel"):
            self._w(name, QLabel).setFont(ui.make_font(ui.SANS, 11))
        self._w("lblLogLegend", QLabel).setFont(ui.make_font(ui.SANS, 10))
        self.log_text.setFont(ui.make_font(ui.MONO, 12))

        # 阴影(规范 shadow-glass token;日志面板阴影加强,浅色卡浮在浅底上需要更重投影)
        ui.add_shadow(self._w("topPanel"), blur=18, dy=6, alpha=46)
        ui.add_shadow(self._w("controlPanel"), blur=26, dy=10, alpha=80)
        ui.add_shadow(self._w("logPanel"), blur=24, dy=8, alpha=95)

        # 量子蓝按钮光晕:accent 实底主按钮 + 同色光晕(对应 HTML .btn box-shadow)
        ui.add_accent_glow(self._w("btn_svc", QPushButton), alpha=46, blur=16)
        ui.add_accent_glow(self._w("btn_set", QPushButton), alpha=46, blur=16)
        # 语义按钮同色光晕:电池输出=绿 / 电池关断=红(浅色语义实底)
        ui.add_semantic_glow(self._w("btn_relay_on", QPushButton), (22, 163, 74), 42)
        ui.add_semantic_glow(self._w("btn_relay_off", QPushButton), (220, 38, 38), 42)

        # 默认后端地址(环境变量 BATTERY_SERVER 可覆盖)
        h, p = server_addr()
        self.ed_addr.setText(f"{h}:{p}")

        # 通道卡片(投影由卡片自持:常规深影,继电器闭合时切换为品牌蓝灯圈辉光)
        self.cards: dict[int, ui.ChannelCard] = {}
        for card in self.ui.findChildren(ui.ChannelCard):
            self.cards[card.ch] = card
            card.clicked.connect(self._on_card_clicked)
            card.double_clicked.connect(self._on_card_double)
            card.offset_requested.connect(self._on_card_offset)
            card.set_state(None)
        if len(self.cards) != (CH_MAX - CH_MIN + 1):
            raise RuntimeError(f"battery_gui.ui 通道卡片数量异常: {len(self.cards)}")
        # 启动时载入本机电压偏移表并显示于卡片
        self._load_offsets_to_cards()

        # 信号接线
        self.btn_svc.clicked.connect(self.connect_service)
        self.btn_backend.clicked.connect(self.toggle_backend)
        self.cb_vendor.currentTextChanged.connect(self._vendor_changed)
        self.btn_bus.clicked.connect(self.toggle_bus)
        self._w("btn_sel_all", QPushButton).clicked.connect(self.select_all)
        self._w("btn_sel_invert", QPushButton).clicked.connect(self.invert_selection)
        self._w("btn_sel_clear", QPushButton).clicked.connect(self.clear_selection)
        self.btn_set.clicked.connect(self.set_output)
        self.btn_relay_on.clicked.connect(lambda: self.set_relay(True))
        self.btn_relay_off.clicked.connect(lambda: self.set_relay(False))
        self.ed_volts.returnPressed.connect(self.set_output)
        self.ed_amps.returnPressed.connect(self.set_output)
        self._w("btn_clear_log", QPushButton).clicked.connect(self._clear_log)

        # 充放电模拟面板(SOC 驱动电压)
        self.cb_chem = self._w("cb_chem", QComboBox)
        self.cb_cur_src = self._w("cb_cur_src", QComboBox)
        self.ed_soc_cur = self._w("ed_soc_cur", QLineEdit)
        self.ed_soc_cap = self._w("ed_soc_cap", QLineEdit)
        self.ed_soc_vmax = self._w("ed_soc_vmax", QLineEdit)
        self.ed_soc_vmin = self._w("ed_soc_vmin", QLineEdit)
        self.ed_soc_from = self._w("ed_soc_from", QLineEdit)
        self.ed_soc_to = self._w("ed_soc_to", QLineEdit)
        self.cb_chem.currentIndexChanged.connect(self._chem_changed)
        self._chem_changed()          # 初始化最高/最低电压为该曲线默认端点
        self.btn_soc_start = self._w("btn_soc_start", QPushButton)
        self.btn_soc_discharge = self._w("btn_soc_discharge", QPushButton)
        self.btn_soc_stop = self._w("btn_soc_stop", QPushButton)
        self.btn_soc_start.clicked.connect(lambda: self._soc_start(charge=True))
        self.btn_soc_discharge.clicked.connect(lambda: self._soc_start(charge=False))
        # 注意:clicked 会带 checked 布尔参,这里用 lambda 丢掉,避免误当 log=False
        self.btn_soc_stop.clicked.connect(lambda *_: self._soc_stop_all())
        self.btn_soc_est = self._w("btn_soc_est", QPushButton)
        self.btn_soc_est.clicked.connect(self._soc_estimate)
        # 电流源切换:模拟电流=面板电流 + 充/放两个按钮;实际电流=面板限流 + 单个「开始」
        self.lbl_cur = self._w("lblCur", QLabel)
        self.lbl_soc_dir = self._w("lblSocDir", QLabel)   # 实际电流方向汇总
        self.w_curve = self._w("soc_curve", ui.SocCurve)  # SOC-电压曲线预览
        self.tab_log_curve = self._w("tabLogCurve", QTabWidget)
        self.cb_cur_src.currentIndexChanged.connect(self._soc_mode_changed)
        self._soc_mode_changed()
        # 曲线预览:面板参数变化即时重画(窗口/曲线/化学体系)
        self.ed_soc_vmin.textChanged.connect(self._soc_curve_refresh)
        self.ed_soc_vmax.textChanged.connect(self._soc_curve_refresh)
        self.cb_chem.currentIndexChanged.connect(self._soc_curve_refresh)
        self._soc_curve_refresh()

    # ---------------- 背景(方向性光源 + 静态色斑 + 磨砂底图)
    def rebuild_backgrounds(self):
        w, h = max(1, self.width()), max(1, self.height())
        self._clear_bg, self._blur_bg = ui.render_backgrounds(w, h)

    def paintEvent(self, ev):
        p = QPainter(self)
        if not self._clear_bg.isNull():
            p.drawPixmap(0, 0, self._clear_bg)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.rebuild_backgrounds()

    # ---------------- 日志(按类型着色)
    LOG_COLORS = {"INFO": ui.C_INFO, "TX": ui.C_TX, "OK": ui.C_OK,
                  "WARN": ui.C_WARN, "ERR": ui.C_ERR}

    def log(self, text: str, kind: str = "INFO") -> None:
        stamp = time.strftime("%H:%M:%S")
        color = self.LOG_COLORS.get(kind, ui.C_INFO)
        esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        self.log_text.appendHtml(
            f'<span style="color:{ui.C_SUB};">[{stamp}]</span> '
            f'<span style="color:{color};">{esc}</span>')
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())
        # 限长,避免长时间运行膨胀
        doc = self.log_text.document()
        while doc.blockCount() > 800:
            cur = QTextCursor(doc)
            cur.movePosition(QTextCursor.Start)
            cur.select(QTextCursor.BlockUnderCursor)
            cur.removeSelectedText()
            cur.deleteChar()

    def _clear_log(self):
        self.log_text.clear()

    # ---------------- 充放电模拟(SOC 驱动电压) ----------------
    # 各曲线的默认 0%/100% SOC 端点电压(V):最低/最高,可被面板输入覆盖
    CURVE_ENDS = {"NCM 三元": (3.000, 4.200), "LFP 磷酸铁锂": (2.500, 3.650)}

    def _chem_changed(self) -> None:
        """切换曲线:把最高/最低电压填为该曲线的默认端点(用户可再改)。"""
        lo, hi = self.CURVE_ENDS.get(self.cb_chem.currentText(), (3.0, 4.2))
        self.ed_soc_vmin.setText(f"{lo:.2f}")
        self.ed_soc_vmax.setText(f"{hi:.2f}")

    def _soc_curve(self) -> list | None:
        """按「曲线 + 面板最高/最低电压」构造 OCV 曲线:最低=0% SOC、最高=100% SOC。

        非法输入(非数字/反序/超量程)返回 None,由调用方给出明确提示。
        """
        base = bsoc.CURVES.get(self.cb_chem.currentText())
        if base is None:
            return None
        try:
            vmin = float(self.ed_soc_vmin.text().strip())
            vmax = float(self.ed_soc_vmax.text().strip())
        except ValueError:
            return None
        if not (math.isfinite(vmin) and math.isfinite(vmax)):
            return None
        if not (0.0 < vmin < vmax <= V_MAX):
            return None
        return bsoc.scale_curve(base, vmin, vmax)

    def _soc_mode(self) -> str:
        """面板电流源模式:'sim' 模拟电流 / 'real' 实际电流。"""
        return "sim" if self.cb_cur_src.currentIndex() == 0 else "real"

    def _soc_mode_changed(self) -> None:
        """按电流源模式切换面板语义(需求:电流配置与充/放按钮只对模拟电流生效)。

        - 模拟电流:输入框 = 电流(A),两个按钮「开始充电 / 开始放电」;
        - 实际电流:输入框 = 限流(A),只留一个「开始」按钮,充/放方向由回读电流
          符号自动判定(负=充电、正=放电),因此不需要用户选方向。
        """
        real = self._soc_mode() == "real"
        self.lbl_cur.setText("限流(A)" if real else "电流(A)")
        self.btn_soc_discharge.setVisible(not real)
        self.btn_soc_start.setText("开始" if real else "开始充电")
        self.btn_soc_start.setToolTip(
            "按回读电流自动判定方向:负=充电、正=放电" if real
            else "按面板电流放电/充电方向模拟")
        self.lbl_soc_dir.setVisible(real)
        self._soc_dir_update()
        # 按钮显隐改变行宽,重排一次布局避免残留空白
        row = self.btn_soc_start.parentWidget()
        if row is not None and row.layout() is not None:
            row.layout().activate()

    def _soc_params(self) -> dict | None:
        """读面板参数并校验。单位:电流 A、容量 Ah、电压 V。返回参数字典或 None(已弹错)。"""
        mode = self._soc_mode()
        try:
            cur = float(self.ed_soc_cur.text().strip())
            cap = float(self.ed_soc_cap.text().strip())
            s0 = float(self.ed_soc_from.text().strip())
            s1 = float(self.ed_soc_to.text().strip())
        except ValueError:
            QMessageBox.warning(self, "充放电模拟", "电流(A)/容量(Ah)/SOC 需为数字。")
            return None
        if not (0 < cur <= 10.0 and 0 < cap <= 1000.0):
            QMessageBox.warning(self, "充放电模拟", "电流(0~10A)/容量(0~1000Ah)超范围。")
            return None
        if not (0 <= s0 <= 100 and 0 <= s1 <= 100):
            QMessageBox.warning(self, "充放电模拟", "SOC 需在 0~100 之间。")
            return None
        curve = self._soc_curve()
        if curve is None:
            QMessageBox.warning(
                self, "充放电模拟",
                f"最高/最低电压需为数字且 0 < 最低 < 最高 ≤ {V_MAX:g}V。")
            return None
        chem = self.cb_chem.currentText()
        return {"curve": curve, "current": cur,
                "rated_ah": cap, "soc0": s0, "soc1": s1, "mode": mode}

    def _soc_estimate(self) -> None:
        """按选中通道当前电压反查曲线估值 SOC,填入起始 SOC 框(需求 3)。

        使用面板「最低/最高电压」定义的 0%/100% 窗口,与模拟所用曲线完全一致。
        """
        if not self.selected:
            QMessageBox.information(self, "充放电模拟", "请先选中一个通道。")
            return
        ch = sorted(self.selected)[0]
        st = self.channel_states.get(ch)
        if not (st and st.get("online")):
            QMessageBox.warning(self, "充放电模拟", f"CH{ch} 当前不在线,无法估值。")
            return
        curve = self._soc_curve()
        if curve is None:
            QMessageBox.warning(
                self, "充放电模拟",
                f"最高/最低电压需为数字且 0 < 最低 < 最高 ≤ {V_MAX:g}V。")
            return
        v = float(st.get("volts", 0.0))
        soc = bsoc.soc_from_voltage(v, curve)
        self.ed_soc_from.setText(f"{soc:.0f}")
        self.log(f"[充放电] CH{ch} 电压 {v:.3f}V → 估算起始 SOC {soc:.0f}%"
                 f"(曲线 {self.cb_chem.currentText()} {curve[0][1]:.2f}~{curve[-1][1]:.2f}V)",
                 "INFO")

    def _soc_start(self, charge: bool | None) -> None:
        """启动充放电模拟。charge: True 充 / False 放 / None 实际电流模式自动判定。"""
        if not self.bus_open:
            QMessageBox.warning(self, "充放电模拟", "请先打开总线会话。")
            return
        chs = self._selected_sorted()
        if chs is None:
            return
        p = self._soc_params()
        if p is None:
            return
        auto = (p["mode"] == "real")
        if auto:
            charge = None          # 实际电流:方向由回读电流符号决定(负=充/正=放)
        else:
            # 模拟电流:目标 SOC 必须在行进方向上,否则一启动就"已到目标"
            # (历史现象:估起始SOC=100 + 目标SOC默认100,按放电立刻报"达截止 100%")
            if charge and p["soc1"] <= p["soc0"]:
                QMessageBox.warning(
                    self, "充放电模拟",
                    f"充电需目标 SOC 大于起始 SOC(当前 {p['soc0']:.0f}%→{p['soc1']:.0f}%)。\n"
                    f"请把「目标SOC起→止」的止值改大,或改用「开始放电」。")
                return
            if not charge and p["soc1"] >= p["soc0"]:
                QMessageBox.warning(
                    self, "充放电模拟",
                    f"放电需目标 SOC 小于起始 SOC(当前 {p['soc0']:.0f}%→{p['soc1']:.0f}%)。\n"
                    f"请把「目标SOC起→止」的止值改小,或改用「开始充电」。")
                return
        # 仅对在线通道启动(离线通道无应答,持续下发会把总线打进 bus-off)
        online = [c for c in chs
                  if not (c in self.channel_states
                          and not self.channel_states[c].get("online"))]
        skipped = [c for c in chs if c not in online]
        if skipped:
            self.log(f"[充放电] 跳过离线通道: {', '.join(map(str, skipped))}", "WARN")
        if not online:
            QMessageBox.information(self, "充放电模拟", "选中通道均已离线,无可模拟目标。")
            return
        for ch in online:
            sim = bsoc.CellSim(ch, curve=p["curve"], rated_ah=p["rated_ah"],
                               soc_start=p["soc0"], soc_end=p["soc1"],
                               current=p["current"], charge=charge,
                               mode=p["mode"])
            sim.start(0.0)
            self._soc_sims[ch] = sim
            self.cards[ch].set_soc(p["soc0"], p["rated_ah"] * p["soc0"] / 100.0)
        vwin = f"{p['curve'][0][1]:.2f}~{p['curve'][-1][1]:.2f}V"
        if auto:
            self.log(f"[充放电] 实际电流启动 {len(online)} 通道: "
                     f"{p['rated_ah']:.2f}Ah SOC {p['soc0']:.0f}→{p['soc1']:.0f}% "
                     f"限流 {p['current']:.2f}A · 0/100%={vwin}"
                     f"(方向按回读电流自动判定:负=充/正=放)", "OK")
        else:
            dirn = "充电" if charge else "放电"
            self.log(f"[充放电] {dirn}启动 {len(online)} 通道: "
                     f"{p['rated_ah']:.2f}Ah SOC {p['soc0']:.0f}→{p['soc1']:.0f}% "
                     f"{p['current']:.2f}A · 0/100%={vwin}(模拟电流)", "OK")
        self._soc_dir_update()
        self._soc_curve_refresh()

    def _soc_dir_text(self) -> str:
        """实际电流模式的方向汇总(面板右侧一行,变化才刷新)。"""
        if not self._soc_sims:
            return ""
        cnt = {"充电": 0, "放电": 0, "待定": 0}
        for sim in self._soc_sims.values():
            cnt[sim.direction] = cnt.get(sim.direction, 0) + 1
        parts = [f"{k} {cnt[k]}" for k in ("充电", "放电", "待定") if cnt.get(k)]
        return "实际电流: " + " · ".join(parts) if parts else ""

    def _soc_dir_update(self) -> None:
        """刷新方向汇总标签(仅实际电流模式;文本未变则不写控件)。"""
        if self.lbl_soc_dir is None:
            return
        txt = self._soc_dir_text() if self._soc_mode() == "real" else ""
        if txt != self._soc_dir_text_cache:
            self._soc_dir_text_cache = txt
            self.lbl_soc_dir.setText(txt)

    def _soc_stop_all(self, log: bool = True) -> None:
        for sim in self._soc_sims.values():
            sim.stop()
        self._soc_sims.clear()
        self._soc_quiet_until.clear()
        for ch in self.cards:
            self.cards[ch].set_soc(None)
        self._soc_dir_update()
        self._soc_curve_refresh()
        if log:
            self.log("[充放电] 全部停止。", "WARN")

    def _soc_tick(self) -> None:
        """1s 一步:积分容量→按 SOC 查表得电压→下发到各运行通道,并把
        SOC/容量显示在对应电芯卡片上(需求 2)。

        安全护栏(防发送风暴/bus-off):
        - 仅对仍在线通道下发;离线通道立即停止模拟并提示。
        - 目标电压与上次实发值相差 < 0.005V 时跳过发送(电压平台期不刷帧)。
        全程不逐条打日志(需求 5),仅 开始/截止/异常 记一条。
        """
        if not self._soc_sims:
            return
        if not (self.svc and self.svc._alive and self.bus_open):
            return
        jobs = []
        done_chs = []
        for ch, sim in list(self._soc_sims.items()):
            if not sim.running:
                done_chs.append(ch)
                continue
            st = self.channel_states.get(ch)
            online = bool(st and st.get("online"))
            if not online:
                done_chs.append(ch)
                self.log(f"[充放电] CH{ch} 已离线,停止模拟(防总线卡死)。", "ERR")
                continue
            # 实际电流模式:用最近回读电流积分(方向由符号判定,负=充/正=放)
            rb = None
            if sim.mode == "real":
                rb = float(st.get("amps", 0.0))
            v = sim.step(1.0, readback_amp=rb)
            # 卡片显示当前 SOC/容量
            self.cards[ch].set_soc(sim.soc, sim.ah)
            if sim.done:
                done_chs.append(ch)
                if sim.stop_reason == "limit":
                    edge = "满" if sim.soc >= 99.5 else "空"
                    self.log(f"[充放电] CH{ch} {sim.direction}至 SOC {sim.soc:.0f}%"
                             f"({edge},物理边界),电压 {v:.3f}V 保持。", "WARN")
                else:
                    self.log(f"[充放电] CH{ch} {sim.direction}达截止 SOC {sim.soc:.0f}%,"
                             f"电压 {v:.3f}V 保持。", "WARN")
                continue
            # 电压变化阈值:平台期不重复刷帧
            prev = getattr(sim, "_last_sent_v", None)
            if prev is None or abs(v - prev) >= 0.005:
                sim._last_sent_v = v
                jobs.append((ch, v, sim.current))
                # 记录静音窗口:本次下发对应的 TX 回声 3s 内不写日志
                self._soc_quiet_until[ch] = time.monotonic() + 3.0
        for ch in done_chs:
            self._soc_sims.pop(ch, None)
            self._soc_quiet_until.pop(ch, None)
        self._soc_dir_update()
        self._soc_curve_refresh()
        if jobs:
            self._soc_send_volts(jobs)

    def _soc_send_volts(self, jobs) -> None:
        """逐通道后台 set 目标电压(独立线程,不占用 busy 互斥)。

        失败通道回 GUI 线程停止对应模拟并告警——避免对无应答通道持续
        发送把 CX 卡打进 bus-off(实测 ret=-2 后须重开总线)。
        """
        svc = self.svc
        if svc is None:
            return

        def work():
            bad = []
            for ch, v, a in jobs:
                try:
                    # quiet: SOC 自动下发不产生 TX 日志(防日志风暴, 需求 1)
                    r = svc.request({"cmd": "set", "ch": ch, "volts": v, "amps": a,
                                     "quiet": True}, timeout=10.0)
                    if not r.get("ok"):
                        bad.append((ch, r.get("error", "?")))
                except Exception as ex:
                    bad.append((ch, str(ex)))
            return bad

        def done(bad):
            if not bad:
                return
            # 总线级故障(bus-off/发送失败):一次性停掉全部模拟,只报一条。
            # 后端已自动复位通道,继续每秒对卡死总线写帧只会放大问题。
            bus_fail = [(ch, err) for ch, err in bad
                        if any(k in str(err) for k in ("发送失败", "未打开", "超时", "断开"))]
            if bus_fail:
                n = len(self._soc_sims)
                self._soc_stop_all(log=False)
                self.log(f"[充放电] 总线发送失败({bus_fail[0][1]}),已停止全部 {n} 通道模拟;"
                         f"后端已自动复位 CAN 通道,请确认接线/在线状态后重试。", "ERR")
                return
            for ch, err in bad:
                self._soc_sims.pop(ch, None)
                self._soc_quiet_until.pop(ch, None)
                self.cards[ch].set_soc(None)
                self.log(f"[充放电] CH{ch} 下发失败({err}),已停止该通道模拟。", "ERR")

        self._run_thread(work, done)

    # ---------------- 状态/按钮
    @staticmethod
    def _set_kind(btn: QPushButton, kind: str) -> None:
        """切换按钮 kind 动态属性并重载 QSS(unpolish/polish)。"""
        if btn.property("kind") != kind:
            btn.setProperty("kind", kind)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _refresh_buttons(self) -> None:
        """一体化按钮状态机:文案/可用性/颜色由 连接/总线/忙 推导。
        停止后端/关闭总线 → 红;启动后端/打开总线 → 绿。"""
        connected = self.svc is not None and self.svc._alive
        busy = self.busy
        self.btn_svc.setText("重连" if connected else "连接")
        self.btn_svc.setEnabled(not busy)
        self.btn_backend.setText("停止后端" if connected else "启动后端")
        self.btn_backend.setEnabled(not busy)
        self._set_kind(self.btn_backend, "danger" if connected else "success")
        self.btn_bus.setText("关闭总线" if self.bus_open else "打开总线")
        self.btn_bus.setEnabled(connected and not busy)
        self._set_kind(self.btn_bus, "danger" if self.bus_open else "success")
        ctrl = connected and self.bus_open and not busy
        for w in (self.btn_set, self.btn_relay_on, self.btn_relay_off):
            w.setEnabled(ctrl)

    # 旧接口保留为薄封装,所有调用点统一走 _refresh_buttons
    def _set_ctrl_enabled(self, enabled: bool) -> None:
        self._refresh_buttons()

    @property
    def busy(self) -> bool:
        """操作互斥:任意一个在途操作未结束时为 True(G4 收敛)。"""
        return self._busy_count > 0

    def _set_busy(self, on: bool) -> None:
        """嵌套计数式 busy:on 增计数,off 减计数(多个并发操作互不误清)。"""
        if on:
            self._busy_count += 1
        elif self._busy_count > 0:
            self._busy_count -= 1
        self._refresh_buttons()

    def _update_svc_buttons(self) -> None:
        self._refresh_buttons()

    def _set_svc_label(self, text, color):
        self.lbl_svc.setText(text)
        self.lbl_svc.setStyleSheet(f"color: {color}; background: transparent;")

    def _set_bus_label(self, text, color):
        self.lbl_bus.setText(text)
        self.lbl_bus.setStyleSheet(f"color: {color}; background: transparent;")

    # ---------------- 线程提交
    def _run_thread(self, fn, on_done) -> None:
        """后台线程跑 fn(),结果经 Signal 回 GUI 线程调 on_done(result)。

        登记在途 worker 供 closeEvent 等待,避免窗口销毁时向已删除的 _Bridge
        发射信号(WORKER 竞态,见审查 G2)。
        """
        t = threading.Thread(target=self._worker_safe, args=(fn, on_done), daemon=True)
        self._workers.add(t)
        t.start()

    def _worker_safe(self, fn, on_done) -> None:
        try:
            try:
                r = fn()
            except Exception as ex:
                r = {"ok": False, "error": f"通信异常: {ex}"}
            # 跨线程 emit 是 queued;窗口销毁后 receiver 可能已删,
            # 即使 _bridge 引用仍在也会抛 RuntimeError——安全捕获。
            bridge = self._bridge
            if bridge is not None:
                try:
                    bridge.done.emit(r, on_done)
                except RuntimeError:
                    pass    # 窗口已销毁,丢弃结果
        finally:
            # 线程结束即腾出 closeEvent 的等待集合
            self._workers.discard(threading.current_thread())

    def _on_thread_done(self, r, on_done):
        if callable(on_done):
            on_done(r)

    def _require_svc(self) -> bool:
        if self.svc is None or not self.svc._alive:
            QMessageBox.warning(self, "程控模拟电池", "尚未接入后端服务,请先点击「连接」。")
            return False
        return True

    def _submit(self, req: dict, on_done) -> None:
        if not self._require_svc() or self.busy:
            return
        self._set_busy(True)

        def done(r):
            self._set_busy(False)
            on_done(r)

        self._run_thread(lambda: self.svc.request(req), done)

    # ---------------- 服务接入与事件监控
    def _parse_addr(self, quiet=False):
        raw = self.ed_addr.text().strip()
        host, _, port_s = raw.rpartition(":")
        try:
            return (host or "127.0.0.1"), int(port_s)
        except ValueError:
            if not quiet:
                QMessageBox.warning(self, "程控模拟电池", f"地址格式错误: {raw}(应为 HOST:PORT)")
            return None, None

    def connect_service(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.btn_svc.setEnabled(False)
        self.btn_svc.setText("连接中…")
        self._set_svc_label("⏳ 正在接入后端…", ui.C_INFO)
        host, tcp_port = self._parse_addr()
        if host is None:
            self._set_busy(False)
            self._update_svc_buttons()
            return

        def work():
            addr = ensure_server(host, tcp_port)
            if addr is None:
                raise ConnectionError(f"无法连接或启动后端服务 {host}:{tcp_port}")
            cli = ServiceClient("gui", *addr)
            snap = cli.request({"cmd": "status"})
            return {"ok": True, "cli": cli, "snap": snap}

        def done(r):
            if r.get("ok"):
                self._svc_ready(r["cli"], r["snap"])
            else:
                self._svc_failed(r.get("error", "连接失败"))

        self._run_thread(work, done)

    def _svc_ready(self, cli: ServiceClient, snap: dict) -> None:
        # 重连时先关掉旧客户端:否则后端会保留两条 "gui" 连接,事件被发两遍,
        # 旧客户端的读线程与事件队列还会持续堆积(实测后端重启后出现双连接)。
        old = self.svc
        if old is not None and old is not cli:
            try:
                old.close()
            except Exception:
                pass
        self.svc = cli
        self._set_busy(False)
        self._diag_logged = False
        self._update_svc_buttons()
        self._set_svc_label(f"● 已接入 {self.svc.host}:{self.svc.port}", ui.C_OK)
        self._sync_vendor_options()
        self._proto_warned = False      # 新连接:重新判定后端版本
        self._apply_snapshot(snap)
        self.log(f"已接入后端 {self.svc.host}:{self.svc.port},开始实时监控 24 通道。", "OK")

    def _sync_vendor_options(self) -> None:
        try:
            r = self.svc.request({"cmd": "list_vendors"})
        except Exception:
            return
        vendors = r.get("vendors") or []
        if vendors:
            self.cb_vendor.blockSignals(True)
            self.cb_vendor.clear()
            self.cb_vendor.addItems(vendors)
            dv = r.get("default_vendor") or vendors[0]
            if dv in vendors:
                self.cb_vendor.setCurrentText(dv)
            self.cb_vendor.blockSignals(False)
            self.ed_channel.setCurrentText(str(r.get("default_channel") or "CAN1"))

    def _svc_failed(self, msg: str) -> None:
        self._set_busy(False)
        self._update_svc_buttons()
        self._set_svc_label(f"✗ {msg}", ui.C_ERR)
        self.log(msg, "ERR")

    def _drain_loop(self) -> None:
        if self.svc is None:
            return
        try:
            while True:
                ev = self.svc.poll_event(0)
                if ev is None:
                    break
                self._handle_event(ev)
        except Exception as ex:
            # 事件泵异常:记录而非静默(审查 G7);连接断开由 _watch_backend 兜底
            self.log(f"事件处理异常: {ex}", "WARN")

    def _watch_backend(self) -> None:
        if self.svc is not None and not self.svc._alive:
            try:
                self.svc.close()
            except Exception:
                pass
            self.svc = None
            self.bus_open = False
            self._set_bus_label("○ 总线未打开", ui.C_INFO)
            self._set_ctrl_enabled(False)
            self._clear_channel_views()
            self._set_svc_label("○ 与后端断开,等待其重新上线…", ui.C_WARN)
            self._update_svc_buttons()
            self.log("[后端] 连接中断,自动重连已就绪。", "WARN")
        if self.svc is None and not self.busy:
            host, tcp_port = self._parse_addr(quiet=True)
            if host is not None and probe(host, tcp_port):
                self.log("检测到后端服务已上线,自动重连…", "INFO")
                self.connect_service()

    def _handle_event(self, ev: dict) -> None:
        kind = ev.get("ev")
        if kind in ("welcome", "status"):
            self._apply_snapshot(ev)
        elif kind == "channels":
            self._apply_channels(ev.get("channels") or [])
        elif kind == "tx":
            if self._is_own_soc_tx(ev):
                return
            src = "本机" if ev.get("from") == "gui" else ev.get("from", "?")
            self.log(f"[TX·{src}] {ev.get('text', '')}", "TX")
        elif kind == "bus":
            state = ev.get("state")
            if state == "opened":
                self.bus_open = True
                if ev.get("scale"):
                    self._scale_name = str(ev["scale"])
                self.cb_vendor.blockSignals(True)
                self.cb_vendor.setCurrentText(ev.get("vendor") or self.cb_vendor.currentText())
                self.cb_vendor.blockSignals(False)
                self.ed_channel.setCurrentText(ev.get("channel") or self.ed_channel.currentText())
                if ev.get("bitrate"):
                    lbl = self._baud_label(int(ev["bitrate"]))
                    if lbl:
                        self.cb_baud.setCurrentText(lbl)
                self._set_bus_label(
                    f"● 总线: {ev.get('vendor')}:{ev.get('channel')} @ "
                    f"{fmt_bitrate(ev.get('bitrate'))}", ui.C_OK)
                self._set_ctrl_enabled(True)
                self.log(f"[总线] 会话已打开 {ev.get('vendor')}:{ev.get('channel')} @ "
                         f"{fmt_bitrate(ev.get('bitrate'))}", "OK")
            elif state == "lost":
                self.bus_open = False
                self._set_bus_label("✗ 总线异常断开", ui.C_ERR)
                self._set_ctrl_enabled(False)
                self._clear_channel_views()
                self.log("[总线] 会话异常断开", "ERR")
            else:
                self.bus_open = False
                self._set_bus_label("○ 总线未打开", ui.C_INFO)
                self._set_ctrl_enabled(False)
                self._clear_channel_views()
                self.log("[总线] 会话已关闭", "INFO")
        elif kind == "clients":
            names = ev.get("names") or []
            self.lbl_clients.setText(f"在线前端: {', '.join(names)}")
        elif kind == "bye":
            self._set_svc_label("✗ 后端已停止", ui.C_ERR)
            self.lbl_clients.setText("")
            self.bus_open = False
            self._set_bus_label("○ 总线未打开", ui.C_INFO)
            self._set_ctrl_enabled(False)
            self._clear_channel_views()
            self.log("[后端] 服务已停止,可点击「连接」重新拉起。", "WARN")
            if self.svc is not None:
                try:
                    self.svc.close()
                except Exception:
                    pass
                self.svc = None
            self._update_svc_buttons()

    def _is_own_soc_tx(self, ev: dict) -> bool:
        """是否为本窗口充放电模拟自动下发的 TX 回声(需静音)。

        正常后端对 quiet 请求根本不广播;但若端口被旧后端进程占用(忽略 quiet),
        自动调压会每秒刷一条日志。这里按"该通道正在模拟 + 距本次下发 <3s"过滤,
        既压掉噪声,又不影响手动「设参数」等操作的 TX 记录。
        """
        if ev.get("from") != "gui":
            return False
        m = _RE_SET_CH.search(str(ev.get("text") or ""))
        if not m:
            return False
        ch = int(m.group(1))
        if ch not in self._soc_sims:
            return False
        return time.monotonic() < self._soc_quiet_until.get(ch, 0.0)

    def _check_proto(self, st: dict) -> None:
        """检测后端进程协议版本,过旧则提示重启(避免"新界面 + 旧后端")。"""
        try:
            got = int(st.get("proto") or 0)
        except (TypeError, ValueError):
            got = 0
        if got >= REQUIRED_PROTO or self._proto_warned:
            return
        self._proto_warned = True
        self.log(f"[后端] 后端进程版本过旧(proto={got},需 ≥{REQUIRED_PROTO}):"
                 f"quiet/chs 等新字段会被忽略。请点「停止后端」再「启动后端」重启后端。",
                 "ERR")
        self._set_svc_label(f"⚠ 后端版本过旧(proto={got}<{REQUIRED_PROTO}),请重启后端",
                            ui.C_WARN)

    def _clear_channel_views(self) -> None:
        """断开总线/后端停止后,把 24 通道卡片清为离线(--),不再残留最后一帧旧值。"""
        self.channel_states.clear()
        for ch in self.cards:
            self.cards[ch].set_state(None)
            self.cards[ch].clear_sample()
        self._refresh_sel_state()

    def _apply_snapshot(self, st: dict) -> None:
        self._check_proto(st)
        self.bus_open = bool(st.get("connected"))
        bus = st.get("bus") or {}
        if bus.get("scale"):
            self._scale_name = str(bus["scale"])
        if bus.get("vendor"):
            self.cb_vendor.blockSignals(True)
            self.cb_vendor.setCurrentText(bus["vendor"])
            self.cb_vendor.blockSignals(False)
        if bus.get("channel"):
            self.ed_channel.setCurrentText(bus["channel"])
        if bus.get("bitrate"):
            lbl = self._baud_label(int(bus["bitrate"]))
            if lbl:
                self.cb_baud.setCurrentText(lbl)
        names = st.get("clients") or []
        self.lbl_clients.setText(f"在线前端: {', '.join(names)}")
        if self.bus_open:
            self._set_bus_label(
                f"● 总线: {bus.get('vendor')}:{bus.get('channel')} @ "
                f"{fmt_bitrate(bus.get('bitrate'))}", ui.C_OK)
            self._set_ctrl_enabled(True)
        else:
            self._set_bus_label("○ 总线未打开", ui.C_INFO)
            self._set_ctrl_enabled(False)
        self._apply_channels(st.get("channels") or [])
        for h in (st.get("history") or [])[-6:]:
            k = {"tx": "TX", "err": "ERR", "warn": "WARN"}.get(str(h.get("kind", "")).lower(),
                                                               "INFO")
            self.log(f"[{h['time']}][{h['kind']}] {h['text']}", k)

    def _baud_label(self, bps: int) -> str | None:
        for lbl, v in BAUD_MAP.items():
            if v == bps:
                return lbl
        return fmt_bitrate(bps) if bps else None

    def _vendor_changed(self, v: str) -> None:
        if v in ("gcan", "cx", "zlg", "zlg_eu"):
            if self.ed_channel.currentText().upper() not in ("CAN1", "CAN2"):
                self.ed_channel.setCurrentText("CAN1")

    # ---------------- 通道卡片与多选
    def _sample_sparks(self) -> None:
        """0.25s 采样:喂各卡 spark, 并从全体通道窗口数据算全局同步刻度。"""
        st = self.channel_states
        for ch, card in self.cards.items():
            row = st.get(ch)
            if row and row.get("online"):
                card.push_sample(float(row.get("volts")), float(row.get("amps")))
            else:
                card.push_sample(None, None)
        # 全局刻度:全部在线通道 spark 窗口内电压+电流的 min~max(留 5% 边距)
        lo = hi = None
        for ch, card in self.cards.items():
            for vals in (card.spark._v, card.spark._a):
                for v in vals:
                    if v is None:
                        continue
                    if lo is None or v < lo:
                        lo = v
                    if hi is None or v > hi:
                        hi = v
        if lo is not None and hi > lo:
            pad = (hi - lo) * 0.05
            glo, ghi = lo - pad, hi + pad
            for card in self.cards.values():
                card.spark.set_global_range(glo, ghi)
        elif lo is not None:
            # 全部同值: 以该值 ± 小量作刻度
            pad = max(abs(lo) * 0.01, 0.05)
            for card in self.cards.values():
                card.spark.set_global_range(lo - pad, lo + pad)

    def _apply_channels(self, rows: list[dict]) -> None:
        for row in rows:
            ch = row.get("ch")
            if ch is None or ch not in self.cards:
                continue
            self.channel_states[ch] = row
            card = self.cards[ch]
            card.set_state(row)
            if row.get("online"):
                card.setToolTip(
                    f"CH{ch} 原始回读(定标诊断)\n"
                    f"raw_v = {row.get('raw_v')} · raw_i = {row.get('raw_i')}\n"
                    f"[{row.get('raw_hex', '')}]\n"
                    f"当前定标: {self._scale_name or '?'}")
            else:
                card.setToolTip("")
            # 首帧回读打印一次 raw 诊断,便于核对定标口径
            if row.get("online") and not self._diag_logged:
                self._diag_logged = True
                self.log(f"诊断: CH{ch} raw_v={row.get('raw_v')} raw_i={row.get('raw_i')} "
                         f"[{row.get('raw_hex', '')}] 定标={self._scale_name or '?'} · "
                         f"悬停任意卡片可查看原始回读。", "INFO")
        self._refresh_sel_state()

    def _on_card_clicked(self, ch: int) -> None:
        if ch in self.selected:
            self.selected.discard(ch)
        else:
            self.selected.add(ch)
        self.cards[ch].set_selected(ch in self.selected)
        self._refresh_sel_state()

    def _on_card_double(self, ch: int) -> None:
        st = self.channel_states.get(ch)
        if st and st.get("online"):
            self.ed_volts.setText(f"{float(st['volts']):.3f}")
            self.ed_amps.setText(f"{float(st['amps']):.3f}")
        if ch not in self.selected:
            self.selected.add(ch)
            self.cards[ch].set_selected(True)
        self._refresh_sel_state()
        self.log(f"已选中通道 {ch} 并回填当前设定值。", "INFO")

    # ---------------- 电压偏移校准 ----------------
    def _load_offsets_to_cards(self) -> None:
        off = boff.load_offsets()
        for ch, card in self.cards.items():
            card.set_offset(off.get(ch, 0))

    def _on_card_offset(self, ch: int) -> None:
        """右键卡片 → 设置该通道电压偏移(mV)。0 = 清除。"""
        cur = boff.load_offsets().get(ch, 0)
        text, ok = QInputDialog.getText(
            self, f"通道 {ch} 电压偏移",
            f"电压偏移 mV(设 0 清除;正=电压偏低需调高,负=偏高需调低)\n"
            f"当前: {cur:+d} mV  例: 设置后设 3.200V 实际发送 3.200+偏移",
            QLineEdit.Normal, str(cur))
        if not ok:
            return
        try:
            mv = int(float(text.strip()))
        except ValueError:
            QMessageBox.warning(self, "电压偏移", f"无效数字: {text!r}")
            return
        if not -10000 <= mv <= 10000:
            QMessageBox.warning(self, "电压偏移", "偏移须在 -10000 ~ +10000 mV 之间。")
            return
        off = boff.load_offsets()
        off[ch] = mv
        if not boff.save_offsets(off):
            QMessageBox.critical(self, "电压偏移", f"保存失败: {boff.INI_PATH}")
            return
        self.cards[ch].set_offset(mv)
        self.log(f"通道 {ch} 电压偏移已设为 {mv:+d} mV(设 3.200V 实际发 "
                 f"{3.200 + mv / 1000.0:.4f}V)。", "INFO")

    def select_all(self):
        self.selected = set(range(CH_MIN, CH_MAX + 1))
        for c in self.cards.values():
            c.set_selected(True)
        self._refresh_sel_state()

    def invert_selection(self):
        self.selected = set(range(CH_MIN, CH_MAX + 1)) - self.selected
        for ch, c in self.cards.items():
            c.set_selected(ch in self.selected)
        self._refresh_sel_state()

    def clear_selection(self):
        self.selected.clear()
        for c in self.cards.values():
            c.set_selected(False)
        self._refresh_sel_state()

    def _sel_list_text(self) -> str:
        if not self.selected:
            return ""
        chs = sorted(self.selected)
        if len(chs) > 12:
            return f"{chs[0]} … {chs[-1]} 等 {len(chs)} 个"
        return ", ".join(str(c) for c in chs)

    def _refresh_sel_state(self) -> None:
        if self.selected:
            self.lbl_sel.setText(f"已选 {len(self.selected)} 通道: {self._sel_list_text()}")
        else:
            self.lbl_sel.setText("未选中通道(点击上方卡片多选)")
        self._soc_curve_refresh()      # 选中变化 → 曲线工作点高亮跟随

    # ---------------- SOC-电压曲线预览
    def _soc_curve_refresh(self, *_args) -> None:
        """刷新「SOC 曲线」页:配置的 OCV 曲线 + 各运行通道的实时工作点。

        与模拟使用同一条曲线(曲线+最高/最低电压),因此看到的端点就是 0%/100%
        实际下发位置;工作点 = (当前 SOC, 该 SOC 对应电压)。
        """
        w = getattr(self, "w_curve", None)
        if w is None:
            return
        chem = self.cb_chem.currentText()
        base = bsoc.CURVES.get(chem) or []
        curve = self._soc_curve()
        pts = curve or base
        if not pts:
            w.set_curve([], 0.0, 1.0, "", (), "无可用曲线")
            return
        vmin, vmax = pts[0][1], pts[-1][1]
        title = f"{chem} · 0%={vmin:.2f}V · 100%={vmax:.2f}V"
        points = [(ch, sim.soc, sim.voltage(), ch in self.selected)
                  for ch, sim in sorted(self._soc_sims.items())]
        if curve is None:
            hint = "最高/最低电压无效,按默认端点显示"
        elif self._soc_sims:
            hint = f"运行 {len(self._soc_sims)} 通道"
        else:
            hint = "未运行(仅预览曲线)"
        w.set_curve(pts, vmin, vmax, title, points, hint)

    # ---------------- 动作
    def toggle_bus(self) -> None:
        """打开/关闭总线一体:按当前总线状态自动切换。"""
        if self.bus_open:
            self.close_bus()
        else:
            self.open_bus()

    def toggle_backend(self) -> None:
        """启动/停止后端一体:按后端连接状态自动切换。"""
        if self.svc is not None and self.svc._alive:
            self.stop_backend()
        else:
            self.start_backend()

    def open_bus(self) -> None:
        vendor = self.cb_vendor.currentText().strip()
        channel = self.ed_channel.currentText().strip()
        bitrate = BAUD_MAP.get(self.cb_baud.currentText().strip())
        if not bitrate:
            QMessageBox.warning(self, "程控模拟电池", "请选择波特率(默认 100 kHz)。")
            return

        def done(r: dict) -> None:
            if r.get("ok"):
                self.log(f"总线会话已打开: {vendor}:{channel} @ {fmt_bitrate(bitrate)} · "
                         f"开始轮询 24 通道。", "OK")
            else:
                self.log(r.get("error", "打开失败"), "ERR")
                QMessageBox.critical(self, "程控模拟电池", r.get("error", "打开失败"))

        self._submit({"cmd": "open", "vendor": vendor, "channel": channel,
                      "bitrate": bitrate}, done)

    def close_bus(self) -> None:
        self._submit({"cmd": "close"},
                     lambda r: self.log("总线会话已请求关闭" if r.get("ok")
                                        else r.get("error", "关闭失败"),
                                        "OK" if r.get("ok") else "ERR"))

    def _parse_v(self, raw: str) -> float | None:
        try:
            v = float(raw)
            if 0 <= v <= V_MAX:
                return v
        except ValueError:
            pass
        QMessageBox.warning(self, "程控模拟电池",
                            f"电压必须是 0~{V_MAX:g} 的数字(V),例如 3.2。")
        return None

    def _parse_a(self, raw: str) -> float | None:
        try:
            a = float(raw)
            if 0 <= a <= A_MAX:
                return a
        except ValueError:
            pass
        QMessageBox.warning(self, "程控模拟电池",
                            f"电流必须是 0~{A_MAX:g} 的数字(A),例如 1.0。")
        return None

    def _selected_sorted(self) -> list[int] | None:
        if not self.selected:
            QMessageBox.information(self, "程控模拟电池",
                                    "尚未选中通道:请先点击卡片选择(可多选)。")
            return None
        return sorted(self.selected)

    def set_output(self) -> None:
        if not self.bus_open:
            QMessageBox.warning(self, "程控模拟电池", "请先打开总线会话。")
            return
        chs = self._selected_sorted()
        if chs is None:
            return
        volts = self._parse_v(self.ed_volts.text().strip())
        if volts is None:
            return
        amps = self._parse_a(self.ed_amps.text().strip())
        if amps is None:
            return
        self._batch(chs, {"cmd": "set", "chs": chs, "volts": volts, "amps": amps},
                    f"设定 {volts:.3f} V / {amps:.3f} A")

    def set_relay(self, on: bool) -> None:
        if not self.bus_open:
            QMessageBox.warning(self, "程控模拟电池", "请先打开总线会话。")
            return
        chs = self._selected_sorted()
        if chs is None:
            return
        self._batch(chs, {"cmd": "relay", "chs": chs, "on": on},
                    f"继电器{'闭合' if on else '断开'}")

    def _batch(self, chs: list[int], req: dict, what: str) -> None:
        """批量控制:单次请求携带 chs 列表,后端逐通道下发、一次回读同步——
        比逐通道单独请求快一个数量级。单条线程内完成,不阻塞 GUI。"""
        if not self._require_svc() or self.busy:
            return
        self._set_busy(True)
        self.log(f"批量下发: {what} → 通道 {self._sel_list_text()}", "INFO")

        def work():
            try:
                return self.svc.request(req, timeout=30.0)
            except Exception as ex:
                return {"ok": False, "error": str(ex)}

        def work_compat():
            """先尝试 chs 批量(新版后端);老后端不认识 chs 时退化为逐通道顺序下发。"""
            r = work()
            err = str(r.get("error", ""))
            if r.get("ok") or r.get("chs") is not None or "ch" not in err:
                return r
            # 老后端(无 chs 支持)回退:逐通道,速度较慢但保证可用
            oks, fails = [], []
            for ch in chs:
                single = dict(req)
                single.pop("chs", None)
                single["ch"] = ch
                try:
                    sr = self.svc.request(single, timeout=15.0)
                except Exception as ex:
                    sr = {"ok": False, "error": str(ex)}
                if sr.get("ok"):
                    oks.append(ch)
                else:
                    fails.append({"ch": ch, "error": sr.get("error", "?")})
            if not oks and fails:
                return {"ok": False, "error": fails[0]["error"], "fails": fails}
            return {"ok": not fails, "chs": oks, "fails": fails, "legacy": True}

        def done(r):
            self._set_busy(False)
            if r.get("legacy"):
                self.log("当前后端为旧版(无批量接口),已逐通道下发;重启后端可提速。", "WARN")
            fails = r.get("fails") or []
            n_ok = len(chs) - len(fails)
            if not fails and not r.get("chs"):
                # 整批未执行(校验失败 / 通信异常)
                self.log(r.get("error", "下发失败"), "ERR")
                return
            if n_ok:
                self.log(f"{what} 成功 {n_ok}/{len(chs)} 通道。",
                         "OK" if not fails else "WARN")
            for f in fails:
                self.log(f"CH{f.get('ch')} {what} 失败: {f.get('error', '?')}", "ERR")

        self._run_thread(work_compat, done)

    # ---------------- 后端服务启停
    def start_backend(self) -> None:
        if self.busy:
            return
        host, tcp_port = self._parse_addr()
        if host is None:
            return
        self._set_busy(True)
        self.btn_backend.setEnabled(False)
        self.btn_backend.setText("启动中…")
        self._set_svc_label("⏳ 正在启动后端…", ui.C_INFO)

        def work():
            addr = ensure_server(host, tcp_port)
            return {"ok": True, "addr": addr}

        def done(r):
            self._set_busy(False)
            self._update_svc_buttons()
            addr = r.get("addr")
            if addr:
                self._set_svc_label(f"● 后端已就绪 {addr[0]}:{addr[1]}(点「连接」接入)", ui.C_OK)
                self.log(f"后端服务已就绪 {addr[0]}:{addr[1]}。", "OK")
            else:
                err = r.get("error", "启动失败")
                self._set_svc_label(f"✗ 启动失败: {err}", ui.C_ERR)
                self.log(f"启动后端失败: {err}", "ERR")

        self._run_thread(work, done)

    def stop_backend(self) -> None:
        if self.busy:
            return
        host, tcp_port = self._parse_addr()
        if host is None:
            return
        connected = self.svc is not None and self.svc._alive
        if not connected and not probe(host, tcp_port):
            QMessageBox.information(self, "程控模拟电池", "后端当前未在运行。")
            return
        if QMessageBox.question(
                self, "停止后端",
                "停止后端会同时关闭总线会话,并使所有前端(含 AI/MCP)离线。\n确定停止?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._set_busy(True)

        def work():
            cli = None
            try:
                if connected:
                    self.svc.request({"cmd": "shutdown"})
                else:
                    cli = ServiceClient("gui-stop", host, tcp_port)
                    cli.request({"cmd": "shutdown"})
            finally:
                if cli is not None:
                    cli.close()
            return {"ok": True}

        def done(r):
            self._set_busy(False)
            if r.get("ok"):
                self.log("已向后端发送停止指令,等待其退出…", "WARN")
                QTimer.singleShot(3000, self._check_backend_gone)
            else:
                self.log(r.get("error", "停止失败"), "ERR")

        self._run_thread(work, done)

    def _check_backend_gone(self) -> None:
        host, tcp_port = self._parse_addr(quiet=True)
        if host is not None and probe(host, tcp_port):
            self.log("⚠ 后端仍在线(可能被其他前端重新拉起),如需要可再次点击「停止后端」。",
                     "WARN")

    def closeEvent(self, ev) -> None:
        # 1) 先停定时器,避免关闭过程中 timer 回调再触新操作
        self._drain_timer.stop()
        self._watch_timer.stop()
        self._spark_timer.stop()
        # 2) 短暂等待在途 worker(socket 已就绪可被关,让其快速退出),
        #    避免窗口销毁后 worker 还向已删除的 _Bridge 发信号
        if self.svc is not None:
            try:
                self.svc.close()          # 令在途 request 立即抛错退出
            except Exception:
                pass
        deadline = time.monotonic() + 1.0
        for t in list(self._workers):
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        # 3) 标记 bridge 已随窗口销毁(worker_safe 据此跳过 emit)
        self._bridge = None
        super().closeEvent(ev)


def main() -> None:
    app = QApplication(sys.argv)
    win = BatteryWindow()
    win.rebuild_backgrounds()

    if "--shot" in sys.argv:
        idx = sys.argv.index("--shot")
        out = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "gui_shot.png"
        # 演示数据:便于离屏核对卡片布局
        demo = []
        for ch in range(CH_MIN, CH_MAX + 1):
            demo.append({"ch": ch, "online": ch <= 20,
                         "volts": 3.0 + ch * 0.05, "amps": (ch % 5) * 0.4 - 0.8,
                         "relay": ch % 3 == 0, "temp": 25 + ch})
        win._apply_channels(demo)
        win.selected = {1, 2, 3}
        for ch in (1, 2, 3):
            win.cards[ch].set_selected(True)
        win._refresh_sel_state()
        win.log("界面启动,等待接入后端…", "INFO")
        win.log("[TX·cli] CH3 设电压 3.100 V", "TX")
        win.log("CH1..3 设定成功 3/3 通道。", "OK")
        win.log("CH7 回读超时,已重试。", "WARN")
        win.log("总线会话异常断开。", "ERR")
        win.show()
        app.processEvents()
        pm = win.grab()
        pm.save(out)
        print("shot saved ->", out)
        return

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
