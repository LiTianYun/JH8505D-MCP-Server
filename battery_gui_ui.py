#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程控模拟电池控制台 · UI 设计层(自定义控件 + 设计 Token + QSS)。

界面结构在 battery_gui.ui(Qt Designer 标准文件,可可视化编辑);
本文件承载 .ui 表达不了的部分:
- 设计 Token:颜色 / 玻璃三档 / 圆角 / 字体回退链(规范 §1,唯一事实源)
- 全局 QSS:由 qss_text() 生成,运行时应用到 .ui 根控件
  (控件样式用 kind 动态属性选择器,如 QPushButton[kind="primary"],
   在 Qt Designer 里选中控件 → 属性编辑器 + 号添加字符串属性 kind)
- 自绘控件(在 .ui 中以 promoted custom widget 引用):
    GlassPanel    玻璃卡(strong/mid/light 三档,.ui 里用字符串属性 grade 指定)
    FeaturedPanel 深色锚点卡(全页唯一实色块)
    ChannelCard   通道卡(.ui 里用数字属性 ch 指定通道号)
    SocCurve      OCV-SOC 曲线预览(配置窗口 + 各通道实时工作点)
- render_backgrounds():方向性光源背景 + 静态色斑 + 高斯近似磨砂底图(规范路线 A)

视觉契约:《UI设计风格规范_毛玻璃拟态.md》
  - 冷调蓝白底 + 唯一品牌蓝 #4D6BFE,禁止蓝紫粉多彩
  - 玻璃四要素缺一不可:半透明填充 / 磨砂底 / 亮描边 / 顶部内高光
  - 默认静止,无常驻动画

注意:QUiLoader 以单参 parent 构造自定义控件,故 grade/ch 均走 Qt 动态属性,
不要在构造函数里加必填参数。

