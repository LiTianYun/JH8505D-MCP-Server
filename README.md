# 程控模拟电池(Programmable Battery)— 针对惠州精惠仪器 JH8505D 模拟电池的 MCP 服务器

> 针对 **惠州精惠仪器 JH8505D 模拟电池** 的 24 通道 CAN 程控模拟电池系统。
> **正式版 v1.0**(git tag `v1.0`,2026-09-08;GUI 已重构为 PySide6 毛玻璃拟态)

基于《[模拟电池CAN协议规范.md](模拟电池CAN协议规范.md)》实现的 **CAN 总线模拟电池控制与监控系统**,
完全复刻 `程控电阻` 工程的"**后端独占 + 多前端共享**"架构:

```
 ┌──────────┐  ┌──────────┐  ┌───────────┐
 │ GUI 控制台│  │  CLI     │  │ MCP(AI)  │     前端:先后无关、互不抢占
 └────┬─────┘  └────┬─────┘  └─────┬─────┘
      │     TCP 127.0.0.1:51920(JSON 行协议)  │
 ┌────┴─────────────┴─────────────┴─────┐
 │         battery_server.py            │     后端:唯一持有 CAN 总线,周期轮询 24 通道并广播
 └──────────────────┬───────────────────┘
                     │ 厂商 DLL 桥接(参考 BMUTesterProject):gcan/cx/zlg/zlg_eu,默认 100 kbps
                 程控模拟电池总装(通道 1..24,100 kbps,29-bit 扩展帧)
```

- **接真实设备(推荐)**:可选 CAN 卡由 `CAN_DLL/Can_Bridge.ini` 的 `VendorList` 决定
  (与 **BMUTesterProject** 同款;**不含仿真器**),默认 `SelVendor=CX`。运行时加载厂商 CAN DLL
  (`ECanVci64.dll`/`ControlCAN.dll`/`zlgcan.dll`),默认波特率 **100 kbps**
  (SJA1000 Timing0=0x04/Timing1=0x1C)。dll 已部署到脚本同级 `CAN_DLL/<厂商>/`。
- **接真实设备(备选 · python-can)**:`vendor=canalystii/pcan/kvaser/vector/socketcan...`
  (需 `pip install python-can`)。
- **AI 可监控**:GUI 窗口实时显示 AI(MCP)对任意通道的操作与回读;反之亦然。

> 早期版本含内置 sim 24 通道仿真器用于离线演示,现已**从可选列表中移除**,避免干扰真实判断;
> 仅保留为内部测试钩子(显式手动启用)。

---

## 文件

| 文件 | 作用 |
|---|---|
| `battery_proto.py` | 协议编解码层:扩展帧 ID 公式、命令组帧、应答解析(含负值补码)、多定标换算(vx100/mv_ma/vx10/vx1) |
| `battery_bus.py` | CAN 总线适配(厂商 DLL 桥接 gcan/cx/zlg/zlg_eu 等;可选卡由 INI 决定) |
| `battery_vci.py` | 厂商 DLL 桥接 · VCI:GCAN `ECanVci64.dll`、CX `ControlCAN.dll`(ctypes 运行时加载) |
| `battery_zlg.py` | 厂商 DLL 桥接 · ZCAN:ZLG `zlgcan.dll`(ctypes 运行时加载) |
| `CAN_DLL/` | 厂商 dll 部署目录 + `Can_Bridge.ini`(可选 CAN 卡配置,与 BMUTesterProject 同款) |
| `battery_server.py` | 后端服务:独占总线、周期轮询(cmd=12 RTR)、离线判定、事件广播 |
| `battery_gui.py` | PySide6 控制台业务逻辑:后端接入 / 事件泵 / 多选批量电压电流继电器控制 / 分色日志 |
| `battery_gui.ui` | GUI 界面布局(Qt Designer 标准文件,可可视化编辑;24 通道卡片矩阵 6×4) |
| `battery_gui_ui.py` | GUI 设计层:毛玻璃拟态 Token / QSS / 玻璃自绘控件(GlassPanel / FeaturedPanel / ChannelCard) |
| `battery_mcp.py` | **MCP stdio 服务器**(AI 工具),零第三方依赖 |
| `battery_cli.py` | 命令行前端(脚本/自动化用) |
| `mcp_host.py` | MCP 测试驱动:以 JSON-RPC 直接调 battery_mcp.py |
| `程控模拟电池控制台.bat` | 双击启动 GUI(自动拉起后端) |

