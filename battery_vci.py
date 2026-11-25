#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程控模拟电池 — VCI(ControlCAN/GCAN) 原生 DLL 桥接(ctypes)。

移植自 BMUTesterProject 的 `src/can/bridge/vci/VciBridge.cpp`(C++/Qt, 运行时加载厂商 DLL),
用 Python ctypes 以完全相同的方式驱动通用 VCI 兼容 CAN 卡:

适用厂商(与 BMU 工程工厂注册一致):
    GCAN 广成     —— ECanVci64.dll,导出函数名无 "VCI_" 前缀(OpenDevice 等),设备类型 4
    CX  创芯科技  —— ControlCAN.dll(VCI 兼容),导出名带 "VCI_" 前缀,设备类型 4

dll 部署目录(与本仓库脚本同级):
    CAN_DLL/GCAN/ECanVci64.dll   (依赖 CHUSBDLL64.dll 同目录)
    CAN_DLL/CX/ControlCAN.dll    (创芯官方 x64 函数库,自包含)

实现要点(逐条对齐 VciBridge.cpp):
- LoadLibraryExW + LOAD_WITH_ALTERED_SEARCH_PATH 加载厂商 dll(依赖从其同目录解析)
- GetProcAddress 解析导出函数;任一必需导出缺失 → dll 不可用
- 打开序列:OpenDevice(devtype, devind, 0) → InitCAN(滤波/时序) → StartCAN
- 波特率:SJA1000 Timing0/Timing1 时序表(与 BMU 工程 VciBridge.cpp/ZlgBridge.cpp 完全一致),
  100 kbps = Timing0=0x04, Timing1=0x1C(默认)
- 收发:VCI_CAN_OBJ 结构(与 ControlCAN.h 二进制布局一致);ExternFlag=1 为扩展帧
- 错误:ReadErrInfo 查询,位映射为统一错误位(对齐 ICanBridge.h 的 CanBridgeError)
- CAN1/CAN2 设备名 → 设备索引 0/1(本协议单机一通道总线,默认 CAN1)
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

# 本文件所在目录 = CAN_DLL 部署根(脚本同级)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAN_DLL_ROOT = os.path.join(BASE_DIR, "CAN_DLL")
CAN_INI_PATH = os.path.join(CAN_DLL_ROOT, "Can_Bridge.ini")

# ---- 厂商名映射:INI/大写 名称 <-> 内部 vendor 名(小写) ----
# 与 BMUTesterProject CanBridgeFactory 注册表一致,仅此 4 家
VENDOR_ALIAS = {
    "GCAN": "gcan",
    "CX": "cx",
    "ZLG": "zlg",
    "ZLG_EU": "zlg_eu",
}
ALL_HARDWARE_VENDORS = tuple(VENDOR_ALIAS.values())  # 内部小写:gcan/cx/zlg/zlg_eu


def _parse_ini_value(text: str) -> list[str]:
    """按逗号拆分 ini 列表值,去除空白与空项。"""
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def read_can_ini() -> dict:
    """解析脚本同级 CAN_DLL/Can_Bridge.ini(与 BMU 工程同款)。

    返回 {vendor, vendor_list, sel_vendor, sel_channel, scale};ini 缺失/无键时
    用缺省(与 CanBridgeFactory 语义一致:回退 GCAN/全注册厂商)。
    """
    cfg = {"vendor": "GCAN", "vendor_list": list(VENDOR_ALIAS),
           "sel_vendor": None, "sel_channel": None, "scale": None}
    try:
        import configparser
        cp = configparser.ConfigParser()
        # utf-8-sig:兼容 PowerShell 写出的带 BOM ini
        with open(CAN_INI_PATH, "r", encoding="utf-8-sig") as f:
            cp.read_file(f)
    except Exception:
        return cfg
    if not cp.has_section("CAN"):
        return cfg
    sec = cp["CAN"]
    if sec.get("Vendor"):
        cfg["vendor"] = sec.get("Vendor").strip()
    if sec.get("VendorList"):
        lst = _parse_ini_value(sec.get("VendorList"))
        if lst:
            cfg["vendor_list"] = lst
    if sec.get("SelVendor"):
        cfg["sel_vendor"] = sec.get("SelVendor").strip()
    if sec.get("SelChannel"):
        cfg["sel_channel"] = sec.get("SelChannel").strip()
    if sec.get("Scale"):
        cfg["scale"] = sec.get("Scale").strip()
    return cfg


