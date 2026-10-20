#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sub2api 用量监控 —— Windows 系统托盘小应用

托盘图标常驻右下角通知区，图标颜色按「当日剩余额度占比」实时变化：
  - 剩余 ≥ 50%  绿色
  - 剩余 20–50% 橙色
  - 剩余 < 20%  红色
  - 剩余 = 0%   灰色（或：无当日额度限制 / 未连接）

左键单击图标（或右键菜单「显示详情」）弹出详情窗口；关闭详情窗口只隐藏到托盘，
不退出程序；右键菜单可「立即刷新 / 退出」。

依赖：customtkinter、pystray、Pillow、sub2api_client。
运行：Windows 下 `py monitor.py`。
"""

import datetime
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
from tkinter import messagebox

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

from sub2api_client import ApiError, Sub2APIClient

# PyInstaller 单文件模式下 __file__ 指向临时解压目录，config 要写到 exe 旁边
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

INTERVALS = ["1", "3", "5", "10", "15", "30", "60"]

# 开机自启（注册表 HKCU Run 键）
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "sub2api-monitor"

# 托盘图标颜色（按当日剩余额度占比）
GREEN = "#3fb950"
ORANGE = "#d29922"
RED = "#f85149"
GRAY = "#8b949e"
PIE_TRACK = "#3f4752"  # 饼图「空槽」底色（未用部分）

FONT = "Segoe UI"

# 深色主题配色（详情窗口）
PAL = {
    "bg": "#0d1117", "card": "#161b22", "card2": "#1c232e", "border": "#2a3441",
    "text": "#e6edf3", "muted": "#8b949e", "accent": "#3d8bfd",
    "green": GREEN, "orange": ORANGE, "red": RED, "track": "#232b36",
}


def font(size=13, weight="normal"):
    return (FONT, size, weight)


def fmt_int(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "-"


def fmt_cost(v):
    try:
        return f"{float(v):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "-"


def fmt_ms(v):
    try:
        return f"{float(v):.0f} ms"
    except (TypeError, ValueError):
        return "-"


def expiry_parts(s):
    if not s:
        return "-", ""
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        days = (dt.date() - datetime.date.today()).days
        return dt.strftime("%Y-%m-%d"), f"剩 {days} 天"
    except (ValueError, TypeError):
        return str(s), ""


def ratio_color(r):
    """剩余额度占比 -> 托盘图标颜色。"""
    if r is None:
        return GRAY
    if r >= 0.50:
        return GREEN
    if r >= 0.20:
        return ORANGE
    if r > 0:
        return RED
    return GRAY


def make_icon(color, frac=0.0, size=64):
    """生成饼图托盘图标：彩色 = 剩余额度，灰色扇区 = 已用额度。

    frac 为「剩余额度占比」（0..1）；整圆先铺成剩余额度的彩色，
    再用灰色扇区从顶部顺时针扫过已用部分（1-frac）。颜色随剩余额度
    变化（绿→橙→红）。frac=0（已用尽）时为纯灰圆，frac=1 为纯彩色圆。
    """
    frac = min(max(frac, 0.0), 1.0)
    used = 1.0 - frac
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    box = (3, 3, size - 3, size - 3)
    d.ellipse(box, fill=color)                   # 彩色底 = 剩余额度
    if used >= 1.0:
        d.ellipse(box, fill=PIE_TRACK)           # 已用 100%：全灰
    elif used > 0:
        # PIL 角度：0°=3点钟方向，顺时针增加；顶部 = 270°。灰色（已用）扇区从顶部顺时针扫。
        d.pieslice(box, 270.0, 270.0 + 360.0 * used, fill=PIE_TRACK)
    return img


def extract_limits(u):
    """从 /v1/usage 抽出一组 (label, used, limit) 额度数据。"""
    bars = []
    sub = u.get("subscription") or {}

    def add(label, used_key, limit_key):
        lim = sub.get(limit_key)
        if isinstance(lim, (int, float)) and lim > 0:
            bars.append((label, float(sub.get(used_key) or 0), float(lim)))

    add("今日", "daily_usage_usd", "daily_limit_usd")
    add("本周", "weekly_usage_usd", "weekly_limit_usd")
    add("本月", "monthly_usage_usd", "monthly_limit_usd")
    add("近5小时", "usage_5h_usd", "limit_5h_usd")

    quota = u.get("quota") or {}
    if isinstance(quota.get("limit"), (int, float)) and quota["limit"] > 0:
        bars.append(("总额度", float(quota.get("used") or 0), float(quota["limit"])))

    for rl in u.get("rate_limits") or []:
        lim = rl.get("limit")
        if isinstance(lim, (int, float)) and lim > 0:
            bars.append((f"限流 {rl.get('window', '')}", float(rl.get("used") or 0), float(lim)))
    return bars


# --------------------------------------------------------------------------- #
# 表格（基于 CTkScrollableFrame 的自绘表格）
# --------------------------------------------------------------------------- #

class DataTable(ctk.CTkFrame):
    def __init__(self, master, cols, height=150):
        super().__init__(master, fg_color=PAL["card"], corner_radius=12,
                         border_width=1, border_color=PAL["border"])
        self.cols = cols

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 6))
        for key, title, width, align in cols:
            ctk.CTkLabel(head, text=title, width=width, anchor="w",
                         font=font(11, "bold"), text_color=PAL["muted"]).pack(
                side="left", padx=2)

        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent", height=height)
        self.body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def set_rows(self, rows):
        for w in self.body.winfo_children():
            w.destroy()
        for i, r in enumerate(rows):
            bg = PAL["card2"] if i % 2 == 0 else "transparent"
            rowf = ctk.CTkFrame(self.body, fg_color=bg, corner_radius=6)
            rowf.pack(fill="x", pady=1)
            for key, title, width, align in self.cols:
                ctk.CTkLabel(rowf, text=r.get(key, "-"), width=width,
                             anchor=align, font=font(11),
                             text_color=PAL["text"]).pack(side="left", padx=2)


# --------------------------------------------------------------------------- #
# 主程序
# --------------------------------------------------------------------------- #

class TrayApp:
    def __init__(self):
        self._load_config()
        self.client = None
        self.running = False
        self.worker = None
        self.poll_count = 0
        self.interval_seconds = 5
        self.refresh_event = threading.Event()
        self.q = queue.Queue()
        self._last_snapshot = None

        # --- 详情窗口（默认隐藏，托盘点击再显示）---
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.root.title("sub2api 用量监控")
        self.root.geometry("760x720")
        self.root.minsize(680, 560)
        self.root.configure(fg_color=PAL["bg"])
        self._build_ui()
        self._restore_fields()
        self.root.protocol("WM_DELETE_WINDOW", self._hide_window)

        # --- 托盘图标 ---
        self.icon = pystray.Icon(
            "sub2api-monitor",
            make_icon(GRAY),
            "sub2api · 未连接",
            pystray.Menu(
                pystray.MenuItem("显示详情", self._toggle_window, default=True),
                pystray.MenuItem("立即刷新", self._refresh_now),
                pystray.MenuItem("开机自启", self._toggle_autostart,
                                 checked=lambda item: self._is_autostart()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit),
            ),
        )

        self.root.after(200, self._drain_queue)

    # ---- 配置 ----
    def _load_config(self):
        cfg = {"base_url": "", "api_key": "", "interval": 5, "remember": False}
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                cfg.update({k: saved[k] for k in cfg if k in saved})
        except (OSError, ValueError):
            pass
        self.cfg = cfg

    def _save(self):
        self.cfg["base_url"] = self.var_base.get().strip()
        try:
            self.cfg["interval"] = int(self.var_interval.get())
        except (tk.TclError, ValueError):
            self.cfg["interval"] = 5
        self.cfg["remember"] = bool(self.var_remember.get())
        self.cfg["api_key"] = self.var_key.get().strip() if self.cfg["remember"] else ""
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---- 开机自启 ----
    def _autostart_command(self):
        """返回写入 Run 键的启动命令（exe 或 pythonw + 脚本路径）。"""
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        # 源码运行：用 pythonw 后台启动，避免弹出控制台窗口
        pyw = os.path.splitext(sys.executable)[0] + "w.exe"
        if not os.path.exists(pyw):
            pyw = sys.executable
        return f'"{pyw}" "{os.path.abspath(__file__)}"'

    def _is_autostart(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                val, _ = winreg.QueryValueEx(k, RUN_VALUE_NAME)
                return val == self._autostart_command()
        except OSError:
            return False

    def _set_autostart(self, enabled):
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  self._autostart_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass

    def _toggle_autostart(self, icon=None, item=None):
        self._set_autostart(not self._is_autostart())
        self.icon.update_menu()
        # 同步主面板勾选框（切回主线程）
        self.root.after(0, lambda: self.var_autostart.set(self._is_autostart()))

    def _on_autostart_change(self):
        self._set_autostart(self.var_autostart.get())
        self.icon.update_menu()

    # ---- UI 构建 ----
    def _build_ui(self):
        # 顶部栏（固定，不随内容滚动）
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(header, text="sub2api 用量监控", font=font(15, "bold"),
                     text_color=PAL["text"]).pack(side="left")
        self.status_dot = ctk.CTkLabel(header, text="●", font=font(12, "bold"),
                                       text_color=PAL["muted"])
        self.status_dot.pack(side="left", padx=(8, 4))
        self.var_status = ctk.CTkLabel(header, text="未连接", font=font(11),
                                       text_color=PAL["muted"])
        self.var_status.pack(side="left")

        # 全局滚动容器（容纳除顶部栏外的全部内容）
        self.scroll = ctk.CTkScrollableFrame(self.root, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True)

        # 连接设置
        settings = ctk.CTkFrame(self.scroll, fg_color=PAL["card"], corner_radius=12,
                                border_width=1, border_color=PAL["border"])
        settings.pack(fill="x", padx=10, pady=(4, 8))
        row = ctk.CTkFrame(settings, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(row, text="Base URL", font=font(11), text_color=PAL["muted"],
                     width=70).pack(side="left")
        self.var_base = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.var_base, height=30,
                     placeholder_text="https://your-sub2api-gateway",
                     fg_color=PAL["bg"], border_color=PAL["border"],
                     text_color=PAL["text"]).pack(side="left", fill="x", expand=True,
                                                  padx=(0, 10))
        ctk.CTkLabel(row, text="API Key", font=font(11), text_color=PAL["muted"],
                     width=54).pack(side="left")
        self.var_key = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.var_key, height=30, show="●",
                     placeholder_text="sk-…", fg_color=PAL["bg"],
                     border_color=PAL["border"], text_color=PAL["text"]).pack(
            side="left", fill="x", expand=True)

        row2 = ctk.CTkFrame(settings, fg_color="transparent")
        row2.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkLabel(row2, text="刷新间隔", font=font(11), text_color=PAL["muted"]).pack(
            side="left")
        self.var_interval = tk.StringVar(value="5")
        ctk.CTkOptionMenu(row2, values=INTERVALS, variable=self.var_interval,
                          width=70, height=28, fg_color=PAL["card2"],
                          button_color=PAL["accent"], text_color=PAL["text"],
                          font=font(11), command=self._on_interval_change).pack(
            side="left", padx=(6, 10))
        ctk.CTkLabel(row2, text="秒", font=font(11), text_color=PAL["muted"]).pack(
            side="left")
        self.var_remember = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(row2, text="记住 API Key", variable=self.var_remember,
                        font=font(11), text_color=PAL["muted"],
                        fg_color=PAL["accent"]).pack(side="left", padx=(12, 0))
        self.var_autostart = tk.BooleanVar(value=self._is_autostart())
        ctk.CTkCheckBox(row2, text="开机自启", variable=self.var_autostart,
                        command=self._on_autostart_change, font=font(11),
                        text_color=PAL["muted"], fg_color=PAL["accent"]).pack(
            side="left", padx=(12, 0))
        self.btn_start = ctk.CTkButton(row2, text="连接并监控", width=100, height=28,
                                       corner_radius=8, fg_color=PAL["accent"],
                                       hover_color=PAL["accent"], font=font(12, "bold"),
                                       command=self._start)
        self.btn_start.pack(side="right")
        self.btn_stop = ctk.CTkButton(row2, text="停止", width=70, height=28,
                                      corner_radius=8, fg_color=PAL["card2"],
                                      hover_color=PAL["border"],
                                      text_color=PAL["text"], font=font(12),
                                      command=self._stop, state="disabled")
        self.btn_stop.pack(side="right", padx=(0, 8))

        # 概览卡片
        hero = ctk.CTkFrame(self.scroll, fg_color="transparent")
        hero.pack(fill="x", padx=10, pady=(0, 8))
        for c in range(4):
            hero.grid_columnconfigure(c, weight=1, uniform="hero")
        self.hero_remaining = self._hero_card(hero, 0, "剩余额度")
        self.hero_today = self._hero_card(hero, 1, "今日剩余")
        self.hero_weekly = self._hero_card(hero, 2, "本周剩余")
        self.hero_expiry = self._hero_card(hero, 3, "订阅到期")

        # 额度进度
        bars_card = ctk.CTkFrame(self.scroll, fg_color=PAL["card"], corner_radius=12,
                                 border_width=1, border_color=PAL["border"])
        bars_card.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(bars_card, text="剩余额度", font=font(12, "bold"),
                     text_color=PAL["text"]).pack(anchor="w", padx=12, pady=(8, 2))
        self.bars_frame = ctk.CTkFrame(bars_card, fg_color="transparent")
        self.bars_frame.pack(fill="x", padx=12, pady=(0, 8))

        # 用量 KPI
        kpi = ctk.CTkFrame(self.scroll, fg_color=PAL["card"], corner_radius=12,
                           border_width=1, border_color=PAL["border"])
        kpi.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(kpi, text="用量明细（统计口径，可能有延迟）", font=font(12, "bold"),
                     text_color=PAL["text"]).pack(anchor="w", padx=12, pady=(8, 2))
        grid = ctk.CTkFrame(kpi, fg_color="transparent")
        grid.pack(fill="x", padx=8, pady=(0, 8))
        for c in range(4):
            grid.grid_columnconfigure(c, weight=1, uniform="kpi")
        self.kpi = {}
        cards = [("today_tokens", "今日 Token"), ("today_cost", "今日费用"),
                 ("today_requests", "今日请求"), ("total_tokens", "累计 Token"),
                 ("total_cost", "累计费用"), ("rpm", "RPM"),
                 ("tpm", "TPM"), ("avg_ms", "平均耗时")]
        for i, (key, label) in enumerate(cards):
            box = ctk.CTkFrame(grid, fg_color=PAL["card2"], corner_radius=10)
            box.grid(row=i // 4, column=i % 4, padx=3, pady=3, sticky="nsew")
            ctk.CTkLabel(box, text=label, font=font(10), text_color=PAL["muted"]).pack(
                pady=(8, 0))
            var = tk.StringVar(value="-")
            ctk.CTkLabel(box, textvariable=var, font=font(14, "bold"),
                         text_color=PAL["text"]).pack(pady=(0, 8))
            self.kpi[key] = var

        # 分模型 / 每日明细
        models_card = ctk.CTkFrame(self.scroll, fg_color="transparent")
        models_card.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(models_card, text="按模型拆分", font=font(12, "bold"),
                     text_color=PAL["text"]).pack(anchor="w", padx=2, pady=(0, 4))
        self.table_model = DataTable(models_card, [
            ("model", "模型", 210, "w"), ("requests", "请求", 65, "e"),
            ("input_tokens", "输入", 90, "e"), ("output_tokens", "输出", 90, "e"),
            ("total_tokens", "总 Token", 95, "e"),
            ("actual_cost", "费用", 80, "e")], height=110)
        self.table_model.pack(fill="x")

        daily_card = ctk.CTkFrame(self.scroll, fg_color="transparent")
        daily_card.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(daily_card, text="每日明细", font=font(12, "bold"),
                     text_color=PAL["text"]).pack(anchor="w", padx=2, pady=(0, 4))
        self.table_daily = DataTable(daily_card, [
            ("date", "日期", 100, "w"), ("requests", "请求", 70, "e"),
            ("total_tokens", "总 Token", 100, "e"), ("input_tokens", "输入", 80, "e"),
            ("output_tokens", "输出", 80, "e"), ("actual_cost", "费用", 80, "e")],
            height=120)
        self.table_daily.pack(fill="x")

        # 底部状态栏
        footer = ctk.CTkFrame(self.scroll, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=(0, 6))
        self.var_last = ctk.CTkLabel(footer, text="—", font=font(10),
                                     text_color=PAL["muted"])
        self.var_last.pack(side="left")
        ctk.CTkLabel(footer, text="关闭窗口会隐藏到托盘", font=font(10),
                     text_color=PAL["muted"]).pack(side="right")

    def _hero_card(self, parent, col, title):
        card = ctk.CTkFrame(parent, fg_color=PAL["card"], corner_radius=12,
                            border_width=1, border_color=PAL["border"])
        card.grid(row=0, column=col, padx=3, sticky="nsew")
        ctk.CTkLabel(card, text=title, font=font(11), text_color=PAL["muted"]).pack(
            anchor="w", padx=12, pady=(10, 2))
        v = ctk.CTkLabel(card, text="-", font=font(17, "bold"),
                         text_color=PAL["accent"])
        v.pack(anchor="w", padx=12)
        s = ctk.CTkLabel(card, text="-", font=font(10), text_color=PAL["muted"])
        s.pack(anchor="w", padx=12, pady=(2, 10))
        return (v, s)

    def _restore_fields(self):
        self.var_base.set(self.cfg.get("base_url", ""))
        iv = str(self.cfg.get("interval", 5))
        if iv in INTERVALS:
            self.var_interval.set(iv)
        self.interval_seconds = int(iv) if iv in INTERVALS else 5
        self.var_remember.set(bool(self.cfg.get("remember", False)))
        if self.cfg.get("remember"):
            self.var_key.set(self.cfg.get("api_key", ""))

    # ---- 窗口显隐（托盘线程回调 → 主线程）----
    def _toggle_window(self, icon=None, item=None):
        self.root.after(0, self._toggle_window_tk)

    def _toggle_window_tk(self):
        if self.root.state() == "withdrawn":
            self._show_window()
        else:
            self._hide_window()

    def _show_window(self):
        if self._last_snapshot:
            self._render(self._last_snapshot)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_window(self):
        self.root.withdraw()

    def _refresh_now(self, icon=None, item=None):
        self.root.after(0, self._refresh_now_tk)

    def _refresh_now_tk(self):
        if not self.running:
            self._start()
        else:
            self.refresh_event.set()

    def _quit(self, icon=None, item=None):
        self.running = False
        self.icon.stop()
        self.root.after(0, self._shutdown)

    def _shutdown(self):
        self._save()
        self.root.destroy()

    # ---- 事件循环（主线程）----
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "snapshot":
                    self._apply_snapshot(payload)
                elif kind == "auth_required":
                    self._stop()
                    messagebox.showwarning("API Key 无效", payload)
                elif kind == "error":
                    self._set_status(payload, "error")
        except queue.Empty:
            pass
        self.root.after(200, self._drain_queue)

    # ---- 启停 ----
    def _start(self):
        base = self.var_base.get().strip()
        key = self.var_key.get().strip()
        if not base:
            messagebox.showwarning("提示", "请填写 Base URL")
            self._show_window()
            return
        if not key:
            messagebox.showwarning("提示", "请填写 API Key")
            self._show_window()
            return
        self.client = Sub2APIClient(base, key)
        try:
            self.interval_seconds = max(1, int(self.var_interval.get()))
        except (tk.TclError, ValueError):
            self.interval_seconds = 5
        self._save()

        self.running = True
        self.refresh_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_status("连接中…", "busy")

        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def _stop(self):
        self.running = False
        self.refresh_event.set()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._set_status("已停止", "idle")
        self.icon.icon = make_icon(GRAY)
        self.icon.title = "sub2api · 未连接"

    def _on_interval_change(self, value):
        try:
            self.interval_seconds = max(1, int(value))
        except ValueError:
            self.interval_seconds = 5

    def _set_status(self, text, kind="idle"):
        self.var_status.configure(text=text)
        color = {"idle": PAL["muted"], "busy": PAL["accent"],
                 "ok": PAL["green"], "error": PAL["red"]}.get(kind, PAL["muted"])
        self.status_dot.configure(text_color=color)
        self.var_status.configure(text_color=color)

    # ---- 后台轮询线程 ----
    def _worker_loop(self):
        while self.running:
            self._poll_once()
            self.refresh_event.wait(self.interval_seconds)
            self.refresh_event.clear()

    def _poll_once(self):
        try:
            snapshot = self.client.fetch_snapshot()
            self.q.put(("snapshot", snapshot))
        except ApiError as e:
            if e.status in (401, 403):
                self.q.put(("auth_required", f"API Key 无效或无权限：{e.message}"))
                return
            self.q.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"异常: {e}"))

    # ---- 快照应用：更新托盘 + 详情窗口 ----
    def _apply_snapshot(self, snapshot):
        self._last_snapshot = snapshot
        self.poll_count += 1
        self._update_tray(snapshot)
        if self.root.state() != "withdrawn":
            self._render(snapshot)

    def _update_tray(self, snapshot):
        u = snapshot.get("usage") or {}
        sub = u.get("subscription") or {}
        limit = sub.get("daily_limit_usd")
        used = sub.get("daily_usage_usd")
        if not isinstance(limit, (int, float)) or limit <= 0:
            self.icon.icon = make_icon(GRAY)
            self.icon.title = "sub2api · 无当日额度限制"
            return
        limit = float(limit)
        used = float(used) if isinstance(used, (int, float)) else 0.0
        remaining = limit - used
        frac = remaining / limit                 # 剩余占比 = 彩色扇区
        self.icon.icon = make_icon(ratio_color(frac), frac)
        self.icon.title = (
            f"sub2api · 今日用量 {used / limit * 100:.0f}%"
            f"（剩 {fmt_cost(remaining)}/{fmt_cost(limit)} USD）")

    # ---- 详情窗口渲染 ----
    def _render(self, snapshot):
        u = snapshot.get("usage") or {}
        sub = u.get("subscription") or {}
        unit = u.get("unit") or "USD"
        plan = u.get("planName") or "-"
        valid = bool(u.get("isValid", True))
        remaining = u.get("remaining")

        # 概览卡片
        rv, rs = self.hero_remaining
        rv.configure(text=f"{fmt_cost(remaining)} {unit}" if remaining is not None else "-",
                     text_color=PAL["accent"])
        rs.configure(text=f"计划 {plan} · {'有效' if valid else '无效'}")

        def _remain_card(hero, used, limit):
            v, s = hero
            if isinstance(limit, (int, float)) and limit > 0:
                rem = float(limit) - float(used or 0)
                r = rem / limit
                v.configure(text=f"{r * 100:.0f}%", text_color=ratio_color(r))
                s.configure(text=f"剩 {fmt_cost(rem)} / {fmt_cost(limit)} {unit}")
            else:
                v.configure(text="-", text_color=PAL["muted"])
                s.configure(text="无限制")

        _remain_card(self.hero_today, sub.get("daily_usage_usd"), sub.get("daily_limit_usd"))
        _remain_card(self.hero_weekly, sub.get("weekly_usage_usd"), sub.get("weekly_limit_usd"))

        date_str, days_str = expiry_parts(sub.get("expires_at") or u.get("expires_at"))
        ev, es = self.hero_expiry
        ev.configure(text=date_str, text_color=PAL["text"])
        es.configure(text=days_str)

        # 剩余额度进度条
        self._render_bars(extract_limits(u))

        # 用量 KPI
        usage = u.get("usage") or {}
        today = usage.get("today") or {}
        total = usage.get("total") or {}
        self.kpi["today_tokens"].set(fmt_int(today.get("total_tokens")))
        self.kpi["today_cost"].set(fmt_cost(today.get("actual_cost")))
        self.kpi["today_requests"].set(fmt_int(today.get("requests")))
        self.kpi["total_tokens"].set(fmt_int(total.get("total_tokens")))
        self.kpi["total_cost"].set(fmt_cost(total.get("actual_cost")))
        self.kpi["rpm"].set(fmt_int(usage.get("rpm")))
        self.kpi["tpm"].set(fmt_int(usage.get("tpm")))
        self.kpi["avg_ms"].set(fmt_ms(usage.get("average_duration_ms")))

        # 分模型
        models = sorted(u.get("model_stats") or [],
                        key=lambda m: -(float(m.get("actual_cost") or 0)))
        self.table_model.set_rows([{
            "model": m.get("model", "-"), "requests": fmt_int(m.get("requests")),
            "input_tokens": fmt_int(m.get("input_tokens")),
            "output_tokens": fmt_int(m.get("output_tokens")),
            "total_tokens": fmt_int(m.get("total_tokens")),
            "actual_cost": fmt_cost(m.get("actual_cost"))} for m in models])

        # 每日明细
        daily = sorted(u.get("daily_usage") or [],
                       key=lambda d: str(d.get("date", "")), reverse=True)
        self.table_daily.set_rows([{
            "date": d.get("date", "-"), "requests": fmt_int(d.get("requests")),
            "total_tokens": fmt_int(d.get("total_tokens")),
            "input_tokens": fmt_int(d.get("input_tokens")),
            "output_tokens": fmt_int(d.get("output_tokens")),
            "actual_cost": fmt_cost(d.get("actual_cost"))} for d in daily])

        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.var_last.configure(text=f"最后更新 {now}（已刷新 {self.poll_count} 次）")
        self._set_status("监控中", "ok")

    def _render_bars(self, bars):
        for w in self.bars_frame.winfo_children():
            w.destroy()
        if not bars:
            ctk.CTkLabel(self.bars_frame, text="（无额度限制）", font=font(11),
                         text_color=PAL["muted"]).pack(anchor="w")
            return
        for label, used, limit in bars:
            rem = max(0.0, limit - used)
            r = rem / limit if limit > 0 else 0
            row = ctk.CTkFrame(self.bars_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=label, width=64, anchor="w", font=font(11),
                         text_color=PAL["muted"]).pack(side="left")
            bar = ctk.CTkProgressBar(row, height=8, corner_radius=4,
                                     fg_color=PAL["track"],
                                     progress_color=ratio_color(r))
            bar.pack(side="left", fill="x", expand=True, padx=8)
            bar.set(min(r, 1.0))
            ctk.CTkLabel(row, text=f"剩 {fmt_cost(rem)} / {fmt_cost(limit)}", width=180,
                         anchor="e", font=font(11), text_color=PAL["text"]).pack(
                side="left")
            ctk.CTkLabel(row, text=f"{r * 100:.0f}%", width=44, anchor="e",
                         font=font(11, "bold"),
                         text_color=ratio_color(r)).pack(side="left")

    # ---- 启动 ----
    def run(self):
        if self.cfg.get("base_url") and self.cfg.get("api_key"):
            self.root.after(400, self._start)   # 已保存 key，自动开始监控
        else:
            self.root.after(400, self._show_window)  # 首次运行，弹出设置
        self.icon.run_detached()
        self.root.mainloop()


def main():
    TrayApp().run()


if __name__ == "__main__":
    main()