---

## 快速开始

```bat
:: 1) 打开控制台 GUI(自动拉起后端;点「打开总线」,默认读 Can_Bridge.ini 选真实 CAN 卡,如 cx)
程控模拟电池控制台.bat

:: 2) 或纯命令行演示(需已插入 CAN 卡;默认厂商 cx,通道 CAN1,波特率 100k)
python battery_cli.py list-vendors  :: 查看可选 CAN 卡(读 Can_Bridge.ini)
python battery_cli.py open          :: 打开默认 CAN 卡
python battery_cli.py set 1 3.2 1.0 :: 通道1: 设 3.2 V / 1.0 A
python battery_cli.py relay 1 1     :: 通道1: 继电器闭合(输出投入)
python battery_cli.py read          :: 回读 24 通道状态
python battery_cli.py monitor       :: 实时监控模式(Ctrl+C 退出)
```

---

## 接真实 CAN 设备 — 厂商 DLL 桥接(参考 BMUTesterProject)

CAN 连接实现移植自 `BMUTesterProject` 的 `src/can/bridge/`
(`VciBridge.cpp`/`ZlgBridge.cpp` + `ICanBridge`/工厂),用 Python `ctypes` 运行时加载厂商 DLL,
不依赖 python-can 自带后端。支持厂商与 BMU 工程注册表一致:

| vendor | 厂商 | dll | 接口 | 设备类型 |
|---|---|---|---|---|
| `gcan` | 广成 | `CAN_DLL/GCAN/ECanVci64.dll`(依赖 `CHUSBDLL64.dll` 同目录) | VCI(导出无前缀) | 4 |
| `cx` | 创芯科技 | `CAN_DLL/CX/ControlCAN.dll` | VCI(导出带 `VCI_` 前缀) | 4 |
| `zlg` | 周立功 USBCAN-II | `CAN_DLL/ZLG/zlgcan.dll`(依赖 `kerneldlls/`) | ZCAN(新一代) | 4 |
| `zlg_eu` | 周立功 USBCAN-E-U/2E-U | `CAN_DLL/ZLG_EU/zlgcan.dll` | ZCAN | 21 |

关键行为(与 BMU 工程逐项一致):
- **波特率默认 100 kbps**,SJA1000 Timing0/Timing1 时序表(100k = `0x04/0x1C`);
  支持 5k~1M(见 `battery_vci.TIMING_TABLE`)。
- `LoadLibraryExW + LOAD_WITH_ALTERED_SEARCH_PATH` 加载,dll 缺失/依赖缺时报清晰中文错误。
- 扩展帧收发(ExternFlag / ZCAN_EFF)、单次发送;通道 `CAN1/CAN2` → 索引 0/1。
- 打开序列 `OpenDevice → InitCAN(Timing) → StartCAN`,与 BMU `VciBridge::openDevice` 相同。