def default_scale() -> str:
    """默认定标(读 Can_Bridge.ini [CAN] Scale);读不到或非法直接报错,不回退。

    定标只有一个判据:设备把 raw 当什么单位解析。
      mv_ma=按 mV    vx100=按 0.01V    vx10=按 0.1V    vx10000=上下行不对称(本机)
    与固件版本无关,也不能用"读数偏 ×10 就改某一档"反推,选错就是整批偏差。
    故意不设缺省:静默回退会让错误定标在整批下发里潜伏。

    改 ini 后必须单通道闭环验证:设 3.10V -> 读回 3.10V。
    """
    from battery_proto import SCALES
    sc = (read_can_ini().get("scale") or "").strip()
    if sc not in SCALES:
        raise RuntimeError(
            f"定标未配置或非法:[CAN] Scale={sc!r}(来自 {CAN_INI_PATH}),"
            f"可选 {sorted(SCALES)}。不回退默认值,请补全配置后重启服务。"
        )
    return sc


def default_vendor() -> str:
    """默认厂商(小写):优先 SelVendor(最近使用),其次 Vendor;均无效回退 gcan。

    与 BMU 主界面行为一致:记住用户上次选的卡。
    """
    cfg = read_can_ini()
    for key in ("sel_vendor", "vendor"):
        name = cfg.get(key)
        if name and name.upper() in VENDOR_ALIAS:
            return VENDOR_ALIAS[name.upper()]
    return "gcan"


def default_channel() -> str:
    """默认通道:ini SelChannel(如 can1),规范化 CAN1/CAN2;缺省 CAN1。"""
    ch = (read_can_ini().get("sel_channel") or "CAN1").strip().upper()
    return ch if ch in ("CAN1", "CAN2") else "CAN1"


def vendor_dll_exists(vendor: str) -> bool:
    """厂商 dll 是否已部署(dll 文件存在于 CAN_DLL/<厂商>/ 或平铺 CAN_DLL/)。"""
    v = vendor.lower()
    if v not in VENDOR_ALIAS.values():
        return False
    up = next(name for name, low in VENDOR_ALIAS.items() if low == v)  # 小写 -> 大写(如 gcan->GCAN)
    # gcan/cx 由 VciDllBridge 的 VENDOR_CONFIG 提供 dll 名;zlg/zlg_eu 固定 zlgcan.dll
    dll = VENDOR_CONFIG[up][0] if up in VENDOR_CONFIG else "zlgcan.dll"
    sub = os.path.join(CAN_DLL_ROOT, up, dll)
    flat = os.path.join(CAN_DLL_ROOT, dll)
    return os.path.exists(sub) or os.path.exists(flat)


def configured_vendors() -> list[str]:
    """INI VendorList 中"受支持且 dll 已部署"的可选 CAN 卡(小写)。

    语义对齐 CanBridgeFactory::vendorsFromIni:只收已注册厂商;
    若 INI 缺省/全部无效则回退全部已注册(但此处仍要求 dll 存在才可选)。
    """
    cfg = read_can_ini()
    out: list[str] = []
    for name in cfg["vendor_list"]:
        v = VENDOR_ALIAS.get(name.upper())
        if v and v not in out and vendor_dll_exists(v):
            out.append(v)
    return out

