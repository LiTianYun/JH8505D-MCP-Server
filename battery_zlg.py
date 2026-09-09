#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程控模拟电池 — ZLG 周立功 zlgcan.dll(ZCAN 新一代接口) 原生桥接(ctypes)。

移植自 BMUTesterProject 的 `src/can/bridge/zlg/ZlgBridge.cpp` + 最小头 `ZlgCan.h`,
用 Python ctypes 以完全相同的方式运行时加载 zlgcan.dll:

- 结构体/宏布局逐一对齐 ZlgCan.h:
    ZCAN_CAN_FRAME(16B) / ZCAN_TRANSMIT_DATA / ZCAN_RECEIVE_DATA
    ZCAN_CHANNEL_INIT_CONFIG(can 成员: acc_code/acc_mask/reserved/filter/timing0/timing1/mode)
    ZCAN_CHANNEL_ERR_INFO
    can_id 标志位: EFF=bit31, RTR=bit30, ERR=bit29, 低 29 位 ID
- 设备类型参数化(与工厂注册一致): ZLG=USBCAN-II(4), ZLG_EU=USBCAN-E-U/2E-U(21)
- dll 部署: 脚本同级 CAN_DLL/ZLG/zlgcan.dll(依赖 kerneldlls/ 随 dll 同目录)
- 打开序列: ZCAN_OpenDevice → ZCAN_InitCAN(时序表) → ZCAN_StartCAN
- 波特率: SJA1000 Timing0/Timing1(与 VCI 表相同), 100k = 0x04/0x1C(默认)
- 收发: transmit_type=1 单次发送; ZCAN_MAKE_CAN_ID / ZCAN_IS_EFF
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from battery_vci import CAN_DLL_ROOT, DEFAULT_BITRATE, TIMING_TABLE

LOAD_WITH_ALTERED_SEARCH_PATH = 0x8
ZCAN_STATUS_OK = 1
ZCAN_TYPE_CAN = 0
ZCAN_INVALID_HANDLE = 0

# 设备类型编码(ZlgCan.h)
ZCAN_USBCAN2 = 4
ZCAN_USBCAN_2E_U = 21

# can_id 标志位(ZlgCan.h / SocketCAN 布局)
ZCAN_CAN_EFF_FLAG = 0x80000000
ZCAN_CAN_RTR_FLAG = 0x40000000
ZCAN_CAN_ERR_FLAG = 0x20000000
ZCAN_CAN_ID_FLAG = 0x1FFFFFFF

# 厂商配置: name -> (设备类型)
VENDOR_CONFIG = {
    "ZLG": ZCAN_USBCAN2,       # USBCAN-II / USBCAN-2A
    "ZLG_EU": ZCAN_USBCAN_2E_U,  # USBCAN-E-U / 2E-U 系列
}


def make_can_id(arb_id: int, eff: bool, rtr: bool = False, err: bool = False) -> int:
    """ZCAN_MAKE_CAN_ID(id, eff, rtr, err)"""
    return (arb_id & ZCAN_CAN_ID_FLAG) | (0x80000000 if eff else 0) \
        | (0x40000000 if rtr else 0) | (0x20000000 if err else 0)


def is_eff(can_id: int) -> bool:
    return bool(can_id & ZCAN_CAN_EFF_FLAG)


def is_rtr(can_id: int) -> bool:
    return bool(can_id & ZCAN_CAN_RTR_FLAG)


def get_id(can_id: int) -> int:
    return can_id & ZCAN_CAN_ID_FLAG


class ZcanCanFrame(ctypes.Structure):
    _fields_ = [
        ("can_id", ctypes.c_uint32),
        ("can_dlc", ctypes.c_ubyte),
        ("__pad", ctypes.c_ubyte),
        ("__res0", ctypes.c_ubyte),
        ("__res1", ctypes.c_ubyte),
        ("data", ctypes.c_ubyte * 8),
    ]


class ZcanTransmitData(ctypes.Structure):
    _fields_ = [
        ("frame", ZcanCanFrame),
        ("transmit_type", ctypes.c_uint32),
    ]


class ZcanReceiveData(ctypes.Structure):
    _fields_ = [
        ("frame", ZcanCanFrame),
        ("timestamp", ctypes.c_uint64),
    ]