**部署**(厂商 dll 已随工程放好,参考 BMU `out/CAN_DLL/` 布局):
```bat
:: CAN_DLL/ 结构(已就绪):
::   Can_Bridge.ini   —— [CAN] Vendor / VendorList / SelVendor / SelChannel / Scale
::   GCAN/  ECanVci64.dll + CHUSBDLL64.dll
::   CX/    ControlCAN.dll            (创芯官方 x64 库,自包含)
::   ZLG/   zlgcan.dll + kerneldlls/  (含 msvcr120/msvcp120)
::   ZLG_EU/ zlgcan.dll + kerneldlls/
:: 换用别的卡:编辑 Can_Bridge.ini 的 VendorList / SelVendor;或用 CLI:
python battery_cli.py list-vendors
python battery_cli.py open cx CAN1 100000
:: 设值/回读照常
python battery_cli.py set 1 3.2 1.0
```
> dll 缺失/未插卡时,open 返回明确错误(如“CX 设备打开失败:请确认 CAN 卡已连接…”),
> 不会误报为“找不到 DLL”;若 dll 本身缺失才会提示加载失败。

### 电压/电流读数异常排查(负值 / ×10 偏差)

- **读数偏 ×10**(设 3.1V 显示 31V):定标口径不对,把 `Can_Bridge.ini` 的 `[CAN] Scale`
  改为 `vx100`(0.01V/0.01A,当前默认);GUI「定标」下拉也可切换(mv_ma/vx100/vx10/vx1)。
- **个别通道电流/电压出现巨大正数**(应负却为正):负值补码未解析——当前代码已支持
  16bit/24bit 补码符号扩展,电压电流均可为负(放电/反向)。
- **精确定标**:CLI 诊断输出每通道原始字节与解析中间值:
  ```bat
  python battery_cli.py raw 1     :: 设已知 3.1V 后看 raw_v:310→vx100;3100→mv_ma;31→vx10
  ```
  依据 raw_v 选择对应 Scale 写入 ini,重启后即精确。

---

## 接入 MCP(供 AI 使用)

在 DSH / Claude Code / 其他 MCP 客户端中注册:

```json
{
  "mcpServers": {
    "programmable-battery": {
      "command": "python",
      "args": ["/abs/path/to/程控模拟电池/battery_mcp.py"]
    }
  }
}
```

暴露的 MCP 工具:

| 工具 | 说明 |
|---|---|
| `list_vendors` | 列出可选真实 CAN 卡(读 Can_Bridge.ini 的 VendorList:gcan·cx·zlg·zlg_eu) |
| `open_bus(vendor?, channel?, bitrate?, scale?)` | 打开 CAN 卡;vendor/channel 缺省读 Can_Bridge.ini;波特率默认 100 kbps |
| `close_bus()` | 关闭总线会话 |
| `read_all_channels()` | 轮询并回读 24 通道状态 |
| `read_channel(ch)` | 回读单通道状态 |
| `set_voltage(ch, volts)` | 设输出电压(0~32 V) |
| `set_current(ch, amps)` | 设输出电流(0~10 A) |
| `set_output(ch, volts?, amps?)` | 同时设电压+电流 |
| `set_relay(ch, on)` | 输出继电器闭合/断开(输出投入/关断) |
| `get_status()` | 后端连接 / 总线 / 事件状态 |

命令行冒烟测试(不走客户端,直接验证 MCP):

```bat
python mcp_host.py list-vendors
python mcp_host.py open cx CAN1   :: 打开 CX 卡(未插卡会提示设备打开失败)
python mcp_host.py set 1 3.2 1.0
python mcp_host.py relay 1 1
python mcp_host.py read 1
```

---

## 与协议文档的对应(实现要点)

| 文档章节 | 实现 |
|---|---|
| §1 链路参数 | 扩展帧、默认 100 kbps;DLL 桥接 `bitrate=100000`(SJA1000 时序 0x04/0x1C) |
| §2 帧 ID 编码 | `make_id/split_id`(cmd<<17 \| page<<14 \| src<<7 \| dist) |
| §3/§4 下行命令 | cmd=0 设电压、1 设电流、3 合并、9 继电器、12 RTR 读 |
| §5 上行应答 | cmd=12 应答 8 字节:电压/电流 raw、Data[6] 量纲+继电器位、Data[7] 温度;**支持负值补码**(放电/反向为负) |
| §5.3 离线判定 | 连续 6 轮轮询无应答 → 该通道判离线 |
| §6 定标 | `Can_Bridge.ini [CAN] Scale` 可配:`vx100`(默认,0.01V/0.01A,本机 CX 基准)、`mv_ma`(文档 §6 mV/mA)、`vx10`/`vx1`;读数偏 ×10 时用 vx100 |
| §7 控制流程 | 设值命令连续发 2 次、间隔 5ms;轮询帧间隔 ~4ms |
| §8 通道/范围 | 24 通道、主节点地址 99、0~32 V / 0~10 A |