# ---- 统一错误位(对齐 ICanBridge.h CanBridgeError) ----
CAN_ERR_NONE = 0x0000
CAN_ERR_FIFO_OVERFLOW = 0x0001
CAN_ERR_BUS_ERROR = 0x0002
CAN_ERR_BUS_OFF = 0x0004
CAN_ERR_BUF_OVERFLOW = 0x0008

# ---- VCI 状态 ----
STATUS_OK = 1
STATUS_ERR = 0

# ---- SJA1000 波特率时序表(与 BMU 工程两处 bridge 的 switch 完全一致) ----
# 本协议/设备默认 100 kbps(Timing0=0x04, Timing1=0x1C)
TIMING_TABLE = {
    5_000: (0xBF, 0xFF),
    10_000: (0x31, 0x1C),
    20_000: (0x18, 0x1C),
    40_000: (0x87, 0xFF),
    50_000: (0x09, 0x1C),
    80_000: (0x83, 0xFF),
    100_000: (0x04, 0x1C),
    125_000: (0x03, 0x1C),
    200_000: (0x81, 0xFA),
    250_000: (0x01, 0x1C),
    400_000: (0x80, 0xFA),
    500_000: (0x00, 0x1C),
    666_000: (0x80, 0xB6),
    800_000: (0x00, 0x16),
    1_000_000: (0x00, 0x14),
}
DEFAULT_BITRATE = 100_000  # 默认 100 kbps


class VciCanObj(ctypes.Structure):
    """VCI_CAN_OBJ(与 ControlCAN.h 布局一致, 16 字节对齐)"""

    _fields_ = [
        ("ID", ctypes.c_uint32),
        ("TimeStamp", ctypes.c_uint32),
        ("TimeFlag", ctypes.c_ubyte),
        ("SendType", ctypes.c_ubyte),
        ("RemoteFlag", ctypes.c_ubyte),
        ("ExternFlag", ctypes.c_ubyte),
        ("DataLen", ctypes.c_ubyte),
        ("Data", ctypes.c_ubyte * 8),
        ("Reserved", ctypes.c_ubyte * 3),
    ]


class VciInitConfig(ctypes.Structure):
    """VCI_INIT_CONFIG"""

    _fields_ = [
        ("AccCode", ctypes.c_uint32),
        ("AccMask", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32),
        ("Filter", ctypes.c_ubyte),
        ("Timing0", ctypes.c_ubyte),
        ("Timing1", ctypes.c_ubyte),
        ("Mode", ctypes.c_ubyte),
    ]


class VciErrInfo(ctypes.Structure):
    """VCI_ERR_INFO"""

    _fields_ = [
        ("ErrCode", ctypes.c_uint32),
        ("Passive_ErrData", ctypes.c_ubyte * 3),
        ("ArLost_ErrData", ctypes.c_ubyte),
    ]


# 厂商配置:name -> (dll文件名, 设备类型, 导出前缀)
VENDOR_CONFIG = {
    "GCAN": ("ECanVci64.dll", 4, ""),     # 广成 USBCAN-II(ControlCAN.h 注释: 设备类型 4)
    "CX": ("ControlCAN.dll", 4, "VCI_"),  # 创芯科技 VCI 兼容(设备类型 4, 带 VCI_ 前缀)
}

LOAD_WITH_ALTERED_SEARCH_PATH = 0x8


