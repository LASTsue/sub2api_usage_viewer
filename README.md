# sub2api 用量实时监控（Windows 系统托盘小应用）

一个常驻 Windows 右下角通知区的小应用，用 **API Key**（`sk-…`）实时轮询 sub2api 网关接口 `GET /v1/usage`。托盘图标是一枚**动态饼图**，扇区大小即时反映「今日用量占比」，左键点击弹出详情窗口。

## 托盘图标（饼图 = 今日用量）

图标是一枚饼图：**已用额度作为灰色扇区从顶部（12 点方向）顺时针扫过**，其余彩色部分 = 剩余额度；扇区颜色随剩余额度变化：

| 当日剩余额度 | 扇区颜色 |
|---|---|
| ≥ 50% | 🟢 绿色 |
| 20% – 50% | 🟠 橙色 |
| < 20% | 🔴 红色 |
| 0%（用量 100%） | ⚪ 灰色 |

- 用量 0%（全剩余）→ 全绿实心圆；用量 100%（用尽）→ 全灰。
- 无当日额度限制 / 未连接 → 灰色。
- 鼠标悬停显示「今日用量 xx%（剩 xx/xx USD）」。

## 文件

| 文件 | 说明 |
|---|---|
| `monitor.py` | 托盘主程序（pystray + CustomTkinter 详情窗口） |
| `sub2api_client.py` | 无 UI 依赖的 HTTP 客户端（可复用 / 脚本调用） |
| `config.json` | 运行时自动生成，保存 Base URL 等信息 |

## 依赖

- Python 3（Windows 官方 Python ≥ 3.8 自带 tkinter），外加 `customtkinter`、`pystray`、`Pillow`：

```bat
pip install customtkinter pystray pillow
```

## 运行

在 **Windows** 下（GUI 不能在 WSL 里跑）：

```bat
cd C:\Users\zll\sub2api-monitor
pip install customtkinter pystray pillow
py monitor.py
:: 或 python monitor.py
```

## 使用

1. 首次运行会弹出设置窗口，填 `Base URL`（你的 sub2api 网关地址）和 `API Key`（你的 `sk-…`）。
2. 点「连接并监控」，图标开始变色，默认每 5 秒刷新一次。
3. **左键单击图标**（或右键 →「显示详情」）打开详情窗口；关闭详情窗口只是隐藏到托盘，不会退出。
4. 右键菜单：**显示详情 / 立即刷新 / 退出**（退出才是真正关闭程序）。
5. 勾选「记住 API Key」可把 key 存到本地 `config.json`，下次启动自动连接（默认不存）。

## 详情窗口

- **概览卡片**：剩余额度、今日剩余、本周剩余、订阅到期。
- **剩余额度进度条**：今日 / 本周 / 本月 / 近5小时（订阅额度，**实时**，颜色随剩余占比绿→橙→红）。
- **用量明细**：今日/累计 Token、费用、请求数、RPM/TPM、平均耗时。
- **按模型拆分 / 每日明细**：分模型、按天统计。

> ⚠️ 注意：`/v1/usage` 里「订阅额度 / 剩余额度」是**实时计数**；而 `usage.today` 的 token/请求、`model_stats`、`daily_usage` 属于统计口径，部分部署可能有数小时的聚合延迟（例如今日 token 显示为 0、每日明细落后几天）。以额度与图标颜色为准。

## 用到的接口

| 端点 | 用途 | 鉴权 |
|---|---|---|
| `GET /v1/usage` | 用量 / 订阅额度 / 分模型 / 每日明细 | `Authorization: Bearer sk-…` |

返回结构随 key 的计费模式变化（订阅 / 额度 / 余额），程序已做兼容。

## 打包成单文件 exe（可选）

```bat
pip install pyinstaller
py -m PyInstaller -F -w -n sub2api-monitor --clean ^
    --hidden-import=pystray._win32 ^
    --hidden-import=pystray._util.win32 ^
    --hidden-import=six.moves.queue ^
    monitor.py
```

产物在 `dist\sub2api-monitor.exe`，可拷走双击运行。

## 安全提醒

- API Key 等价于账号凭证，请勿泄露。默认不落盘；勾选「记住 API Key」后以明文存于 `config.json`。
- sub2api 本身可能违反 Anthropic 等服务商 ToS，请仅用于你拥有授权的场景。
