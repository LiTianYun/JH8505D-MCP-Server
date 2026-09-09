#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通道电压偏移表(calibration):每通道一个电压偏移(单位 mV),保存在本机 ini。

- 文件:脚本同目录 battery_offset.ini(不入 git;每台设备的硬件偏差不同)
- 结构:[VoltOffset] ch1=-6 ch2=0 ...(缺省通道视为 0)
- 语义:设置电压时,实际发送 = 用户设定值 + 偏移。例:通道2 偏移 -6mV,
  用户设 3.200V → 后端实际发送 3.194V(用于修正个别输出通道电压偏高/偏低)。
- GUI 右键卡片设置;后端每次 set/voltage 自动读取补偿——GUI/MCP/CLI 全生效。
"""
from __future__ import annotations

import configparser
import os
import tempfile
import time
from pathlib import Path

INI_PATH = Path(__file__).with_name("battery_offset.ini")
SECTION = "VoltOffset"
DEFAULT_TEXT = ("[VoltOffset]\n"
                "; 每通道电压偏移(mV)。设置电压时实际发送 = 设定值 + 偏移\n"
                "; 例: ch2=-6 表示通道2电压偏高 6mV,设 3.200V 实际发 3.194V\n")

# 进程缓存 + mtime 失效:避免每帧重读磁盘(H16)。外部(如 GUI)改文件后可察觉。
_cache: dict[int, int] | None = None
_cache_mtime: float = 0.0
_cache_size: int = 0


def _invalidate_cache() -> None:
    global _cache, _cache_mtime, _cache_size
    _cache = None
    _cache_mtime = 0.0
    _cache_size = 0


def load_offsets() -> dict[int, int]:
    """读偏移表 {ch: 偏移mV}。无文件/解析失败返回全 0。
    带进程级缓存:ini 未变化时直接返回缓存,降低 set/voltage 高频路径开销。"""
    global _cache, _cache_mtime, _cache_size
    off = {ch: 0 for ch in range(1, 25)}
    try:
        if not INI_PATH.exists():
            _invalidate_cache()
            return off
        st = INI_PATH.stat()
        if (_cache is not None and st.st_mtime_ns == _cache_mtime
                and st.st_size == _cache_size):
            return _cache
        cp = configparser.ConfigParser()
        with open(INI_PATH, "r", encoding="utf-8-sig") as f:
            cp.read_file(f)
        if cp.has_section(SECTION):
            for k, v in cp.items(SECTION):
                try:
                    ch = int(k.lower().replace("ch", ""))
                    if 1 <= ch <= 24:
                        off[ch] = int(float(v))
                except (ValueError, TypeError):
                    continue
        _cache = off
        _cache_mtime = st.st_mtime_ns
        _cache_size = st.st_size
    except (OSError, OSError):
        _invalidate_cache()
    except Exception:
        _invalidate_cache()
    return off


def save_offsets(offsets: dict[int, int]) -> bool:
    """整表写回 ini(含注释模板)。原子写(临时文件+os.replace)避免并发读半写(H16)。"""
    try:
        os.makedirs(INI_PATH.parent, exist_ok=True)
        lines = [DEFAULT_TEXT.rstrip("\n")]
        for ch in range(1, 25):
            v = int(offsets.get(ch, 0))
            if v:
                lines.append(f"ch{ch}={v}")
        content = "\n".join(lines) + "\n"
        # 原子写:先写临时文件再 replace,保证 readers 永远不会看到半写内容
        fd, tmp = tempfile.mkstemp(dir=str(INI_PATH.parent),
                                   prefix=".battery_offset.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, INI_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _invalidate_cache()          # 使缓存失效,后续 load 重读
        return True
    except OSError:
        return False


def apply_offset(ch: int, volts: float) -> float:
    """发送前补偿:用户设定 volts(V) + 该通道偏移(mV) → 实际下发值(V)。"""
    return volts + load_offsets().get(ch, 0) / 1000.0