class VciDllBridge:
    """通用 VCI DLL 桥接(对齐 VciBridge.cpp; 仅 Windows)。"""

    def __init__(self, vendor: str = "GCAN"):
        if os.name != "nt":
            raise RuntimeError("VCI DLL 桥接仅支持 Windows")
        vendor = vendor.upper()
        if vendor not in VENDOR_CONFIG:
            raise RuntimeError(f"不支持的 VCI 厂商: {vendor}(可用 {list(VENDOR_CONFIG)})")
        self.vendor = vendor
        self.dll_name, self.dev_type, self.prefix = VENDOR_CONFIG[vendor]
        self.dev_index = 0
        self._dll = None
        self._fns: dict[str, ctypes] = {}
        self.last_hint = ""
        self.open_ok = False
        self._last_init: VciInitConfig | None = None
        self._load()

    # ---------------- dll 加载与导出解析 ----------------

    def _load(self) -> None:
        """LoadLibraryExW + LOAD_WITH_ALTERED_SEARCH_PATH, 回退平铺文件名。"""
        candidates = []
        sub = os.path.join(CAN_DLL_ROOT, self.vendor, self.dll_name)  # CAN_DLL/GCAN/ECanVci64.dll
        candidates.append(sub)
        candidates.append(os.path.join(CAN_DLL_ROOT, self.dll_name))  # CAN_DLL/<dll> 平铺
        candidates.append(self.dll_name)                               # 系统路径/当前目录

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        load_ex = k32.LoadLibraryExW
        load_ex.restype = wintypes.HMODULE
        load_ex.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
        get_proc = k32.GetProcAddress
        get_proc.restype = ctypes.c_void_p
        get_proc.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]

        self._hmod = None
        for path in candidates:
            if os.path.isabs(path) and not os.path.exists(path):
                continue
            try:
                if os.path.isabs(path):
                    hmod = load_ex(os.path.abspath(path), None, LOAD_WITH_ALTERED_SEARCH_PATH)
                else:
                    hmod = load_ex(path, None, 0)
            except OSError:
                hmod = None
            if hmod:
                self._hmod = hmod
                break
        if not self._hmod:
            self.last_hint = (
                f"{self.vendor} 的 {self.dll_name} 加载失败: 请确认脚本同级 CAN_DLL/{self.vendor}/ "
                f"目录完整(含依赖 dll)且 64 位运行库就绪"
            )
            return

        # 用 ctypes.WinDLL 包句柄(不重复 LoadLibrary), 以支持属性式导出调用
        self._dll = ctypes.WinDLL(self.dll_name, handle=self._hmod)
        # 解析必需导出(前缀 + 函数名, 对齐 VCI_RESOLVE)
        required = ["OpenDevice", "InitCAN", "CloseDevice", "StartCAN",
                    "Receive", "Transmit", "ReadErrInfo"]
        for name in required:
            if get_proc(self._hmod, (self.prefix + name).encode()):
                try:
                    self._fns[name] = getattr(self._dll, self.prefix + name)
                except AttributeError:
                    pass
        missing = [n for n in required if n not in self._fns]
        if missing:
            self._dll = None
            self.last_hint = (
                f"{self.vendor} 的 {self.dll_name} 导出函数不完整(缺 {missing}),"
                f"请确认 dll 为对应厂商的原版 64 位库"
            )
            return
        # 可选导出(重启通道用, 缺失不影响)
        for name in ("ResetCAN", "ClearBuffer"):
            try:
                if get_proc(self._hmod, (self.prefix + name).encode()):
                    self._fns[name] = getattr(self._dll, self.prefix + name)
            except AttributeError:
                pass

    @property
    def dll_ready(self) -> bool:
        return self._dll is not None

    # ---------------- 打开 / 关闭 ----------------

    def open_device(self, device_name: str = "CAN1", bitrate: int = DEFAULT_BITRATE) -> tuple[int, str]:
        """打开设备+初始化通道+启动。返回 (ret, hint)。ret: 0 成功, 负值错误码。"""
        if not self.dll_ready:
            return -10, self.last_hint
        # 设备名 -> 索引
        if device_name == "CAN2":
            self.dev_index = 1
        else:
            self.dev_index = 0
        f = self._fns
        # 1. OpenDevice(devtype, devind, 0)
        f["OpenDevice"].restype = ctypes.c_uint32
        f["OpenDevice"].argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        if f["OpenDevice"](self.dev_type, self.dev_index, 0) != STATUS_OK:
            self.last_hint = (f"{self.vendor} 设备打开失败: 请确认 CAN 卡已连接、驱动已安装、"
                              f"未被其他程序占用")
            return -1, self.last_hint
        # 2. InitCAN(滤波默认全收, 时序表)
        cfg = VciInitConfig()
        cfg.AccCode = 0xFFFFFFFF
        cfg.AccMask = 0xFFFFFFFF
        cfg.Filter = 1
        cfg.Mode = 0
        timing = TIMING_TABLE.get(bitrate, TIMING_TABLE[DEFAULT_BITRATE])
        cfg.Timing0, cfg.Timing1 = timing
        f["InitCAN"].restype = ctypes.c_uint32
        f["InitCAN"].argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.POINTER(VciInitConfig)]
        if f["InitCAN"](self.dev_type, self.dev_index, 0, ctypes.byref(cfg)) != STATUS_OK:
            self._close_dll_device()
            self.last_hint = (f"{self.vendor} 通道初始化失败: 请确认通道号正确(CAN1/CAN2) "
                              f"且波特率参数受支持({bitrate} bps)")
            return -1, self.last_hint
        self._last_init = cfg
        # 3. StartCAN
        f["StartCAN"].restype = ctypes.c_uint32
        f["StartCAN"].argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        if f["StartCAN"](self.dev_type, self.dev_index, 0) != STATUS_OK:
            self._close_dll_device()
            self.last_hint = f"{self.vendor} 通道启动失败: 请检查 CAN 总线接线与波特率"
            return -1, self.last_hint
        self.open_ok = True
        self.last_hint = ""
        return 0, ""

    def _close_dll_device(self) -> None:
        if "CloseDevice" in self._fns:
            fn = self._fns["CloseDevice"]
            fn.restype = ctypes.c_uint32
            fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
            try:
                fn(self.dev_type, self.dev_index)
            except Exception:
                pass

    def close_device(self) -> int:
        if self.open_ok:
            self._close_dll_device()
            self.open_ok = False
        return 0

    # ---------------- 收发 ----------------

    def write_frame(self, arb_id: int, data: bytes = b"", is_ext: bool = True,
                    is_remote: bool = False, dlc: int = 8) -> tuple[int, int]:
        """发送一帧。返回 (ret, len)。对齐 writeFrame: 返回发送字节数或负错误码。"""
        if not self.open_ok:
            return -1, 0
        if not self.dll_ready:
            return -10, 0
        obj = VciCanObj()
        obj.ID = arb_id & 0x1FFFFFFF
        obj.SendType = 1            # 单次发送
        obj.RemoteFlag = 1 if is_remote else 0
        obj.ExternFlag = 1 if is_ext else 0
        n = len(data) if not is_remote else dlc
        n = max(0, min(n, 8))
        obj.DataLen = n
        if data:
            for i in range(min(n, len(data))):
                obj.Data[i] = data[i]
        fn = self._fns["Transmit"]
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.POINTER(VciCanObj), ctypes.c_uint32]
        if fn(self.dev_type, self.dev_index, 0, ctypes.byref(obj), 1) == 1:
            return n, n
        return -2, 0

    def read_frame(self, timeout_ms: int = 100) -> tuple[int, int, bytes, bool]:
        """读一帧。返回 (ret, id, data, is_ext)。
        ret: >=0 数据长度; -1 未打开; -2 超时; -3 硬件/驱动错误; -10 dll 未就绪"""
        if not self.open_ok:
            return -1, 0, b"", False
        if not self.dll_ready:
            return -10, 0, b"", False
        obj = VciCanObj()
        fn = self._fns["Receive"]
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.POINTER(VciCanObj), ctypes.c_uint32, ctypes.c_int]
        n = fn(self.dev_type, self.dev_index, 0, ctypes.byref(obj), 1, timeout_ms)
        if n == 0xFFFFFFFF:
            return -3, 0, b"", False
        if n == 0:
            return -2, 0, b"", False
        dlen = obj.DataLen
        if not 0 <= dlen <= 8:
            return -2, 0, b"", False
        return dlen, obj.ID, bytes(obj.Data[:dlen]), bool(obj.ExternFlag)

    def read_err_info(self) -> tuple[int, int]:
        """查询统一错误位。返回 (ret, err_code)。"""
        if not self.open_ok:
            return -1, CAN_ERR_NONE
        if not self.dll_ready:
            return -10, CAN_ERR_NONE
        err = VciErrInfo()
        fn = self._fns["ReadErrInfo"]
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.POINTER(VciErrInfo)]
        if fn(self.dev_type, self.dev_index, 0, ctypes.byref(err)) != STATUS_OK:
            return -2, CAN_ERR_NONE
        code = err.ErrCode
        uni = CAN_ERR_NONE
        if code & 0x0001:  # ERR_CAN_OVERFLOW
            uni |= CAN_ERR_FIFO_OVERFLOW
        if code & (0x0002 | 0x0004 | 0x0008 | 0x0010):  # ERRALARM|PASSIVE|LOSE|BUSERR
            uni |= CAN_ERR_BUS_ERROR
        if code & 0x0020:  # ERR_CAN_BUSOFF
            uni |= CAN_ERR_BUS_OFF
        if code & 0x0800:  # ERR_BUFFEROVERFLOW
            uni |= CAN_ERR_BUF_OVERFLOW
        return 0, uni

    def restart_channel(self) -> int:
        """复位→清缓冲→按上次配置重新初始化→启动。"""
        if not self.open_ok:
            return -1
        if not self.dll_ready:
            return -10
        if "ResetCAN" not in self._fns or "ClearBuffer" not in self._fns:
            return -3
        f = self._fns
        f["ResetCAN"].restype = ctypes.c_uint32
        f["ResetCAN"].argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        if f["ResetCAN"](self.dev_type, self.dev_index, 0) != STATUS_OK:
            return -2
        f["ClearBuffer"].restype = ctypes.c_uint32
        f["ClearBuffer"].argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        f["ClearBuffer"](self.dev_type, self.dev_index, 0)
        if self._last_init is not None:
            f["InitCAN"].restype = ctypes.c_uint32
            f["InitCAN"].argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.POINTER(VciInitConfig)]
            if f["InitCAN"](self.dev_type, self.dev_index, 0,
                            ctypes.byref(self._last_init)) != STATUS_OK:
                return -2
        f["StartCAN"].restype = ctypes.c_uint32
        f["StartCAN"].argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        if f["StartCAN"](self.dev_type, self.dev_index, 0) != STATUS_OK:
            return -2
        return 0


if __name__ == "__main__":
    # 结构体尺寸自检(与 ControlCAN.h 布局一致: VCI_CAN_OBJ=24, VCI_INIT_CONFIG=16, VCI_ERR_INFO=8)
    assert ctypes.sizeof(VciCanObj) == 24
    assert ctypes.sizeof(VciInitConfig) == 16
    assert ctypes.sizeof(VciErrInfo) == 8
    print("VciCanObj size:", ctypes.sizeof(VciCanObj))
    print("VciInitConfig size:", ctypes.sizeof(VciInitConfig))
    print("VciErrInfo size:", ctypes.sizeof(VciErrInfo))
    # 时序表: 100k 默认
    assert TIMING_TABLE[100_000] == (0x04, 0x1C)
    print("timing 100k:", TIMING_TABLE[100_000], "-> 0x%02X 0x%02X" % TIMING_TABLE[100_000])
    # 无 dll 时: 应报清晰加载错误
    b = VciDllBridge("GCAN")
    print("GCAN dll_ready:", b.dll_ready, "| hint:", b.last_hint[:60])
    r, h = b.open_device()
    print("GCAN open ret:", r)
    print("自检通过")