class _ZcanCanCfg(ctypes.Structure):
    """ZCAN_CHANNEL_INIT_CONFIG.can 成员(CAN 非 FD)"""
    _fields_ = [
        ("acc_code", ctypes.c_uint32),
        ("acc_mask", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("filter", ctypes.c_ubyte),
        ("timing0", ctypes.c_ubyte),
        ("timing1", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),
    ]


class _ZcanCanFdCfg(ctypes.Structure):
    _fields_ = [
        ("acc_code", ctypes.c_uint32),
        ("acc_mask", ctypes.c_uint32),
        ("abit_timing", ctypes.c_uint32),
        ("dbit_timing", ctypes.c_uint32),
        ("brp", ctypes.c_uint32),
        ("filter", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),
        ("pad", ctypes.c_uint16),
        ("reserved", ctypes.c_uint32),
    ]


class _ZcanCfgUnion(ctypes.Union):
    _fields_ = [("can", _ZcanCanCfg), ("canfd", _ZcanCanFdCfg)]


class ZcanChannelInitConfig(ctypes.Structure):
    _fields_ = [
        ("can_type", ctypes.c_uint32),
        ("u", _ZcanCfgUnion),
    ]


class ZcanChannelErrInfo(ctypes.Structure):
    _fields_ = [
        ("error_code", ctypes.c_uint32),
        ("passive_ErrData", ctypes.c_ubyte * 3),
        ("arLost_ErrData", ctypes.c_ubyte),
    ]


class ZlgDllBridge:
    """ZLG zlgcan.dll 桥接(对齐 ZlgBridge.cpp; 仅 Windows)。"""

    def __init__(self, vendor: str = "ZLG"):
        if os.name != "nt":
            raise RuntimeError("ZLG DLL 桥接仅支持 Windows")
        vendor = vendor.upper()
        if vendor not in VENDOR_CONFIG:
            raise RuntimeError(f"不支持的 ZLG 厂商: {vendor}(可用 {list(VENDOR_CONFIG)})")
        self.vendor = vendor
        self.dev_type = VENDOR_CONFIG[vendor]
        self.dll_name = "zlgcan.dll"
        self.dev_index = 0
        self.can_index = 0
        self._hmod = None
        self._dll = None
        self._fns: dict[str, ctypes] = {}
        self.last_hint = ""
        self.open_ok = False
        self._init_cfg: ZcanChannelInitConfig | None = None
        self._load()

    # ---------------- dll 加载与导出解析 ----------------

    def _load(self) -> None:
        candidates = [
            os.path.join(CAN_DLL_ROOT, self.vendor, self.dll_name),   # CAN_DLL/ZLG/zlgcan.dll
            os.path.join(CAN_DLL_ROOT, self.dll_name),                # CAN_DLL/zlgcan.dll 平铺
            self.dll_name,
        ]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        load_ex = k32.LoadLibraryExW
        load_ex.restype = wintypes.HMODULE
        load_ex.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
        get_proc = k32.GetProcAddress
        get_proc.restype = ctypes.c_void_p
        get_proc.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]

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
                f"zlgcan.dll 加载失败: 请确认脚本同级 CAN_DLL/{self.vendor}/ 目录完整"
                f"(zlgcan.dll、msvcr120/msvcp120、kerneldlls/)"
            )
            return
        self._dll = ctypes.WinDLL(self.dll_name, handle=self._hmod)
        required = ["ZCAN_OpenDevice", "ZCAN_InitCAN", "ZCAN_CloseDevice", "ZCAN_StartCAN",
                    "ZCAN_Receive", "ZCAN_Transmit", "ZCAN_ReadChannelErrInfo"]
        for name in required:
            if get_proc(self._hmod, name.encode()):
                try:
                    self._fns[name] = getattr(self._dll, name)
                except AttributeError:
                    pass
        missing = [n for n in required if n not in self._fns]
        if missing:
            self._dll = None
            self.last_hint = f"zlgcan.dll 导出函数不完整(缺 {missing}), 请确认为官方原版"
            return
        for name in ("ZCAN_ResetCAN", "ZCAN_ClearBuffer"):
            try:
                if get_proc(self._hmod, name.encode()):
                    self._fns[name] = getattr(self._dll, name)
            except AttributeError:
                pass

    @property
    def dll_ready(self) -> bool:
        return self._dll is not None

    # ---------------- 打开 / 关闭 ----------------

    def open_device(self, device_name: str = "CAN1", bitrate: int = DEFAULT_BITRATE) -> tuple[int, str]:
        if not self.dll_ready:
            return -10, self.last_hint
        self.can_index = 1 if device_name == "CAN2" else 0
        f = self._fns
        # ZCAN_OpenDevice(devtype, devind, 0)
        f["ZCAN_OpenDevice"].restype = ctypes.c_void_p
        f["ZCAN_OpenDevice"].argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        hdev = f["ZCAN_OpenDevice"](self.dev_type, self.dev_index, 0)
        if not hdev:
            self.last_hint = (
                f"ZLG 设备打开失败: 请确认 CAN 卡已连接、驱动已安装、未被其他程序占用"
                f"(设备类型 {self.dev_type})")
            return -1, self.last_hint
        self._hdev = hdev
        # ZCAN_InitCAN(hdev, can_index, &cfg)
        cfg = ZcanChannelInitConfig()
        cfg.can_type = ZCAN_TYPE_CAN
        cfg.u.can.acc_code = 0xFFFFFFFF
        cfg.u.can.acc_mask = 0xFFFFFFFF
        cfg.u.can.filter = 1
        cfg.u.can.mode = 0
        timing = TIMING_TABLE.get(bitrate, TIMING_TABLE[DEFAULT_BITRATE])
        cfg.u.can.timing0, cfg.u.can.timing1 = timing
        f["ZCAN_InitCAN"].restype = ctypes.c_void_p
        f["ZCAN_InitCAN"].argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.POINTER(ZcanChannelInitConfig)]
        hch = f["ZCAN_InitCAN"](hdev, self.can_index, ctypes.byref(cfg))
        if not hch:
            self.last_hint = (f"ZLG 通道初始化失败: 请确认通道号正确(CAN1/CAN2) "
                              f"且波特率参数受支持({bitrate} bps)")
            self._close_hdev()
            return -1, self.last_hint
        self._hch = hch
        self._init_cfg = cfg
        # ZCAN_StartCAN(hch)
        f["ZCAN_StartCAN"].restype = ctypes.c_uint32
        f["ZCAN_StartCAN"].argtypes = [ctypes.c_void_p]
        if f["ZCAN_StartCAN"](hch) != ZCAN_STATUS_OK:
            self.last_hint = "ZLG 通道启动失败: 请检查 CAN 总线接线与波特率"
            self._close_hdev()
            return -1, self.last_hint
        self.open_ok = True
        self.last_hint = ""
        return 0, ""

    def _close_hdev(self) -> None:
        if getattr(self, "_hdev", None) and "ZCAN_CloseDevice" in self._fns:
            fn = self._fns["ZCAN_CloseDevice"]
            fn.restype = ctypes.c_uint32
            fn.argtypes = [ctypes.c_void_p]
            try:
                fn(self._hdev)
            except Exception:
                pass
        self._hdev = None
        self._hch = None

    def close_device(self) -> int:
        if self.open_ok:
            self._close_hdev()
            self.open_ok = False
        return 0

    # ---------------- 收发 ----------------

    def write_frame(self, arb_id: int, data: bytes = b"", is_ext: bool = True,
                    is_remote: bool = False, dlc: int = 8) -> tuple[int, int]:
        if not self.open_ok:
            return -1, 0
        if not self.dll_ready:
            return -10, 0
        tx = ZcanTransmitData()
        tx.transmit_type = 1  # 单次发送
        tx.frame.can_id = make_can_id(arb_id, eff=is_ext, rtr=is_remote)
        n = len(data) if not is_remote else dlc
        n = max(0, min(n, 8))
        tx.frame.can_dlc = n
        for i in range(n):
            tx.frame.data[i] = data[i] if i < len(data) else 0
        fn = self._fns["ZCAN_Transmit"]
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ZcanTransmitData), ctypes.c_uint32]
        if fn(self._hch, ctypes.byref(tx), 1) == 1:
            return n, n
        return -2, 0

    def read_frame(self, timeout_ms: int = 100) -> tuple[int, int, bytes, bool]:
        if not self.open_ok:
            return -1, 0, b"", False
        if not self.dll_ready:
            return -10, 0, b"", False
        rx = ZcanReceiveData()
        fn = self._fns["ZCAN_Receive"]
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ZcanReceiveData), ctypes.c_uint32,
                       ctypes.c_int]
        n = fn(self._hch, ctypes.byref(rx), 1, timeout_ms)
        if n == 0xFFFFFFFF:
            return -3, 0, b"", False
        if n == 0:
            return -2, 0, b"", False
        dlen = rx.frame.can_dlc
        if not 0 <= dlen <= 8:
            return -2, 0, b"", False
        return dlen, get_id(rx.frame.can_id), bytes(rx.frame.data[:dlen]), is_eff(rx.frame.can_id)

    def read_err_info(self) -> tuple[int, int]:
        if not self.open_ok:
            return -1, 0
        if not self.dll_ready:
            return -10, 0
        err = ZcanChannelErrInfo()
        fn = self._fns["ZCAN_ReadChannelErrInfo"]
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ZcanChannelErrInfo)]
        if fn(self._hch, ctypes.byref(err)) != ZCAN_STATUS_OK:
            return -2, 0
        code = err.error_code
        uni = 0
        if code & 0x0001:
            uni |= 0x0001
        if code & (0x0002 | 0x0004 | 0x0008 | 0x0010):
            uni |= 0x0002
        if code & 0x0020:
            uni |= 0x0004
        if code & (0x0040 | 0x0800):
            uni |= 0x0008
        return 0, uni

    def restart_channel(self) -> int:
        if not self.open_ok:
            return -1
        if not self.dll_ready:
            return -10
        if "ZCAN_ResetCAN" not in self._fns or "ZCAN_ClearBuffer" not in self._fns:
            return -3
        f = self._fns
        f["ZCAN_ResetCAN"].restype = ctypes.c_uint32
        f["ZCAN_ResetCAN"].argtypes = [ctypes.c_void_p]
        if f["ZCAN_ResetCAN"](self._hch) != ZCAN_STATUS_OK:
            return -2
        f["ZCAN_ClearBuffer"].restype = ctypes.c_uint32
        f["ZCAN_ClearBuffer"].argtypes = [ctypes.c_void_p]
        f["ZCAN_ClearBuffer"](self._hch)
        f["ZCAN_StartCAN"].restype = ctypes.c_uint32
        f["ZCAN_StartCAN"].argtypes = [ctypes.c_void_p]
        if f["ZCAN_StartCAN"](self._hch) != ZCAN_STATUS_OK:
            return -2
        return 0