---

## 后端 JSON 行协议(高级用法)

每行一个 UTF-8 JSON;请求可带 `"id"` 关联响应;`ev:channels` 为广播事件(约 0.4s/轮)。

| 请求 cmd | 说明 |
|---|---|
| `hello(name)` | 注册前端,返回 welcome 快照(含 `proto` 协议版本) |
| `open(vendor, channel, bitrate, scale)` | 打开总线(已开先关再开) |
| `close` | 关闭总线 |
| `set(ch, volts, amps)` / `voltage(ch, volts)` / `current(ch, amps)` | 设电压/电流 |
| `relay(ch, on)` | 继电器开合 |
| `poll` | 手动触发一轮轮询,返回 24 通道状态 |
| `status` / `list_vendors` / `shutdown` | 状态 / 枚举 / 关停 |

> 端口默认 `51920`,可用环境变量 `BATTERY_SERVER=host:port` 覆盖
> (与程控电阻 51917、程控电源 51918 等邻居工程端口互不冲突)。

**可选字段**

| 字段 | 适用命令 | 说明 |
|---|---|---|
| `chs: [1,2,...]` | `set` / `voltage` / `current` / `relay` | 多通道批量,只做一次回读同步与广播 |
| `quiet: true` | 同上 | 自动下发(如充放电模拟每秒调压)**不产生 TX 广播与历史记录**,避免日志刷屏;失败仍照常上报 |

**`proto` 协议版本**:`status` / `welcome` 返回 `proto`(当前 `2`)。端口只能被一个进程占用,
旧后端进程会继续为新前端服务——前端据此判断是否连到了旧代码,过旧时提示重启后端。
前端要求的版本见 `battery_gui.REQUIRED_PROTO`。

**总线故障自动恢复**:向无应答通道发送可能把控制器打进 bus-off(`cx 发送失败(ret=-2)`)。
后端检测到发送/接收失败会 `ResetCAN→ClearBuffer→InitCAN→StartCAN`(DLL 无复位导出时
关闭设备后按原参数重开)并重发本通道一次,成功则继续;仍失败才中止并立即判该通道离线。

## 依赖

- GUI 需 `pip install PySide6`(≥ 6.4);CLI/MCP 主链路:**零第三方依赖**(Python 3.10+ 标准库)。
- 后端接真实 CAN 卡时才需要 `pip install python-can`。

---

## 变更日志

### 2026-09-09

- **feat(gui)**: 新增充放电模拟面板(SOC 驱动电压)——按非线性 OCV-SOC 曲线(NCM/LFP 可选)
  由 SOC 反查目标电压并每秒下发,软件积分容量(Ah),卡片显示 SOC/容量两行;
  支持「模拟电流 / 实际电流」两种模式、「估起始 SOC」按钮(按当前回读电压反查)
- **fix(gui)**: 充放电模拟每秒调压改用 `quiet` 静默下发 + 电压变化阈值 + 离线通道过滤,
  消除日志风暴与"向无应答通道发送"导致的 bus-off
- **fix(server+gui)**: 修复 `quiet` 被旧后端进程静默忽略(端口被旧进程占用):
  新增 `PROTO_VERSION`/`proto` 握手,GUI 检测到过旧后端即提示重启;`quiet` 连历史记录一起静默