业务逻辑(后端连接 / 事件泵 / 批量控制)在 battery_gui.py。
"""
from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, Signal
from PySide6.QtGui import (QColor, QPainter, QPainterPath, QPen, QFont,
                           QFontDatabase, QLinearGradient, QRadialGradient,
                           QPixmap, QBrush)
from PySide6.QtWidgets import (QWidget, QLabel, QHBoxLayout, QVBoxLayout,
                               QGridLayout, QGraphicsDropShadowEffect,
                               QSizePolicy)

# ================================================================ 设计 Token(唯一事实源)
# ---- 月球银 Lunar Light(浅色基准) + 量子蓝 accent ----
# 主背景:雾蓝白浅底;径向渐变 右上较亮 → 左下微带蓝灰
C_BG_STOPS = ("#F4F8FC", "#EEF2F7", "#DCE6F2", "#CDDDF0")     # 浅雾蓝白渐变
# 静态色斑(色, cx, cy, r, α):浅底需更高 α 才可见 —— 对齐 #s-lunar .55/.45/.30
C_ORBS = (("#93C5FD", 0.10, 1.02, 0.40, 120),   # 主光斑 浅蓝(雾蓝)
          ("#C4B5FD", 0.88, 0.95, 0.32, 95),    # 次光斑 浅紫
          ("#A5B4FC", 0.45, 0.50, 0.28, 70))    # 辅助光斑 蓝粉
C_ACCENT = "#4D6BFE"          # 主色:按钮/选中/强调(量子蓝,略深于 lunar 以适配浅底)
C_ACCENT_H = "#6B85FF"        # hover 提亮
C_ACCENT_P = "#3E57DB"        # pressed 压暗
C_TXT = "#1E293B"             # 主文本(浅底深字)
C_SUB = "#64748B"             # 次文本/说明
C_BG_ALT = "#EEF2F7"          # 深一档背景(侧栏/底栏/卡片实底,浅灰蓝)
# 卡片实底块(浅底区分靠白玻璃 + 灰描边)
C_FEAT_BG = "#F8FAFC"
C_FEAT_TXT = "#0F172A"
C_FEAT_SUB = "rgba(15,23,42,0.72)"
# ---- 语义色(浅底用深系,对齐 lunar) ----
C_OK = "#16A34A"              # 成功/在线
C_WARN = "#D97706"            # 警告
C_ERR = "#DC2626"             # 错误/异常
C_INFO = "#0284C7"            # 信息
C_TX = "#2563EB"              # 总线 TX(链接蓝)
# ---- 玻璃三档:浅色玻璃(月亮银 glass 近透明白 .45 / 白描边 .70) ----
# 浅底玻璃要更高白度 + 白描边(规范 lunar);填充用白高 α 模拟近透明磨砂
GLASS_FILL = {"strong": 0.52, "mid": 0.42, "light": 0.34}
# 描边:白 high α(浅底描边要纯白才显形)
GLASS_BORDER = {"strong": 0.70, "mid": 0.60, "light": 0.50}
GLASS_RADIUS = {"strong": 20, "mid": 16, "light": 12}
# 玻璃填充色 = 白 + 高 α(在浅底上呈近透明白玻璃)
GLASS_RGB = (255, 255, 255)
# 顶部内高光 α(浅底玻璃高光更要强;A .35 以上 → 160/255)
GLASS_HL_A = 160
# ---- 通道卡(3 行内容:编号/电压+校准/电流温度继电器)
CARD_MIN_W, CARD_MIN_H = 136, 78
CARD_RADIUS = 16
# ---- 字体回退链(serif 标题/数字 + sans 正文配对)
SERIF = ["Noto Serif CJK SC", "Source Han Serif SC", "Source Han Serif CN", "SimSun", "宋体"]
SANS = ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "微软雅黑"]
MONO = ["Cascadia Mono", "Consolas", "Courier New"]
# ---- 阴影(浅色主题:灰投影,别太黑)
SHADOW_RGB = (55, 75, 110)


# ================================================================ 工具函数
def has_font(family: str) -> bool:
    return family.lower() in {f.lower() for f in QFontDatabase.families()}


def make_font(candidates, px, weight=QFont.Normal, italic=False) -> QFont:
    """按回退链挑字体:显示(衬线) / 正文(无衬线) / 等宽 分开传候选。"""
    for fam in candidates:
        if has_font(fam):
            f = QFont(fam)
            f.setPixelSize(px)
            f.setWeight(weight)
            f.setItalic(italic)
            return f
    f = QFont()
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setItalic(italic)
    return f


def add_shadow(widget, blur=22, dy=8, alpha=60):
    """玻璃投影(规范 shadow-glass token)。"""
    e = QGraphicsDropShadowEffect(widget)
    e.setBlurRadius(blur)
    e.setOffset(0, dy)
    e.setColor(QColor(*SHADOW_RGB, alpha))
    widget.setGraphicsEffect(e)
    return e


def add_accent_glow(widget, alpha=46, blur=16, dy=0):
    """按钮同色光晕(量子蓝:accent 实底 A1.0 + 主色光晕 A.18≈46)。
    等价 HTML .btn 的 box-shadow: 0 6px 18px var(--chip)。"""
    e = QGraphicsDropShadowEffect(widget)
    e.setBlurRadius(blur)
    e.setOffset(0, dy)
    e.setColor(QColor(77, 107, 254, alpha))
    widget.setGraphicsEffect(e)
    return e


def add_semantic_glow(widget, rgb, alpha=42, blur=15, dy=0):
    """按语义色光晕(如绿/红按钮的同色发光)。rgb=(r,g,b)。"""
    e = QGraphicsDropShadowEffect(widget)
    e.setBlurRadius(blur)
    e.setOffset(0, dy)
    e.setColor(QColor(*rgb, alpha))
    widget.setGraphicsEffect(e)
    return e


def text_label(text, px=12, color=C_SUB, weight=QFont.Normal,
               family=SANS, wrap=False) -> QLabel:
    """玻璃上的文本标签:透明底 + 语义色(供代码内构建的控件用)。"""
    l = QLabel(text)
    l.setFont(make_font(family, px, weight))
    l.setStyleSheet(f"color: {color}; background: transparent;")
    if wrap:
        l.setWordWrap(True)
    return l


# ================================================================ 全局 QSS
def qss_text() -> str:
    """全局样式(运行时由 battery_gui.py 应用到 .ui 根控件)。

    用 kind 动态属性选择器而非 objectName,保证 .ui 里 objectName 唯一(供 findChild)。
    token 用 @@name@@ 占位再 replace,避免与 QSS 花括号冲突。
    """
    s = """
        QWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; }

        /* 标签:浅底深字 */
        QLabel { background: transparent; border: none; color: @@txt@@; }
        QLabel[kind="sub"]     { color: @@sub@@; }
        QLabel[kind="feat"]    { color: @@feat_txt@@; }
        QLabel[kind="featSub"] { color: @@feat_sub@@; }

        QPushButton { font-size: 13px; }

        /* 主按钮(primary · accent 实底 · 白字) */
        QPushButton[kind="primary"] {
            background-color: @@accent@@; color: #FFFFFF; border: none;
            border-radius: 12px; padding: 8px 18px; font-weight: 600;
        }
        QPushButton[kind="primary"]:hover   { background-color: @@accent_h@@; }
        QPushButton[kind="primary"]:pressed { background-color: @@accent_p@@; }
        QPushButton[kind="primary"]:disabled { background-color: rgba(77,107,254,0.35); color: rgba(255,255,255,0.8); }

        /* 次级按钮(ghost · 白玻璃底) */
        QPushButton[kind="ghost"] {
            background-color: rgba(255,255,255,0.55); color: @@txt@@;
            border: 1px solid rgba(255,255,255,0.8); border-radius: 12px; padding: 8px 16px;
        }
        QPushButton[kind="ghost"]:hover   { background-color: rgba(255,255,255,0.85); }
        QPushButton[kind="ghost"]:pressed { background-color: rgba(255,255,255,0.45); }
        QPushButton[kind="ghost"]:disabled { color: rgba(100,116,139,0.5); }

        /* 浅色实底按钮(solid · 白玻璃,移入控制卡的全选等) */
        QPushButton[kind="solid"] {
            background-color: rgba(255,255,255,0.60); color: @@txt@@;
            border: 1px solid rgba(100,116,139,0.25); border-radius: 10px;
            padding: 4px 12px; font-size: 12px; font-weight: 600;
        }
        QPushButton[kind="solid"]:hover   { background-color: rgba(77,107,254,0.14); border: 1px solid @@accent@@; }
        QPushButton[kind="solid"]:pressed { background-color: rgba(77,107,254,0.22); }
        QPushButton[kind="solid"]:disabled { color: rgba(100,116,139,0.5); background-color: rgba(255,255,255,0.4); }

        /* 控制卡(白实底)内按钮:accent 实底主行动(白字) + 白玻璃幽灵 */
        QPushButton[kind="dark"] {
            background-color: @@accent@@; color: #FFFFFF; border: none;
            border-radius: 11px; padding: 8px 16px; font-weight: 700;
        }
        QPushButton[kind="dark"]:hover { background-color: @@accent_h@@; }
        QPushButton[kind="dark"]:disabled { background-color: rgba(77,107,254,0.35); color: rgba(255,255,255,0.8); }
        QPushButton[kind="darkGhost"] {
            background-color: rgba(255,255,255,0.55); color: @@txt@@;
            border: 1px solid rgba(100,116,139,0.25); border-radius: 11px; padding: 8px 14px;
        }
        QPushButton[kind="darkGhost"]:hover { background-color: rgba(77,107,254,0.12); border: 1px solid @@accent@@; }
        QPushButton[kind="darkGhost"]:disabled { color: rgba(100,116,139,0.5); }

        /* 语义按钮(浅底用深系色 + 深字保证对比) */
        QPushButton[kind="success"] {
            background-color: @@ok@@; color: #FFFFFF; border: none;
            border-radius: 11px; padding: 6px 14px; font-size: 12px; font-weight: 700;
        }
        QPushButton[kind="success"]:hover { background-color: #15803D; }
        QPushButton[kind="success"]:disabled { background-color: rgba(22,163,74,0.4); color: rgba(255,255,255,0.85); }
        QPushButton[kind="danger"] {
            background-color: @@err@@; color: #FFFFFF; border: none;
            border-radius: 11px; padding: 6px 14px; font-size: 12px; font-weight: 700;
        }
        QPushButton[kind="danger"]:hover { background-color: #B91C1C; }
        QPushButton[kind="danger"]:disabled { background-color: rgba(220,38,38,0.4); color: rgba(255,255,255,0.85); }
        QPushButton[kind="warning"] {
            background-color: @@warn@@; color: #451A03; border: none;
            border-radius: 11px; padding: 6px 14px; font-size: 12px; font-weight: 700;
        }
        QPushButton[kind="warning"]:hover { background-color: #B45309; color: #FFFFFF; }

        /* 输入框 / 下拉:白玻璃底 */
        QLineEdit, QComboBox {
            background-color: rgba(255,255,255,0.65); color: @@txt@@;
            border: 1px solid rgba(100,116,139,0.30); border-radius: 10px;
            padding: 6px 10px; selection-background-color: @@accent@@; selection-color: white;
        }
        QLineEdit:focus, QComboBox:focus { border: 1px solid @@accent@@; }
        QComboBox::drop-down { border: none; width: 22px; }
        QComboBox QAbstractItemView {
            background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 8px;
            color: #0F172A;
            selection-background-color: @@accent@@; selection-color: white;
        }
        QLineEdit[kind="dark"] {
            background-color: rgba(255,255,255,0.65); color: @@txt@@;
            border: 1px solid rgba(100,116,139,0.30); border-radius: 10px; padding: 6px 10px;
        }
        QLineEdit[kind="dark"]:focus { border: 1px solid @@accent_h@@; }

        /* 日志区(透明底等宽字) */
        QPlainTextEdit {
            background-color: transparent; color: @@txt@@; border: none;
            font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px;
        }

        /* 日志/曲线标签页(玻璃底上的胶囊页签) */
        QTabWidget::pane { border: none; background: transparent; }
        QTabBar::tab {
            background-color: rgba(255,255,255,0.50); color: @@sub@@;
            border: 1px solid rgba(100,116,139,0.22); border-radius: 9px;
            padding: 3px 12px; margin-right: 6px; font-size: 12px;
        }
        QTabBar::tab:hover { background-color: rgba(77,107,254,0.10); }
        QTabBar::tab:selected {
            background-color: rgba(77,107,254,0.14); color: @@accent@@;
            border: 1px solid @@accent@@; font-weight: 600;
        }

        /* 滚动条极简(浅色) */
        QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
        QScrollBar::handle:vertical { background: rgba(100,116,139,0.35); border-radius: 4px; min-height: 30px; }
        QScrollBar::handle:vertical:hover { background: rgba(100,116,139,0.55); }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

        QToolTip { background: #0F172A; color: #F1F5F9; border: none; padding: 6px 8px; }
    """
    for k, v in {"accent": C_ACCENT, "accent_h": C_ACCENT_H, "accent_p": C_ACCENT_P,
                 "txt": C_TXT, "sub": C_SUB, "feat_txt": C_FEAT_TXT,
                 "feat_sub": C_FEAT_SUB, "ok": C_OK, "err": C_ERR,
                 "warn": C_WARN}.items():
        s = s.replace("@@%s@@" % k, v)
    return s


# ================================================================ 背景渲染(规范路线 A)
def render_backgrounds(w: int, h: int) -> tuple[QPixmap, QPixmap]:
    """背景 = 方向性光源渐变 + 静态色斑。返回 (清晰底图, 磨砂底图)。

    磨砂底图供 GlassPanel 切片对齐(窗口坐标系),resize 时由主窗口重建一次。
    """
    clear = QPixmap(w, h)
    clear.fill(Qt.transparent)
    p = QPainter(clear)
    p.setRenderHint(QPainter.Antialiasing)

    # 主光:右上亮 → 左下带色(方向性明暗,玻璃放上去才有折射感)
    g = QRadialGradient(w * 0.98, h * 0.02, max(w, h) * 1.15)
    for pos, col in zip((0.0, 0.5, 0.85, 1.0), C_BG_STOPS):
        g.setColorAt(pos, QColor(col))
    p.fillRect(QRect(0, 0, w, h), g)

    # 静态色斑(径向渐变天生柔和 → 透出磨砂即柔光场)。禁止动画常驻。
    for color, fx, fy, fr, fa in C_ORBS:
        c0 = QColor(color); c0.setAlpha(0)
        c1 = QColor(color); c1.setAlpha(fa)
        og = QRadialGradient(w * fx, h * fy, w * fr)
        og.setColorAt(0.0, c1)
        og.setColorAt(1.0, c0)
        p.setPen(Qt.NoPen)
        p.setBrush(og)
        p.drawEllipse(QPointF(w * fx, h * fy), w * fr, w * fr)
    p.end()

    # 高斯近似模糊(缩 1/8 再平滑放大)→ 磨砂底图
    blurred = clear.scaled(max(1, w // 8), max(1, h // 8),
                           Qt.KeepAspectRatio, Qt.SmoothTransformation)
    blurred = blurred.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return clear, blurred


# ================================================================ 玻璃面板
class GlassPanel(QWidget):
    """玻璃卡:磨砂底切片 + 半透明填充 + 1px 亮描边 + 顶部内高光。

    grade ∈ {strong, mid, light}:在 .ui 里用字符串动态属性 grade 指定(默认 mid)。
    内容在 .ui 中直接给该控件挂 layout;主窗口须持有 _blur_bg(render_backgrounds 产物)。
    """

    def __init__(self, parent=None, grade="mid"):
        super().__init__(parent)
        self._grade = grade

    @property
    def grade(self) -> str:
        g = self.property("grade")          # .ui 动态属性优先
        return g if g in GLASS_FILL else self._grade

    @property
    def radius(self) -> int:
        return GLASS_RADIUS[self.grade]

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        path = QPainterPath()
        path.addRoundedRect(rect, self.radius, self.radius)

        # ② 磨砂底:主窗口维护的整窗模糊背景切片(坐标对齐)
        win = self.window()
        blur = getattr(win, "_blur_bg", None)
        if blur is not None and not blur.isNull():
            origin = self.mapTo(win, QPoint(0, 0))
            p.save()
            p.setClipPath(path)
            p.drawPixmap(self.rect(), blur, QRect(origin, self.size()))
            p.restore()

        # ① 半透明填充(三档 α;量子蓝 glass = 白低 α 叠深底形成玻璃)
        p.fillPath(path, QColor(*GLASS_RGB, round(255 * GLASS_FILL[self.grade])))
        # ③ 亮描边(stroke)
        p.setPen(QPen(QColor(255, 255, 255, round(255 * GLASS_BORDER[self.grade])), 1))
        p.drawPath(path)
        # ④ 顶部内高光(光在玻璃上缘的反射 · 最重要细节)
        hl_h = min(self.radius * 0.6, self.height() * 0.35)
        if hl_h > 1:
            hl = QLinearGradient(0, 0, 0, hl_h)
            hl.setColorAt(0.0, QColor(255, 255, 255, GLASS_HL_A))
            hl.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.save()
            p.setClipPath(path)
            p.fillRect(QRectF(0, 0, self.width(), hl_h), hl)
            p.restore()


class FeaturedPanel(QWidget):
    """深色锚点卡:全页唯一实色块(打破玻璃群,承载主操作区)。"""
    radius = 20

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, self.radius, self.radius)
        p.fillPath(path, QColor(C_FEAT_BG))
        p.setPen(QPen(QColor(100, 116, 139, 90), 1))     # 浅底:灰蓝描边
        p.drawPath(path)
        # 卡上缘受光
        hl = QLinearGradient(0, 0, 0, self.radius * 0.7)
        hl.setColorAt(0.0, QColor(255, 255, 255, 160))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.save()
        p.setClipPath(path)
        p.fillRect(QRectF(0, 0, self.width(), self.radius * 0.7), hl)
        p.restore()


# ================================================================ 卡片迷你趋势图
class SparkLine(QWidget):
    """卡片内迷你趋势图(纯展示):最近 WINDOW 秒的电压(蓝)/电流(橙)两条折线。

    - Y 刻度: 支持"全局同步刻度"(全体通道共用一套 min~max, 由主窗口周期
      set_global_range() 同步);未设全局时回退本卡自身自动(该卡窗口 min~max)。
      电压/电流同用一套刻度 → 各卡、各量纲纵向位置完全可比。
    - 平滑: 抗锯齿 + 单条 QPainterPath 连线。
    - 离线/无数据画"…"占位;断点(None)不连线。
    数据入口 push(v, a):原始 V/A;set_global_range(lo, hi):外部同步刻度。
    """

    WINDOW = 60                 # 显示最近 60 秒
    C_V = QColor("#4D6BFE")     # 电压(品牌蓝)
    C_A = QColor("#E4572E")     # 电流(橙)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(26)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._t: deque = deque()
        self._v: deque = deque()
        self._a: deque = deque()
        self._t0: float | None = None
        self._glo: float | None = None      # 全局刻度(外部同步)
        self._ghi: float | None = None

    def set_global_range(self, lo, hi) -> None:
        # 等值检查:范围未变时不触发重绘(G3/G9 防 100ms 无谓 update)
        if self._glo != lo or self._ghi != hi:
            self._glo, self._ghi = lo, hi
            self.update()

    def clear_global_range(self):
        if self._glo is not None or self._ghi is not None:
            self._glo = self._ghi = None
            self.update()

    def push(self, v, a) -> None:
        t = time.time()
        if self._t0 is None:
            self._t0 = t
        self._t.append(t)
        self._v.append(v)          # 原始电压 V(可 None)
        self._a.append(a)          # 原始电流 A(可 None)
        # 只保留 WINDOW 秒
        while self._t and t - self._t[0] > self.WINDOW:
            self._t.popleft(); self._v.popleft(); self._a.popleft()
        self.update()

    def clear(self):
        self._t.clear(); self._v.clear(); self._a.clear()
        self._t0 = None
        self.clear_global_range()
        self.update()

    def _xmap(self, t):
        """时间 -> 控件 x(左=最旧, 右=最新)。"""
        t0 = self._t[0] if self._t else 0.0
        t1 = self._t[-1] if self._t else 1.0
        if t1 - t0 < 1e-6:
            return float(self.width())
        return (t - t0) / (t1 - t0) * self.width()

    def _draw_series(self, p, ws, xs, color, width, lo, hi):
        """用给定 (lo,hi) 画一条折线;断点 None 断开。lo<hi 由调用方保证。"""
        rng = max(hi - lo, 1e-6)
        p.setPen(QPen(color, width))
        seg = []
        for x, v in zip(xs, ws):
            if v is None:
                if len(seg) >= 2:
                    path = QPainterPath()
                    path.moveTo(*seg[0])
                    for pt in seg[1:]:
                        path.lineTo(*pt)
                    p.drawPath(path)
                seg = []
                continue
            y = self.height() - 3 - (v - lo) / rng * (self.height() - 6)
            seg.append((x, y))
        if len(seg) >= 2:
            path = QPainterPath()
            path.moveTo(*seg[0])
            for pt in seg[1:]:
                path.lineTo(*pt)
            p.drawPath(path)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.height() < 8 or not self._t:
            p.setPen(QPen(QColor("#B6C2D1"), 1))
            f = p.font(); f.setPixelSize(9)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "…")
            return
        # Y 刻度:优先全局同步刻度;否则本卡(电压+电流)所有值取 min~max
        if self._glo is not None and self._ghi is not None and self._ghi > self._glo:
            lo, hi = self._glo, self._ghi
        else:
            valid = [x for x in self._v if x is not None] + \
                    [x for x in self._a if x is not None]
            if not valid:
                return
            lo, hi = min(valid), max(valid)
            if hi - lo < 1e-6:               # 平线:上下留一点边距
                hi = lo + 0.1
                lo = lo - 0.1
        xs = [self._xmap(t) for t in self._t]
        self._draw_series(p, self._a, xs, self.C_A, 1.2, lo, hi)
        self._draw_series(p, self._v, xs, self.C_V, 1.6, lo, hi)


# ================================================================ SOC-电压曲线预览
class SocCurve(QWidget):
    """充放电模拟的 OCV-SOC 曲线预览(纯 QPainter,无额外依赖)。

    画什么:
    - X 轴 SOC 0~100%,Y 轴电压(取当前配置的最高/最低电压窗口,上下留 8% 边距);
    - 曲线 = 面板「曲线 + 最高/最低电压」缩放后的 OCV 曲线(端点即 0%/100%);
    - 端点标记 0% / 100% 电压;
    - 各运行通道的实时工作点 (SOC, 电压):实心圆点 + 通道号,选中通道加白圈放大;
    - 未运行/无数据时只画曲线并提示。

    数据入口 set_curve(pts, vmin, vmax, title, points, hint),内容不变时不重绘。
    """

    C_LINE = QColor(C_ACCENT)
    C_FILL = QColor(C_ACCENT)
    C_GRID = QColor("#CBD5E1")
    C_AXIS = QColor("#94A3B8")
    C_PT = QColor("#0F172A")
    C_SEL = QColor(C_ACCENT)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pts: list[tuple[float, float]] = []
        self._vmin = 0.0
        self._vmax = 1.0
        self._title = ""
        self._points: list[tuple[int, float, float, bool]] = []
        self._hint = ""
        self._sig = None

    def set_curve(self, pts, vmin, vmax, title="", points=(), hint="") -> None:
        """更新曲线与工作点。内容未变则不重绘(1s 定时刷新不抖)。"""
        sig = (
            tuple((round(float(s), 3), round(float(v), 5)) for s, v in (pts or ())),
            round(float(vmin), 5), round(float(vmax), 5), title,
            tuple((int(c), round(float(s), 3), round(float(v), 5), bool(sel))
                  for c, s, v, sel in (points or ())),
            hint,
        )
        if sig == self._sig:
            return
        self._sig = sig
        self._pts = [(float(s), float(v)) for s, v in (pts or ())]
        self._vmin, self._vmax = float(vmin), float(vmax)
        self._title, self._hint = title, hint
        self._points = [(int(c), float(s), float(v), bool(sel))
                        for c, s, v, sel in (points or ())]
        self.update()

    # ---- 坐标映射
    def _plot_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(46, 24, -14, -26)

    @staticmethod
    def _x_of(soc: float, r: QRectF) -> float:
        s = max(0.0, min(100.0, soc))
        return r.left() + r.width() * s / 100.0

    @staticmethod
    def _y_of(v: float, r: QRectF, lo: float, hi: float) -> float:
        return r.bottom() - r.height() * (v - lo) / max(hi - lo, 1e-9)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._plot_rect()
        small = p.font(); small.setPixelSize(9)
        title_f = p.font(); title_f.setPixelSize(10); title_f.setBold(True)

        if not self._pts or self._vmax <= self._vmin:
            p.setFont(small)
            p.setPen(QPen(self.C_AXIS, 1))
            p.drawText(self.rect(), Qt.AlignCenter,
                       self._hint or "最高/最低电压无效,无法绘制曲线")
            return

        # Y 范围:窗口上下各留 8%
        pad = max(0.02, (self._vmax - self._vmin) * 0.08)
        lo, hi = self._vmin - pad, self._vmax + pad

        # 网格 + 刻度
        p.setFont(small)
        for i in range(5):
            frac = i / 4.0
            v = lo + (hi - lo) * frac
            y = self._y_of(v, r, lo, hi)
            p.setPen(QPen(self.C_GRID, 1, Qt.DashLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QPen(self.C_AXIS, 1))
            p.drawText(QRectF(0, y - 8, r.left() - 5, 16),
                       Qt.AlignRight | Qt.AlignVCenter, f"{v:.2f}")
        for soc in (0, 25, 50, 75, 100):
            x = self._x_of(soc, r)
            p.setPen(QPen(self.C_GRID, 1, Qt.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QPen(self.C_AXIS, 1))
            p.drawText(QRectF(x - 20, r.bottom() + 4, 40, 16),
                       Qt.AlignCenter, f"{soc}%")
        p.setPen(QPen(self.C_AXIS, 1))
        p.drawLine(r.bottomLeft(), r.bottomRight())

        # 曲线 + 曲线下淡填充
        pts = [(self._x_of(s, r), self._y_of(v, r, lo, hi)) for s, v in self._pts]
        path = QPainterPath()
        path.moveTo(*pts[0])
        for pt in pts[1:]:
            path.lineTo(*pt)
        fill = QPainterPath(path)
        fill.lineTo(r.right(), r.bottom())
        fill.lineTo(r.left(), r.bottom())
        fill.closeSubpath()
        grad = QLinearGradient(0, r.top(), 0, r.bottom())
        c0 = QColor(self.C_FILL); c0.setAlpha(58)
        c1 = QColor(self.C_FILL); c1.setAlpha(0)
        grad.setColorAt(0.0, c0)
        grad.setColorAt(1.0, c1)
        p.fillPath(fill, QBrush(grad))
        p.setPen(QPen(self.C_LINE, 2.0))
        p.drawPath(path)

        # 端点标记(0% / 100%)
        for soc, col in ((0, self._vmin), (100, self._vmax)):
            x, y = self._x_of(soc, r), self._y_of(col, r, lo, hi)
            p.setPen(Qt.NoPen)
            p.setBrush(self.C_LINE)
            p.drawEllipse(QPointF(x, y), 3.0, 3.0)
            p.setFont(small)
            p.setPen(QPen(self.C_AXIS, 1))
            tx = x + 6 if soc == 0 else x - 46
            p.drawText(QRectF(tx, y - 16, 44, 14),
                       Qt.AlignLeft if soc == 0 else Qt.AlignRight,
                       f"{col:.2f}V")

        # 工作点
        p.setFont(small)
        for ch, soc, volts, sel in self._points:
            x = self._x_of(soc, r)
            y = self._y_of(volts, r, lo, hi)
            # 工作点可能落在当前窗口之外(运行中改了曲线/窗口):贴边显示而非消失
            y = max(r.top() + 2.0, min(r.bottom() - 2.0, y))
            if sel:
                p.setPen(QPen(QColor(255, 255, 255, 235), 2))
                p.setBrush(self.C_SEL)
                p.drawEllipse(QPointF(x, y), 5.0, 5.0)
            else:
                p.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
                p.setBrush(self.C_PT)
                p.drawEllipse(QPointF(x, y), 3.4, 3.4)
            p.setPen(QPen(QColor(C_TXT), 1))
            p.drawText(QRectF(x + 6, y - 15, 40, 13), Qt.AlignLeft, f"{ch}")

        # 标题(曲线/窗口)+ 运行提示 + 图例
        p.setFont(title_f)
        p.setPen(QPen(QColor(C_TXT), 1))
        p.drawText(QRectF(r.left(), 2, r.width(), 18), Qt.AlignLeft, self._title)
        tw = p.fontMetrics().horizontalAdvance(self._title)
        p.setFont(small)
        if self._hint:
            p.setPen(QPen(QColor(C_SUB), 1))
            p.drawText(QRectF(r.left() + tw + 10, 2, 200, 18),
                       Qt.AlignLeft | Qt.AlignVCenter, self._hint)
        if self._points:
            fm = p.fontMetrics()
            x = r.right() - 4
            for color, label in ((self.C_PT, "其他"), (self.C_SEL, "选中")):
                wd = fm.horizontalAdvance(label)
                x -= wd
                p.setPen(QPen(QColor(C_SUB), 1))
                p.drawText(QRectF(x, 2, wd + 2, 18),
                           Qt.AlignLeft | Qt.AlignVCenter, label)
                x -= 10
                p.setPen(QPen(QColor(255, 255, 255, 220), 1.2))
                p.setBrush(color)
                p.drawEllipse(QPointF(x + 3, 11), 3.2, 3.2)
                x -= 10


# ================================================================ 通道卡片
class ChannelCard(GlassPanel):
    """单通道卡(mid 玻璃):点击切换选中(多选),双击请求回填设定值。

    在 .ui 中用数字动态属性 ch 指定通道号(1..24)。
    对外接口:
      signals: clicked(ch) / double_clicked(ch) / offset_requested(ch)  (右键设偏移)
      set_selected(on)        选中态(品牌蓝描边 + 淡蓝罩 + 右上角勾选圆点)
      set_state(row | None)   刷新显示;row = {online, volts, amps, relay, temp}
      set_offset(mv)          设置/清除该通道偏移(mV),小字显示于卡片
    """

    clicked = Signal(int)
    double_clicked = Signal(int)
    offset_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ch: int | None = None
        self.selected = False
        self._online = False
        self._relay_on = False
        self._glow_color: QColor | None = None
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(CARD_MIN_W, CARD_MIN_H)
        # 投影自持:按状态切换"灯圈"辉光——离线灰影 / 断开淡红 / 闭合淡绿(见 _apply_glow)
        self._fx = QGraphicsDropShadowEffect(self)
        self.setGraphicsEffect(self._fx)
        self._fx_state = None     # 当前阴影参数缓存(变化才重建)
        self._apply_glow()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 9)
        lay.setSpacing(2)

        # 行1: CH 编号(左) + 在线状态(卡片顶部正中,避开右上角选中勾选)
        top = QGridLayout()
        top.setSpacing(0)
        top.setContentsMargins(0, 0, 0, 0)
        self.lbl_ch = text_label("CH --", 14, C_TXT, QFont.DemiBold)
        top.addWidget(self.lbl_ch, 0, 0, Qt.AlignLeft)
        self.lbl_online = text_label("离线", 11)
        top.addWidget(self.lbl_online, 0, 1, Qt.AlignCenter)
        top.setColumnStretch(0, 1)      # 左右等宽拉伸 → 中列真正居中
        top.setColumnStretch(2, 1)
        lay.addLayout(top)

        # 行2: 电压(衬线大字)+ 校准小字(灰, 紧跟电压右侧)
        vrow = QHBoxLayout()
        vrow.setSpacing(4)
        self.lbl_volts = text_label("--", 22, C_TXT, QFont.DemiBold, SERIF)
        vrow.addWidget(self.lbl_volts)
        vrow.addWidget(text_label("V", 12, C_SUB, QFont.DemiBold), 0, Qt.AlignBottom)
        self.lbl_off = text_label("", 10, C_SUB)      # 校准 ±N mV(灰色小字)
        vrow.addWidget(self.lbl_off, 0, Qt.AlignBottom)
        self.lbl_soc = text_label("", 10, C_ACCENT, QFont.DemiBold)  # SOC/容量(模拟中)
        vrow.addWidget(self.lbl_soc, 0, Qt.AlignBottom)
        vrow.addStretch(1)
        lay.addLayout(vrow)
        self._offset_mv = 0

        # 行3: 电流 + 温度 + 继电器
        bot = QHBoxLayout()
        bot.setSpacing(8)
        self.lbl_amps = text_label("-- A", 12, C_SUB, QFont.DemiBold)
        bot.addWidget(self.lbl_amps)
        bot.addStretch(1)
        self.lbl_temp = text_label("-- ℃", 11)
        bot.addWidget(self.lbl_temp)
        self.lbl_relay = text_label("断开", 11, C_SUB, QFont.DemiBold)
        bot.addWidget(self.lbl_relay)
        lay.addLayout(bot)

        # 等值缓存:set_state 只在真实变化时更新控件(G3 防 10Hz 风暴)
        self._last_v: float | None = None
        self._last_a: float | None = None
        self._last_relay: str = ""
        self._last_temp: int | None = None
        self._was_online = False

        # 行4: 迷你趋势图(最近 60s 电压/电流, 纯展示不可交互)
        self.spark = SparkLine(self)
        lay.addWidget(self.spark)

    def push_sample(self, volts, amps) -> None:
        """喂一个采样点(可 None 表示离线/无数据)。由主窗口定时调用。"""
        self.spark.push(volts, amps)

    def clear_sample(self) -> None:
        self.spark.clear()

    @property
    def ch(self) -> int:
        """通道号:首次访问时读 .ui 动态属性 ch 并刷新编号标签。"""
        if self._ch is None:
            try:
                self._ch = int(self.property("ch") or 0)
            except (TypeError, ValueError):
                self._ch = 0
            if self._ch:
                self.lbl_ch.setText(f"CH {self._ch:02d}")
        return self._ch

    @property
    def radius(self) -> int:
        return CARD_RADIUS

    # ---- 交互
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit(self.ch)
        elif ev.button() == Qt.RightButton:
            self.offset_requested.emit(self.ch)   # 右键 → 主窗口弹偏移设置
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.double_clicked.emit(self.ch)
        super().mouseDoubleClickEvent(ev)

    # ---- 电压偏移(校准)
    def set_offset(self, mv: int) -> None:
        """设置该通道电压偏移(mV);mv==0 清除。灰色小字显示于电压右侧。"""
        self._offset_mv = int(mv)
        if self._offset_mv:
            self.lbl_off.setText(f"校准 {self._offset_mv:+d}mV")
            self.lbl_off.setStyleSheet(f"color: {C_SUB}; background: transparent;")
        else:
            self.lbl_off.setText("")
        self.update()

    def set_soc(self, pct: float | None, ah: float | None = None) -> None:
        """充放电模拟中显示 SOC%/容量(Ah)于电压右侧(品牌蓝小字,分两行)。None 清除。"""
        if pct is None:
            if self.lbl_soc.text():
                self.lbl_soc.setText("")
            return
        lines = [f"SOC {pct:.0f}%"]
        if ah is not None:
            lines.append(f"{ah:.3f}Ah")
        # Qt 支持 '\n' 多行;分两行显示(需求 2)
        self.lbl_soc.setText("\n".join(lines))

    # ---- 状态
    def set_selected(self, on: bool):
        if self.selected != on:
            self.selected = on
            self.update()

    def _apply_glow(self):
        """状态光环(像卡片背后有圈灯):离线=灰影 · 断开=淡红 · 闭合(输出)=淡绿。
        仅在状态变化时重设阴影与颜色,避免 10Hz 无谓 update(G3)。"""
        if not self._online:
            want = None                        # 离线:常规灰影
            fx = ("off", (14, 0, 4, QColor(*SHADOW_RGB, 48)))
        elif self._relay_on:
            want = QColor(C_OK)                # 闭合:淡绿量子(qm 绿)
            fx = ("on", (30, 0, 0, QColor(52, 211, 153, 120)))
        else:
            want = QColor(C_ERR)               # 断开:淡红(qm 红)
            fx = ("on", (30, 0, 0, QColor(248, 113, 113, 110)))
        changed = (self._glow_color != want)
        self._glow_color = want
        if changed:
            # 阴影仅在模式/色彩变化时重建
            if self._fx_state != fx:
                self._fx_state = fx
                mode, (blur, dx, dy, col) = fx
                self._fx.setBlurRadius(blur)
                self._fx.setOffset(dx, dy)
                self._fx.setColor(col)
            self.update()

    def _retint(self, lab: QLabel, color: str):
        if lab.property("_color") != color:
            lab.setProperty("_color", color)
            lab.setStyleSheet(f"color: {color}; background: transparent;")

    def set_state(self, row: dict | None):
        """row: {online, volts, amps, relay, temp} 或 None=离线。格式统一电压 V/电流 A。
        所有字段仅在实际值变化时才 setText/_retint,避免 10Hz 轮询风暴下的
        无谓重排版与重绘(G3)。"""
        online = bool(row and row.get("online"))
        relay_on = bool(online and row.get("relay"))
        self._online = online
        self._relay_on = relay_on
        self._apply_glow()
        if online:
            if not self._was_online:
                self.lbl_online.setText("● 在线"); self._retint(self.lbl_online, C_OK)
            self._was_online = True
            v = float(row["volts"])
            if self._last_v != v:
                self._last_v = v
                self.lbl_volts.setText(f"{v:.3f}")
            a = float(row["amps"])
            if self._last_a != a:
                self._last_a = a
                self.lbl_amps.setText(f"{a:.3f} A")
            self._retint(self.lbl_amps, C_ERR if a < 0 else C_SUB)
            relay = bool(row.get("relay"))
            lbl_relay = "闭合" if relay else "断开"
            if self._last_relay != lbl_relay:
                self._last_relay = lbl_relay
                self.lbl_relay.setText(lbl_relay)
            self._retint(self.lbl_relay, C_OK if relay else C_ERR)
            temp = row.get("temp")
            if temp is None:
                if self._last_temp is not None:
                    self._last_temp = None
                    self.lbl_temp.setText("-- ℃")
                self._retint(self.lbl_temp, C_SUB)
            else:
                t = int(temp)
                if self._last_temp != t:
                    self._last_temp = t
                    self.lbl_temp.setText(f"{t} ℃")
                self._retint(self.lbl_temp, C_ERR if t >= 75 else C_WARN if t >= 60 else C_SUB)
        else:
            if self._was_online:
                self.lbl_online.setText("○ 离线"); self._retint(self.lbl_online, C_SUB)
            self._was_online = False
            for lbl, s in ((self.lbl_volts, "--"), (self.lbl_amps, "-- A"),
                           (self.lbl_temp, "-- ℃"), (self.lbl_relay, "--")):
                if lbl.text() != s:
                    lbl.setText(s)
                self._retint(lbl, C_SUB)
            self._last_v = self._last_a = None
            self._last_relay = ""
            self._last_temp = None

    # ---- 自绘:灯圈描边 + 选中态叠加
    def paintEvent(self, ev):
        super().paintEvent(ev)
        # 同一绘制设备只允许一个 QPainter,且必须 begin/end 配对(Qt 对象契约)。
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._glow_color is not None:
            # 灯圈描边(颜色与 _fx 辉光一致,像卡片背后有圈灯)
            rect = QRectF(self.rect()).adjusted(1.2, 1.2, -1.2, -1.2)
            path = QPainterPath()
            path.addRoundedRect(rect, self.radius, self.radius)
            p.setPen(QPen(self._glow_color, 1.6))
            p.drawPath(path)
        if self.selected:
            rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
            path = QPainterPath()
            path.addRoundedRect(rect, self.radius, self.radius)
            # 淡蓝罩(accent 低 α 叠深底;比浅色版略亮才可见)
            p.save()
            p.setClipPath(path)
            p.fillPath(path, QColor(77, 107, 254, 52))
            p.restore()
            # 品牌蓝描边
            p.setPen(QPen(QColor(C_ACCENT), 2))
            p.drawPath(path)
            # 右上角勾选圆点
            badge = QRectF(self.width() - 26, 8, 18, 18)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C_ACCENT))
            p.drawEllipse(badge)
            p.setPen(QPen(QColor("#FFFFFF"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawLine(QPointF(badge.x() + 4.5, badge.y() + 9.5),
                       QPointF(badge.x() + 8, badge.y() + 13))
            p.drawLine(QPointF(badge.x() + 8, badge.y() + 13),
                       QPointF(badge.x() + 13.5, badge.y() + 5.5))
        p.end()