if __name__ == "__main__":
    # 结构体尺寸自检(ZlgCan.h 布局)
    print("ZcanCanFrame size:", ctypes.sizeof(ZcanCanFrame))           # 16
    print("ZcanTransmitData size:", ctypes.sizeof(ZcanTransmitData))   # 20
    print("ZcanReceiveData size:", ctypes.sizeof(ZcanReceiveData))     # 24
    print("ZcanChannelInitConfig size:", ctypes.sizeof(ZcanChannelInitConfig))
    assert ctypes.sizeof(ZcanCanFrame) == 16
    assert ctypes.sizeof(ZcanTransmitData) == 20
    assert ctypes.sizeof(ZcanReceiveData) == 24
    # ID 宏验证
    cid = make_can_id(0x1234567, eff=True)
    assert is_eff(cid) and get_id(cid) == 0x1234567 and not is_rtr(cid)
    cid2 = make_can_id(0x1234567, eff=True, rtr=True)
    assert is_rtr(cid2)
    print("ID 宏验证通过")
    # 无 dll: 清晰报错
    b = ZlgDllBridge("ZLG")
    print("ZLG dll_ready:", b.dll_ready, "| hint:", b.last_hint[:50])
    r, h = b.open_device()
    print("ZLG open ret:", r)
    print("ZLG 自检通过")