- **fix(server)**: 总线 bus-off(`cx 发送失败(ret=-2)`)自动恢复——复位通道并重发一次,
  轮询收发遇错同样自恢复;失败通道立即判离线,GUI 一次性停止全部模拟并只报一条日志
- **style(gui)**: SOC 面板控件分两行、输入框加宽,单位统一 A / Ah

- **fix(soc+gui)**: 修复「起=止 时一启动就判达截止」——截止改为本步越过目标才停
  (起=100/止=100 按放电会立刻报"放电达截止 SOC 100%");模拟电流模式启动时校验
  方向与目标一致(充电需止>起、放电需止<起),不一致直接提示;区分"达目标"与
  "到物理满/空"两种停止原因
- **feat(gui)**: 日志面板改为标签页「监控日志 | SOC 曲线」——曲线页绘制当前配置的
  OCV-SOC 曲线(端点即最高/最低电压)与各运行通道实时工作点(选中通道高亮),
  不占额外布局空间;窗口/曲线/化学体系改动即时重画
- **feat(gui+soc)**: 新增「最高(V)/最低(V)」定义 100%/0% SOC 位置——OCV 曲线按该窗口
  仿射缩放(平台/陡区形状保持),「估起始SOC」用同一窗口反查;切换 NCM/LFP 自动填入
  默认端点(4.20/3.00V、3.65/2.50V)
- **feat(gui)**: 实际电流模式改为「限流 + 单个开始按钮」——充/放方向由回读电流符号
  自动判定(负=充电、正=放电),面板右侧实时显示方向汇总(充电 N · 放电 M · 待定 K);
  电流配置与「开始充电/开始放电」只对模拟电流模式生效
- **fix(soc)**: 截止判定改为「目标在行进方向上才按目标停,否则跟到物理满/空」,
  修正实际电流模式下 SOC 越界/误判截止;截止后容量与 SOC 保持一致

### 2026-09-08(分支 gui-pyside6-refactor)

- **feat(gui)**: 继电器闭合(输出)的通道卡显示品牌蓝"灯圈"辉光 + 蓝色描边,通断状态一眼可辨
- **perf(server)**: 控制命令(set/voltage/current/relay)支持 `chs` 多通道批量——逐通道下发
  但只做一次回读同步与广播,24 通道批量从 ~10s 降到亚秒级;GUI 多选控制走批量接口
- **refactor(gui)**: 界面布局拆分为 Qt Designer 标准 `battery_gui.ui`(可视化编辑),
  玻璃自绘控件与设计 Token 独立为 `battery_gui_ui.py`,`battery_gui.py` 只留业务逻辑
- **refactor(gui)**: GUI 由 tkinter 重构为 PySide6,按《UI设计风格规范_毛玻璃拟态.md》落地
  (蓝白光场背景 + strong/mid/light 三档玻璃 + 深色锚点控制卡 + 品牌蓝 #4D6BFE)
- **feat(gui)**: 通道信息改为卡片矩阵(6×4 平铺 24 卡,一屏全显示,无需滚动)
- **feat(gui)**: 支持通道多选控制(点击多选/全选/反选/清空,批量设定电压/电流与继电器)
- **style(gui)**: 显示格式统一——电压 V、电流 A(均 3 位小数);日志与状态按类型着色
  (INFO 灰 / TX 蓝 / OK 绿 / WARN 橙 / ERR 红,在线绿、离线灰、继电器闭合蓝)
- **fix(gui)**: 修复总线打开事件日志中未定义变量 `btxt` 导致的潜在崩溃

### 2026-09-07

- **feat**: 实现 24 通道程控模拟电池 MCP 服务器(GUI/CLI/MCP 多前端 + TCP 后端)
- **feat**: 移植 BMUTesterProject 厂商 DLL 桥接(CX/GCAN/ZLG,运行时加载,默认 100 kHz)
- **fix**: 修复电压/电流定标口径与负值补码解析(读数偏 ×10、负电流异常)
- **chore**: 新增 .gitignore 与 git 仓库初始化,初始提交
