#!/usr/bin/env python3
"""hwdash — 硬件面板（GTK4 + libadwaita 原生桌面应用）

三个页面：
  * 温度 —— Resources 风格的渐变填充曲线大卡片 + 可点击图例芯片 + 传感器列表
  * 风扇 —— 卡片式通道列表，模式用 ToggleGroup 切换，曲线在图上直接拖拽编辑
  * 灯光 —— 每个灯区一张卡：灯带预览、模式/颜色/亮度/速度、逐颗渐变编辑器

数据来源：本机守护进程 hwdashd（127.0.0.1:8788）。它不在线时退化为只读模式
（直接读 sysfs 画图，风扇/灯光控制不可用）。

用法：python3 hwdash.py [--snapshot DIR]
  --snapshot DIR：把三个页面各渲染成一张 PNG 后退出。
  Wayland 下外部工具拿不到窗口像素（org.gnome.Shell.Screenshot 被拒绝），
  所以让程序用 Gsk.CairoRenderer 把自己的渲染节点画进 PNG，用于无头自检。
"""
from __future__ import annotations

import faulthandler  # noqa: E402  段错误时打印 Python 调用栈（崩溃取证）

faulthandler.enable()

import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cairo  # noqa: E402  必须在 gi 之前 import 并注册 foreign struct
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_foreign("cairo")
from gi.repository import Adw, Gdk, Gio, GLib, Gsk, Gtk  # noqa: E402

# ------------------------------------------------------------------ 双语 UI
_LANG_PATH = os.path.expanduser("~/.config/hw-dash/lang")
_LANG = "zh"
try:
    _raw = open(_LANG_PATH, encoding="utf-8").read().strip()
    _LANG = _raw if _raw in ("zh", "en") else "zh"
except OSError:
    pass

def T(s: str) -> str:
    """界面双语：中文为源串；英文模式查表，缺条目回退中文。"""
    return _EN.get(s, s) if _LANG == "en" else s

def current_lang() -> str:
    return _LANG

def set_lang(lang: str) -> None:
    global _LANG
    _LANG = lang if lang in ("zh", "en") else "zh"
    try:
        os.makedirs(os.path.dirname(_LANG_PATH), exist_ok=True)
        with open(_LANG_PATH, "w", encoding="utf-8") as fh:
            fh.write(_LANG)
    except OSError:
        pass

def _lang_btn_label() -> str:
    return "EN / 中文" if _LANG == "zh" else "中文 / EN"

_EN = {
    "硬件面板": "Hardware Panel",
    "温度": "Temps", "风扇": "Fans", "电压": "Voltage", "信息": "Info", "灯光": "Lighting",
    "使用前请阅读": "Please read before use",
    "本面板可以接管风扇转速、修改灯光状态。请知悉：\n\n"
    "· 风扇曲线 / 手动模式会接管散热 —— 请确保调速合理、温度监控正常；"
    "过热时硬件会降频甚至强制关机保护，但长期高温会加速老化\n"
    "· 本软件按现状提供，作者不对因使用或误用造成的任何硬件或数据问题负责\n"
    "· 电压 / 温度 / 频率等只读功能没有风险":
    "This panel can take over fan speed and change lighting state. Please note:\n\n"
    "· Curve / manual fan modes take over cooling — keep speeds sane and watch "
    "temperatures; overheating triggers throttling or forced shutdown, but sustained "
    "heat ages hardware faster\n"
    "· This software is provided as-is; the author is not liable for any hardware or "
    "data damage caused by use or misuse\n"
    "· Read-only features (voltage / temperature / frequency) carry no risk",
    "退出": "Quit", "我已了解，继续使用": "I understand, continue",
    "守护进程未运行 —— 只读模式（风扇与灯光不可调）。": "Daemon offline — read-only mode (fans & lighting disabled).",
    "启动：sudo systemctl start hwdashd": "Start: sudo systemctl start hwdashd",
    "点击行或图例芯片可切换该路曲线的显示": "Click a row or legend chip to toggle that curve",
    "风扇通道": "Fan channels", "全部交还 BIOS": "Return all to BIOS",
    "已全部交还 BIOS": "All fans returned to BIOS", "失败": "Failed",
    "⚠ 曲线 / 手动模式会接管风扇转速：调低前请确认散热与温度监控正常，过热由硬件降频、强制关机兜底，但长期高温会加速老化":
    "⚠ Curve / manual modes take over fan speed — verify cooling and temps before dialing down; hardware throttling and forced shutdown are the last line of defense, but sustained heat ages hardware faster",
    "该通道没有转速回读 —— 排针上可能没接风扇": "No tach feedback on this channel — nothing may be connected",
    "此刻目标": "Target now", "实际写入": "applied",
    "拖锚点实时生效 · 点空白加点 · 右键删点": "Drag anchors for live effect · click blank to add · right-click to delete",
    "BIOS 自动": "BIOS auto", "曲线调速": "Curve", "手动固定": "Manual", "占空比": "Duty",
    "每核心电压（MSR 0x198）+ VCore 曲线 + 主板电压轨道（voltmon 同源）。":
    "Per-core voltage (MSR 0x198) + VCore curve + motherboard rails (voltmon-sourced).",
    "主板电压轨道": "Motherboard voltage rails",
    "主板 Super-I/O ADC · 与基准测试台同源 · 报警位可能不可信":
    "Motherboard Super-I/O ADC · same source as the bench · alarm flags may be unreliable",
    "与基准测试台同源 · 报警位可能不可信": "same source as the bench · alarm flags may be unreliable",
    "没有可用的 MSR 读数": "No MSR readings available", "每核心电压不可用": "Per-core voltage unavailable",
    "平均": "avg", "会话": "session", "限值": "limits",
    "报警中": "ALARMING", "报警位不可信": "alarm flag unreliable",
    "无参数": "no params", "速度": "Speed", "亮度": "Brightness", "方向": "Direction",
    "随机颜色": "random colors", "逐颗颜色": "per-LED color",
    "处理器": "CPU", "内存": "Memory", "实时占用": "Live usage",
    "CPU 利用率": "CPU utilization",
    "读不到 cpufreq 频率": "cpufreq not readable",
    "实时频率 · 最低": "Live clocks · min", "平均": "avg", "最高": "max",
    "未检测到独显/核显遥测": "no GPU telemetry detected",
    "核心": "core", "显存": "VRAM", "占用": "util",
    "（无）": "(none)", "未读出内存信息": "no memory info", "实时时钟": "real clock",
    "灯珠": "LEDs", "点击或拖拽灯珠上色（自动切换到 Direct 模式）":
    "Click or drag LEDs to paint (auto-switches to Direct mode)",
    "画笔": "Brush", "粗细": "Size", "填充": "Fill", "清除": "Clear", "彩虹": "Rainbow",
    "所有灯珠 = 画笔色": "All LEDs = brush color", "所有灯珠熄灭": "All LEDs off",
    "全设备彩虹渐变": "Rainbow gradient across the device",
    "模式": "Modes", "选中即生效（OpenRGB 语义）": "Selecting applies instantly (OpenRGB semantics)",
    "把当前状态另存为配置档": "Save current state as a profile",
    "删除选中的配置档": "Delete the selected profile",
    "配置档": "Profile", "保存配置档": "Save profile",
    "把当前所有设备的灯光状态保存为 OpenRGB 配置档。": "Save all devices' lighting state as an OpenRGB profile.",
    "取消": "Cancel", "保存": "Save", "已保存配置档": "Profile saved",
    "保存失败": "Save failed", "没有可删除的配置档": "No profile to delete",
    "已删除": "Deleted", "删除失败": "Delete failed", "加载配置档失败": "Profile load failed",
    "模式下发失败": "Mode apply failed", "没有可用的灯光设备": "No lighting devices available",
    "灯光不可用": "Lighting unavailable", "未知原因": "unknown reason", "未知": "unknown",
    "向左": "Left", "向右": "Right", "向上": "Up", "向下": "Down",
    "水平": "Horizontal", "垂直": "Vertical", "颜色": "Colors", "无模式信息": "No mode info",
        "（无配置档）": "(no profiles)", "颗": "LED(s)",
    "CPU_FAN": "CPU_FAN", "CPU_OPT / 水泵": "CPU_OPT / Pump",
    "SYS_FAN 1": "SYS_FAN 1", "SYS_FAN 2": "SYS_FAN 2", "SYS_FAN 3": "SYS_FAN 3",
    "SYS_FAN / 水泵": "SYS_FAN / Pump",
    "当前": "now", "亮圈锚点决定此刻转速": "the highlighted anchor sets the speed right now",
    "占空比固定，BIOS 不再参与": "Duty fixed; BIOS no longer participates",
}

API = "http://127.0.0.1:8788"
POLL_SEC = 2.0
SNAPSHOT_DIR = None

# 明亮饱和、深浅主题下都可读的曲线配色（CPU 固定用第一色）
PALETTE = ["#3584e4", "#e8503a", "#f5a623", "#2fb344",
           "#9d5ce6", "#00b8d9", "#e85ca8", "#8d9e12"]
ACCENT = "#3584e4"

CSS = b"""
.hw-value { font-weight: 700; font-variant-numeric: tabular-nums; }
.hw-value-big { font-size: 24px; font-weight: 800; font-variant-numeric: tabular-nums; }
.hw-cap { font-size: 12px; opacity: 0.82; }
.temp-ok  { color: #26a269; }
.temp-warm{ color: #e5a50a; }
.temp-hot { color: #e01b24; }
.hw-chip { border-radius: 9999px; padding: 1px 4px; }
.hw-card-pad { padding: 12px 14px 10px 14px; }
.hw-tile { padding: 6px 10px; border-radius: 10px; }
.hw-vmax { color: @accent_color; }
"""


# ------------------------------------------------------------------ 工具

def _hex_rgb(c: str):
    c = c.lstrip("#")
    return (int(c[0:2], 16) / 255, int(c[2:4], 16) / 255, int(c[4:6], 16) / 255)


def _rgba_hex(rgba: Gdk.RGBA) -> str:
    return "#{:02X}{:02X}{:02X}".format(
        int(rgba.red * 255 + 0.5), int(rgba.green * 255 + 0.5), int(rgba.blue * 255 + 0.5))


def _rounded_path(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _temp_class(v: float) -> str:
    return "temp-hot" if v >= 80 else ("temp-warm" if v >= 60 else "temp-ok")


def _drain_box(box: Gtk.Box) -> list[Gtk.Widget]:
    """摘下 box 里全部子控件并返回引用列表（不立即销毁）。

    在信号 emission 上下文里同步 remove + 释放控件是 PyGObject +
    Python 3.14 组合下段错误（_gi.cpython-314 segfault at 30）的高发源：
    dispose 期间若有排队的 emission 回调访问半析构对象，就会解引用
    悬空 C 指针。这里只摘挂（unparent，控件即刻离开 UI 与事件路径），
    Python 引用由调用方持一小段时间再释放，让 emission 队列先排空。
    """
    old = []
    c = box.get_first_child()
    while c is not None:
        nxt = c.get_next_sibling()
        box.remove(c)
        old.append(c)
        c = nxt
    return old


def _drop_later(widgets: list[Gtk.Widget], ms: int = 150):
    """延迟释放控件引用（配合 _drain_box 使用）。"""
    if not widgets:
        return

    def drop():
        widgets.clear()   # 仅释放 Python 引用，dispose 交给 GObject 引用计数
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(ms, drop)


def _fmt_secs(s: float) -> str:
    if s >= 60:
        return f"-{int(round(s / 60))}m"
    return f"-{int(round(s))}s" if s >= 5 else "now"


def _eval(points, t):
    pts = sorted((float(a), float(b)) for a, b in points)
    if not pts:
        return 0.0
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return d1
            f = (t - t0) / (t1 - t0)
            return d0 + (d1 - d0) * f
    return pts[-1][1]


# ------------------------------------------------------------------ API 客户端

class ApiClient:
    def __init__(self):
        self.online = False
        self.last_error = ""
        self.chip = None
        self._last_state: dict | None = None

    def _get(self, path: str, timeout: float = 6.0):
        req = urllib.request.Request(API + path)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _post(self, path: str, payload: dict, timeout: float = 30.0):
        req = urllib.request.Request(
            API + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def state(self) -> dict:
        try:
            d = self._get("/api/state")
            self.online = True
            self.chip = d.get("chip")
            self._last_state = d
            return d
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.online = False
            self.last_error = str(e)
            d = self._local_state()
            self._last_state = d
            return d

    def post(self, path: str, payload: dict) -> tuple[bool, str]:
        try:
            d = self._post(path, payload)
            return bool(d.get("ok")), str(d.get("message") or d.get("error") or "")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            return False, str(e)

    def rgb(self, refresh: bool = False) -> dict:
        try:
            return self._get("/api/rgb" + ("?refresh=1" if refresh else ""), timeout=45.0)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e), "zones": [], "modes": [], "device": {}}

    # -- 守护进程不在线时的只读兜底 ---------------------------------------
    def _local_state(self) -> dict:
        try:
            import hwhw
            sensors = [s.__dict__ for s in hwhw.scan_sensors()]
            cpu = 40.0
            for s in sensors:
                if s["key"] == "coretemp:temp1":
                    cpu = s["value"]
            return {"ts": time.time(), "chip": None, "cpu_temp": cpu,
                    "sensors": sensors,
                    "fans": [{"n": i, "label": f"pwm{i}", "duty": 0, "enable": 2,
                              "rpm": 0, "has_tach": False, "mode": "bios",
                              "curve": [], "curve_duty_now": None}
                             for i in range(1, 7)],
                    "rgb": {"ok": False, "error": "守护进程未运行", "zones": []},
                    "voltages": {"cores": [], "rails": [],
                                 "error": "守护进程未运行（MSR 需 root 读取）"}}
        except Exception as e:  # noqa: BLE001
            return {"ts": time.time(), "chip": None, "cpu_temp": 0,
                    "sensors": [], "fans": [],
                    "rgb": {"ok": False, "error": str(e), "zones": []}}


API_ = ApiClient()


def run_async(fn, on_done=None):
    """阻塞调用放工作线程，结果回 GTK 主线程。

    一切可能超过几十毫秒的 HTTP/子进程调用都必须走这里 ——
    灯光页之前在主线程同步等 OpenRGB（可达数十秒），整个程序冻结。
    """
    def worker():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            res = e
        if on_done:
            def deliver():
                on_done(res)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)
    threading.Thread(target=worker, daemon=True).start()


# ------------------------------------------------------------------ 温度曲线图

class TempChart(Gtk.DrawingArea):
    def __init__(self, hist: dict):
        super().__init__()
        self.hist = hist
        self.window = 300          # 显示最近多少秒
        self.visible: set[str] = set()
        self.colors: dict[str, str] = {}
        # y 轴自适应参数（温度页用默认值；电压页改 unit/step/floor/ceil）
        self.unit = "°"
        self.y_step = 10.0
        self.y_margin = 4.0
        self.y_floor = 30.0
        self.y_ceil = 70.0
        self.set_size_request(-1, 230)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h):
        fg = self.get_color()
        pad_l, pad_r, pad_t, pad_b = 40, 12, 10, 20
        pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
        if pw <= 20 or ph <= 20:
            return
        now = time.time()
        t0 = now - self.window

        vals = [v for k in self.visible for _t, v in self.hist.get(k, ())]
        ymin, ymax = self.y_floor, self.y_ceil
        if vals:
            ymin = min(ymin, math.floor((min(vals) - self.y_margin) / self.y_step) * self.y_step)
            ymax = max(ymax, math.ceil((max(vals) + self.y_margin) / self.y_step) * self.y_step)
        span = max(self.y_step, ymax - ymin)

        def Y(v):
            return pad_t + (1 - (v - ymin) / span) * ph

        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)

        # 水平网格 + 数值刻度
        vv = math.ceil(ymin / self.y_step) * self.y_step
        while vv <= ymax:
            y = Y(vv)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.09)
            cr.set_line_width(1)
            cr.move_to(pad_l, y)
            cr.line_to(w - pad_r, y)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(4, y + 3.5)
            cr.show_text(f"{vv:g}{self.unit}")
            vv += self.y_step

        # 垂直网格 + 时间刻度
        for i in range(5):
            x = pad_l + i / 4 * pw
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.055)
            cr.move_to(x, pad_t)
            cr.line_to(x, pad_t + ph)
            cr.stroke()
            lbl = "now" if i == 4 else _fmt_secs(self.window * (1 - i / 4))
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
            cr.move_to(min(x + 2, w - pad_r - 30), h - 5)
            cr.show_text(lbl)

        # 各路曲线：渐变填充 + 描边 + 端点
        for k in sorted(self.visible):
            data = [(t, v) for (t, v) in self.hist.get(k, ()) if t >= t0]
            if not data:
                continue
            r, g, b = _hex_rgb(self.colors.get(k, ACCENT))
            pts = [(pad_l + (t - t0) / self.window * pw, Y(v)) for t, v in data]
            cr.save()
            cr.rectangle(pad_l - 1, pad_t - 1, pw + 2, ph + 2)
            cr.clip()
            cr.move_to(pts[0][0], pad_t + ph)
            for x, y in pts:
                cr.line_to(x, y)
            cr.line_to(pts[-1][0], pad_t + ph)
            cr.close_path()
            lg = cairo.LinearGradient(0, pad_t, 0, pad_t + ph)
            lg.add_color_stop_rgba(0, r, g, b, 0.30)
            lg.add_color_stop_rgba(1, r, g, b, 0.02)
            cr.set_source(lg)
            cr.fill()
            cr.restore()

            cr.set_source_rgb(r, g, b)
            cr.set_line_width(1.8)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.move_to(*pts[0])
            for x, y in pts[1:]:
                cr.line_to(x, y)
            cr.stroke()
            cr.arc(pts[-1][0], pts[-1][1], 3, 0, 6.2832)
            cr.fill()


# ------------------------------------------------------------------ 温度页

class LegendChip(Gtk.ToggleButton):
    def __init__(self, key: str, color: str, label: str, on_toggle):
        super().__init__()
        self.key = key
        self.add_css_class("hw-chip")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dot = Gtk.DrawingArea()
        dot.set_content_width(11)
        dot.set_content_height(11)

        def draw_dot(a, cr, w, h):
            r, g, b = _hex_rgb(color)
            cr.set_source_rgb(r, g, b)
            cr.arc(w / 2, h / 2, min(w, h) / 2 - 1, 0, 6.2832)
            cr.fill()
        dot.set_draw_func(draw_dot)
        box.append(dot)
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.add_css_class("hw-cap")
        box.append(lbl)
        self.val = Gtk.Label(label="—")
        self.val.add_css_class("hw-value")
        self.val.add_css_class("hw-cap")
        box.append(self.val)
        self.set_child(box)
        self.set_active(True)
        self.connect("toggled", lambda b: on_toggle(key, b.get_active()))


class TempPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.hist: dict[str, deque] = {}
        self.rows: dict[str, dict] = {}
        self.chips: dict[str, LegendChip] = {}
        self._built = False

        # ---- 大卡片：曲线 + 图例
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.add_css_class("hw-card-pad")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="温度监控", xalign=0)
        title.add_css_class("heading")
        head.append(title)
        self.cpu_lbl = Gtk.Label(label="CPU —", xalign=1, hexpand=True)
        self.cpu_lbl.add_css_class("hw-value-big")
        head.append(self.cpu_lbl)
        card.append(head)

        self.chart = TempChart(self.hist)
        card.append(self.chart)

        self.legend = Gtk.FlowBox()
        self.legend.set_selection_mode(Gtk.SelectionMode.NONE)
        self.legend.set_activate_on_single_click(False)
        self.legend.set_max_children_per_line(6)
        self.legend.set_min_children_per_line(1)
        self.legend.set_column_spacing(6)
        self.legend.set_row_spacing(4)
        self.legend.set_halign(Gtk.Align.FILL)
        card.append(self.legend)
        self.append(card)

        # ---- 传感器列表
        group = Adw.PreferencesGroup(title="传感器",
                                     description=T("点击行或图例芯片可切换该路曲线的显示"))
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        group.add(self.listbox)
        clamp = Adw.Clamp(maximum_size=940, tightening_threshold=880)
        clamp.set_child(group)
        self.append(clamp)

    def _toggle(self, key: str, on: bool):
        if on:
            self.chart.visible.add(key)
        else:
            self.chart.visible.discard(key)
        self.chart.queue_draw()

    def _rename(self, key: str):
        row = self.rows[key]["row"]
        dlg = Adw.MessageDialog(transient_for=self.get_root(), heading="重命名传感器",
                                body=f"{key} · 仅影响显示，不影响硬件")
        entry = Gtk.Entry(text=row.get_title())
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", T("取消"))
        dlg.add_response("ok", T("保存"))
        dlg.set_default_response("ok")

        def on_resp(_d, resp):
            if resp == "ok":
                name = entry.get_text().strip()
                if name:
                    row.set_title(name)
                    run_async(lambda: API_.post("/api/sensor", {"key": key, "label": name}))
            dlg.close()
        dlg.connect("response", on_resp)
        dlg.present()

    def _sensor_row(self, s: dict, color: str):
        key = s["key"]
        row = Adw.ActionRow(title=s["label"],
                            subtitle=f"{s['group']} · {s['channel']}"
                                     + (f"  —  {s['note']}" if s.get("note") else ""))
        row.set_activatable(True)

        dot = Gtk.DrawingArea()
        dot.set_content_width(12)
        dot.set_content_height(12)
        dot.set_valign(Gtk.Align.CENTER)

        def draw_dot(a, cr, w, h):
            r, g, b = _hex_rgb(color)
            cr.set_source_rgb(r, g, b)
            cr.arc(w / 2, h / 2, min(w, h) / 2 - 1, 0, 6.2832)
            cr.fill()
        dot.set_draw_func(draw_dot)
        row.add_prefix(dot)

        val = Gtk.Label(label=f"{s['value']:.0f} °C", xalign=1)
        val.add_css_class("hw-value")
        val.add_css_class(_temp_class(s["value"]))
        row.add_suffix(val)

        edit = Gtk.Button(icon_name="document-edit-symbolic")
        edit.add_css_class("flat")
        edit.set_valign(Gtk.Align.CENTER)
        edit.set_tooltip_text("重命名")
        edit.connect("clicked", lambda _b, k=key: self._rename(k))
        row.add_suffix(edit)

        row.connect("activated", lambda _r, k=key: self._toggle_row(k))
        return {"row": row, "val_lbl": val, "cls": _temp_class(s["value"])}

    def _toggle_row(self, key: str):
        chip = self.chips.get(key)
        if chip:
            chip.set_active(not chip.get_active())   # 触发 _toggle

    def update(self, st: dict):
        sensors = st.get("sensors", [])
        if not sensors:
            return
        if not self._built:
            self._built = True
            for i, s in enumerate(sensors):
                color = PALETTE[i % len(PALETTE)]
                key = s["key"]
                self.chart.colors[key] = color
                h = self.hist.setdefault(key, deque(maxlen=2200))
                h.append((time.time(), s["value"]))
                entry = self._sensor_row(s, color)
                self.listbox.append(entry["row"])
                self.rows[key] = entry
                chip = LegendChip(key, color, s["label"], self._toggle)
                chip.val.set_text(f"{s['value']:.0f}°")
                self.legend.append(chip)
                self.chips[key] = chip
                if i < 3:
                    self.chart.visible.add(key)
                else:
                    chip.set_active(False)

        for s in sensors:
            key = s["key"]
            entry = self.rows.get(key)
            if not entry:
                continue
            self.hist.setdefault(key, deque(maxlen=2200)).append((time.time(), s["value"]))
            lbl = entry["val_lbl"]
            new_cls = _temp_class(s["value"])
            if entry["cls"] != new_cls:
                lbl.remove_css_class(entry["cls"])
                lbl.add_css_class(new_cls)
                entry["cls"] = new_cls
            lbl.set_text(f"{s['value']:.0f} °C")
            chip = self.chips.get(key)
            if chip:
                chip.val.set_text(f"{s['value']:.0f}°")
            if entry["row"].get_title() != s["label"]:
                entry["row"].set_title(s["label"])

        self.cpu_lbl.set_text(f"CPU {st.get('cpu_temp', 0):.0f}°C")
        self.cpu_lbl.remove_css_class("temp-ok")
        self.cpu_lbl.remove_css_class("temp-warm")
        self.cpu_lbl.remove_css_class("temp-hot")
        self.cpu_lbl.add_css_class(_temp_class(st.get("cpu_temp", 0)))
        self.chart.queue_draw()


# ------------------------------------------------------------------ 风扇曲线编辑器

class CurveEditor(Gtk.DrawingArea):
    """温度 -> 占空比 曲线编辑器。左键拖动锚点；点空白处新增；右键删除。"""

    T_MIN, T_MAX = 20.0, 100.0
    PAD_L, PAD_R, PAD_T, PAD_B = 46, 16, 12, 26

    def __init__(self, on_change):
        super().__init__()
        self.points: list[list] = [[40, 30], [60, 60], [80, 155], [90, 255]]
        self.cpu_temp: float | None = None
        self.on_change = on_change
        self._drag = -1
        self.set_size_request(-1, 200)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._pressed)
        click.connect("released", self._released)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.add_controller(motion)

    # -- 坐标换算 ----------------------------------------------------------
    def _geom(self, w, h):
        return w - self.PAD_L - self.PAD_R, h - self.PAD_T - self.PAD_B

    def _xy(self, t, d, w, h):
        pw, ph = self._geom(w, h)
        x = self.PAD_L + (t - self.T_MIN) / (self.T_MAX - self.T_MIN) * pw
        y = self.PAD_T + (1 - d / 255.0) * ph
        return x, y

    def _td(self, x, y, w, h):
        pw, ph = self._geom(w, h)
        t = self.T_MIN + (x - self.PAD_L) / max(1, pw) * (self.T_MAX - self.T_MIN)
        d = (1 - (y - self.PAD_T) / max(1, ph)) * 255.0
        return t, max(0, min(255, d))

    # -- 交互 --------------------------------------------------------------
    def _hit(self, x, y, w, h) -> int:
        best, bd = -1, 14.0
        for i, (t, d) in enumerate(self.points):
            px, py = self._xy(t, d, w, h)
            dist = math.hypot(px - x, py - y)
            if dist < bd:
                best, bd = i, dist
        return best

    def _pressed(self, gesture, _n, x, y):
        w, h = self.get_width(), self.get_height()
        # GTK 4.22 没有 get_current_button_state（写了必抛 AttributeError，
        # _drag 永远设不上 —— 表现就是"曲线根本拖不动"）；取当前序列按钮
        # 用 get_current_button()。
        btn = gesture.get_current_button()
        idx = self._hit(x, y, w, h)
        if btn == Gdk.BUTTON_SECONDARY:
            if idx >= 0 and len(self.points) > 2:
                self.points.pop(idx)
                self.queue_draw()
                self.on_change([list(p) for p in self.points])
            return
        if idx >= 0:
            self._drag = idx
            return
        if len(self.points) >= 10:
            return
        t, d = self._td(x, y, w, h)
        t = max(self.T_MIN + 2, min(self.T_MAX - 2, t))
        new_pt = [round(t, 1), int(round(d))]
        self.points.append(new_pt)
        self.points.sort(key=lambda p: p[0])
        self._drag = self.points.index(new_pt)
        self.queue_draw()

    def _motion(self, _c, x, y):
        if self._drag < 0:
            return
        w, h = self.get_width(), self.get_height()
        t, d = self._td(x, y, w, h)
        t = max(self.T_MIN + 2, min(self.T_MAX - 2, t))
        old = self.points[self._drag]
        old[0], old[1] = round(t, 1), int(round(d))
        self.points.sort(key=lambda p: p[0])
        self._drag = self.points.index(old)
        self.queue_draw()
        # 拖动过程中即时下发（节流 250ms）：转速实时跟随，
        # 不必等松手 —— 否则体感就是"曲线调不动"
        now = time.monotonic()
        if now - getattr(self, "_last_emit", 0.0) > 0.25:
            self._last_emit = now
            self.on_change([list(p) for p in self.points])

    def _released(self, _g, _n, _x, _y):
        if self._drag >= 0:
            self._drag = -1
            self.on_change([list(p) for p in self.points])

    # -- 绘制 --------------------------------------------------------------
    def _draw(self, area, cr, w, h):
        fg = self.get_color()
        pw, ph = self._geom(w, h)
        if pw <= 20 or ph <= 20 or not self.points:
            return
        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)

        # 网格
        for i in range(0, 9):
            t = self.T_MIN + i * 10
            x = self.PAD_L + (t - self.T_MIN) / (self.T_MAX - self.T_MIN) * pw
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.07)
            cr.set_line_width(1)
            cr.move_to(x, self.PAD_T)
            cr.line_to(x, self.PAD_T + ph)
            cr.stroke()
            if i % 2 == 0:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
                cr.move_to(x - 12, h - 8)
                cr.show_text(f"{t:g}°")
        for j in range(5):
            d = j * 64
            y = self.PAD_T + (1 - d / 255.0) * ph
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.07)
            cr.move_to(self.PAD_L, y)
            cr.line_to(self.PAD_L + pw, y)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(4, y + 3.5)
            cr.show_text(f"{d * 100 // 255}%")

        ar, ag, ab = _hex_rgb(ACCENT)

        # 曲线下方渐变填充
        pts = [self._xy(t, d, w, h) for t, d in sorted(self.points)]
        cr.save()
        cr.rectangle(self.PAD_L - 1, self.PAD_T - 1, pw + 2, ph + 2)
        cr.clip()
        cr.move_to(pts[0][0], self.PAD_T + ph)
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(pts[-1][0], self.PAD_T + ph)
        cr.close_path()
        lg = cairo.LinearGradient(0, self.PAD_T, 0, self.PAD_T + ph)
        lg.add_color_stop_rgba(0, ar, ag, ab, 0.25)
        lg.add_color_stop_rgba(1, ar, ag, ab, 0.02)
        cr.set_source(lg)
        cr.fill()
        cr.restore()

        # 曲线
        cr.set_source_rgb(ar, ag, ab)
        cr.set_line_width(2)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.move_to(*pts[0])
        for x, y in pts[1:]:
            cr.line_to(x, y)
        cr.stroke()

        # CPU 当前温度参考线
        if self.cpu_temp is not None:
            t = max(self.T_MIN, min(self.T_MAX, self.cpu_temp))
            duty = _eval(self.points, t)
            x, y = self._xy(t, duty, w, h)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.set_line_width(1)
            cr.set_dash([4, 4])
            cr.move_to(x, self.PAD_T)
            cr.line_to(x, self.PAD_T + ph)
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.85)
            cr.arc(x, y, 4.5, 0, 6.2832)
            cr.stroke()
            label = f"{t:.0f}°C → {int(duty) * 100 // 255}%"
            cr.set_font_size(10)
            tw = cr.text_extents(label)[2]
            lx = min(max(x + 8, self.PAD_L), w - self.PAD_R - tw)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
            cr.move_to(lx, self.PAD_T + 12)
            cr.show_text(label)

        # 锚点（距当前 CPU 温度最近的锚点加亮环 —— 它决定此刻的实际转速）
        cur_i = -1
        if self.cpu_temp is not None and self.points:
            bt = min(abs(p[0] - self.cpu_temp) for p in self.points)
            cur_i = next((i for i, p in enumerate(self.points)
                          if abs(p[0] - self.cpu_temp) == bt), -1)
        for i, (x, y) in enumerate(pts):
            active = (i == self._drag)
            cr.set_source_rgb(ar, ag, ab)
            cr.arc(x, y, 6.5 if active else 5.5, 0, 6.2832)
            cr.fill()
            if i == cur_i and i != self._drag:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
                cr.set_line_width(1.5)
                cr.arc(x, y, 10, 0, 6.2832)
                cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.arc(x, y, 2.4, 0, 6.2832)
            cr.fill()


# ------------------------------------------------------------------ 风扇页

MODE_NAMES = ["bios", "curve", "manual"]
def _mode_labels():
    return [T("BIOS 自动"), T("曲线调速"), T("手动固定")]


def _make_mode_switch(cur: str, on_change):
    """优先用 Adw.ToggleGroup（分段控件），老版本回退 DropDown。"""
    if hasattr(Adw, "ToggleGroup"):
        g = Adw.ToggleGroup()
        for name, label in zip(MODE_NAMES, _mode_labels()):
            tg = Adw.Toggle()
            tg.set_name(name)
            tg.set_label(label)
            g.add(tg)
        g.set_active_name(cur if cur in MODE_NAMES else "bios")

        def changed(_g, _ps):
            on_change(g.get_active_name())
        g.connect("notify::active-name", changed)
        return g, (lambda: g.get_active_name()), (lambda m: g.set_active_name(m))
    dd = Gtk.DropDown.new_from_strings(_mode_labels())
    dd.set_selected(MODE_NAMES.index(cur) if cur in MODE_NAMES else 0)

    def changed(_g, _ps):
        on_change(MODE_NAMES[dd.get_selected()])
    dd.connect("notify::selected", changed)
    return dd, (lambda: MODE_NAMES[dd.get_selected()]), \
        (lambda m: dd.set_selected(MODE_NAMES.index(m) if m in MODE_NAMES else 0))


class FanCard(Gtk.Box):
    def __init__(self, fan: dict, on_mode, on_curve, on_manual, toast):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.n = fan["n"]
        self._on_mode, self._on_curve, self._on_manual = on_mode, on_curve, on_manual
        self._toast = toast
        self._syncing = False
        self._manual_timer = None

        clamp = Adw.Clamp(maximum_size=820, tightening_threshold=760)
        self.append(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.add_css_class("card")
        box.add_css_class("hw-card-pad")
        clamp.set_child(box)

        # 头部：名称 + 转速
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.title = Gtk.Label(xalign=0)
        self.title.set_text(f"pwm{self.n} · {fan['label']}")
        self.title.add_css_class("heading")
        head.append(self.title)
        self.readout = Gtk.Label(xalign=1, hexpand=True)
        self.readout.add_css_class("hw-value-big")
        head.append(self.readout)
        box.append(head)

        # 模式切换
        self.mode_w, self.mode_get, self.mode_set = _make_mode_switch(
            fan.get("mode", "bios"), self._mode_changed)
        box.append(self.mode_w)

        # 曲线编辑器
        self.editor = CurveEditor(self._curve_changed)
        self.editor.points = [list(p) for p in (fan.get("curve") or self.editor.points)]
        self.editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.editor_box.append(self.editor)
        box.append(self.editor_box)

        # 手动滑杆
        self.manual_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.manual_row.append(Gtk.Label(label=T("占空比"), xalign=0))
        self.manual_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 255, 5)
        self.manual_scale.set_hexpand(True)
        self.manual_scale.set_draw_value(False)
        self.manual_scale.connect("value-changed", self._manual_changed)
        self.manual_row.append(self.manual_scale)
        self.manual_pct = Gtk.Label(label="50%", width_chars=5, xalign=1)
        self.manual_pct.add_css_class("hw-value")
        self.manual_row.append(self.manual_pct)
        box.append(self.manual_row)

        self.hint = Gtk.Label(xalign=0, wrap=True)
        self.hint.add_css_class("hw-cap")
        box.append(self.hint)
        self.update(fan)

    # -- 回调 --------------------------------------------------------------
    def _mode_changed(self, mode: str):
        if self._syncing:
            return
        self._sync_widgets(mode)
        self._on_mode(self.n, mode)

    def _sync_widgets(self, mode: str):
        self.editor_box.set_visible(mode == "curve")
        self.manual_row.set_visible(mode == "manual")

    def _curve_changed(self, points):
        self._on_curve(self.n, points)

    def _manual_changed(self, scale):
        if self._syncing:
            return
        d = int(round(scale.get_value()))
        self.manual_pct.set_text(f"{d * 100 // 255}%")
        if self._manual_timer:
            GLib.source_remove(self._manual_timer)
        self._manual_timer = GLib.timeout_add(450, self._manual_fire, d)
        return GLib.SOURCE_REMOVE

    def _manual_fire(self, d):
        self._manual_timer = None
        self._on_manual(self.n, d)
        return GLib.SOURCE_REMOVE

    # -- 状态刷新 ----------------------------------------------------------
    def update(self, fan: dict):
        mode = fan.get("mode", "bios")
        rpm = fan.get("rpm", 0)
        duty = fan.get("duty", 0)
        has = fan.get("has_tach", True)
        if has:
            self.readout.set_text(f"{rpm} RPM   {duty * 100 // 255}%")
        else:
            self.readout.set_text(f"{duty * 100 // 255}%")
        self._syncing = True
        if self.mode_get() != mode:
            self.mode_set(mode)
        self._sync_widgets(mode)
        if mode == "manual" and self.manual_scale.get_value() != duty:
            self.manual_scale.set_value(duty)
            self.manual_pct.set_text(f"{duty * 100 // 255}%")
        self._syncing = False
        self.editor.cpu_temp = (API_._last_state or {}).get("cpu_temp")
        self.editor.queue_draw()
        notes = []
        if mode == "curve":
            ct = (API_._last_state or {}).get("cpu_temp")
            tgt = fan.get("curve_duty_now")
            if ct is not None:
                notes.append(f"CPU {T('当前')} {ct:.0f}°C，{T('亮圈锚点决定此刻转速')}")
            if tgt is not None:
                notes.append(f"{T('此刻目标')} {tgt * 100 // 255}%，{T('实际写入')} {duty * 100 // 255}%")
            notes.append(T("拖锚点实时生效 · 点空白加点 · 右键删点"))
        elif mode == "manual":
            notes.append(T("占空比固定，BIOS 不再参与"))
        else:
            notes.append("由主板 Smart Fan 接管 —— 切到「曲线」即可拖折点调速")
        if not has:
            notes.append(T("该通道没有转速回读 —— 排针上可能没接风扇"))
        self.hint.set_text("  ·  ".join(notes))


class FanPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.cards: dict[int, FanCard] = {}
        self._mode_pending: dict[int, str] = {}

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=T("风扇通道"), xalign=0, hexpand=True)
        title.add_css_class("heading")
        head.append(title)
        btn = Gtk.Button(label=T("全部交还 BIOS"))
        btn.add_css_class("destructive-action")
        btn.connect("clicked", self._all_bios)
        head.append(btn)
        self.append(head)

        warn = Gtk.Label(
            label=T("⚠ 曲线 / 手动模式会接管风扇转速：调低前请确认散热与温度监控正常，"
                 "过热由硬件降频、强制关机兜底，但长期高温会加速老化"),
            xalign=0, wrap=True)
        warn.add_css_class("hw-cap")
        warn.set_opacity(0.75)
        self.append(warn)

        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sc.set_child(self.box)
        self.append(sc)

    def toast(self, text):
        # toast() 定义在 HwDash(Application) 上，不在窗口上——
        # 必须经 get_application() 找到应用对象；直接调 w.toast 必炸 AttributeError
        w = self.get_root()
        app = w.get_application() if w is not None else None
        if app is not None and hasattr(app, "toast"):
            app.toast(text)

    def _all_bios(self, _b):
        # 全部走工作线程：主线程同步 HTTP 会冻结整个 UI（风扇调速卡顿感的元凶）
        def done(res):
            ok = not isinstance(res, Exception) and res[0]
            self.toast(T("已全部交还 BIOS") if ok else
                       f"{T('失败')}：{res if isinstance(res, Exception) else res[1]}")
        run_async(lambda: API_.post("/api/fans/all-bios", {}), done)

    def _on_mode(self, n, mode):
        self._mode_pending[n] = mode
        GLib.timeout_add(600, self._flush_mode, n)

    def _flush_mode(self, n):
        mode = self._mode_pending.pop(n, None)
        if mode:
            def done(res):
                ok = not isinstance(res, Exception) and res[0]
                if not ok:
                    self.toast(f"pwm{n}: {res if isinstance(res, Exception) else res[1]}")
            run_async(lambda: API_.post("/api/fan/mode", {"n": n, "mode": mode}), done)
        return GLib.SOURCE_REMOVE

    def _on_curve(self, n, points):
        # 曲线拖动中 250ms 节流连发 —— 必须异步，否则每次拖动都卡主线程
        run_async(lambda: API_.post("/api/fan/curve", {"n": n, "points": points}))

    def _on_manual(self, n, duty):
        run_async(lambda: API_.post("/api/fan/mode", {"n": n, "mode": "manual", "duty": duty}))

    def update(self, st: dict):
        for f in st.get("fans", []):
            card = self.cards.get(f["n"])
            if card is None:
                card = FanCard(f, self._on_mode, self._on_curve, self._on_manual, self.toast)
                self.cards[f["n"]] = card
                self.box.append(card)
            else:
                card.update(f)


# ------------------------------------------------------------------ 灯光页

# ------------------------------------------------------------------ 灯光页（OpenRGB 范式）
# 交互对齐 OpenRGB GUI：
#   * 模式单选列表：选中即下发生效（不是"编辑完再按应用"）
#   * 每个模式有自己的颜色数组 / 速度 / 亮度 / 方向，参数改动 350ms 防抖后实时下发
#   * Direct（逐颗）：LED 视图上点击 / 拖拽上色，画笔大小 1/2/3 可调
#   * 配置档下拉选中即加载；可另存 / 删除

MF_SPEED, MF_DIR_LR, MF_DIR_UD, MF_DIR_HV = 0x0001, 0x0002, 0x0004, 0x0008
MF_BRIGHT, MF_SPEC, MF_RANDOM, MF_PERLED = 0x0010, 0x0020, 0x0040, 0x0100
MF_EFFECT_SPEED = 0x0200
# OpenRGB ModeDirections：LEFT/RIGHT/UP/DOWN/HORIZONTAL/VERTICAL
_DIR_LABEL = {0: "向左", 1: "向右", 2: "向上", 3: "向下", 4: "水平", 5: "垂直"}


def _flags_summary(flags: int) -> str:
    parts = []
    if flags & (MF_SPEED | MF_EFFECT_SPEED):
        parts.append("速度")
    if flags & MF_BRIGHT:
        parts.append("亮度")
    if flags & MF_SPEC:
        parts.append("自定义颜色")
    if flags & MF_RANDOM:
        parts.append("随机颜色")
    if flags & MF_PERLED:
        parts.append(T("逐颗颜色"))
    if flags & (MF_DIR_LR | MF_DIR_UD | MF_DIR_HV):
        parts.append(T("方向"))
    return " · ".join(parts) or T("无参数")


class LedStrip(Gtk.DrawingArea):
    """一个灯区的逐颗视图（OpenRGB 的 LED 块条）：点击 / 拖拽用画笔上色。"""

    def __init__(self, page, zone: dict, base: int):
        super().__init__()
        self.page = page
        self.zone = zone
        # base 必须是本区在全设备灯序里的累计起始偏移。zone["leds_min"] 是
        # "最小灯数"（OpenRGB 语义），绝大多数主板它不等于偏移——拿它当
        # base 会让第 2 条开始的灯区点画到别的区上（点了没反应）。
        self.base = max(0, int(base))
        self.count = max(1, zone["leds_count"])
        self.set_size_request(-1, 34)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self._dragging = False

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", self._press)
        click.connect("released", self._release)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.add_controller(motion)

    # -- 几何：块宽自适应，居中排布 ----------------------------------------
    def _geom(self, w):
        gap = 2.0
        bw = (w - 8 - gap * (self.count - 1)) / self.count
        bw = max(2.0, min(22.0, bw))
        total = bw * self.count + gap * (self.count - 1)
        return (w - total) / 2, bw, gap

    def _led_at(self, x, w):
        x0, bw, gap = self._geom(w)
        i = int((x - x0 + 0.5) / (bw + gap))
        return i if 0 <= i < self.count else None

    def _draw(self, area, cr, w, h):
        page = self.page
        cols = page.led_colors
        fg = self.get_color()
        x0, bw, gap = self._geom(w)
        bh = min(h - 6, 26.0)
        y0 = (h - bh) / 2
        cr.set_line_width(1)
        for i in range(self.count):
            c = cols[self.base + i] if self.base + i < len(cols) else ""
            _rounded_path(cr, x0 + i * (bw + gap), y0, bw, bh, min(4.0, bw / 2))
            if c:
                r, g, b = _hex_rgb(c)
                cr.set_source_rgb(r, g, b)
                cr.fill_preserve()
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
                cr.fill_preserve()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.22)
            cr.stroke()

    # -- 交互 --------------------------------------------------------------
    def _apply_at(self, i):
        page = self.page
        rad = page.brush_size - 1
        col = page.brush_color
        changed = False
        for j in range(max(0, i - rad), min(self.count, i + rad + 1)):
            if page.led_colors[self.base + j] != col:
                page.led_colors[self.base + j] = col
                changed = True
        if changed:
            self.queue_draw()
            page.schedule_flush()

    def _press(self, gesture, _n, x, y):
        self._dragging = True
        i = self._led_at(x, self.get_width())
        if i is not None:
            self._apply_at(i)
        self.grab_focus()

    def _release(self, *_a):
        self._dragging = False
        self.page.flush_now()

    def _motion(self, ctl, x, y):
        if not self._dragging:
            return
        i = self._led_at(x, self.get_width())
        if i is not None:
            self._apply_at(i)


class ColorPage(Gtk.Box):
    """OpenRGB 式灯光页：设备 / 配置档 → 逐颗 LED 编辑 → 模式单选列表 + 参数。"""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.devices: list[dict] = []
        self.dev_idx = 0
        self.profiles: list[str] = []
        self.led_colors: list[str] = []
        self.brush_color = "FF4D00"
        self.brush_size = 1
        self._active_mode = 0
        self._cur_mode_idx = 0
        self._refreshing = False
        self._flush_pending = False
        self._last_flush = 0.0
        self._param_timer = None
        self._building = False
        self._zone_strips: list[LedStrip] = []

        # 滚动容器必须在外层（vexpand 吃满可用高度），clamp 在里面限宽；
        # 反过来 clamp 在外会让内容以自然高度撑爆窗口（高度溢出屏幕）。
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(sc)
        clamp = Adw.Clamp(maximum_size=1080, tightening_threshold=1000)
        sc.set_child(clamp)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(self.box)

        self.banner = Adw.Banner(revealed=False)
        self.box.append(self.banner)
        self._build()
        self.refresh()

    # -------------------------------------------------------------- 顶部
    def _card(self):
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        c.add_css_class("card")
        c.add_css_class("hw-card-pad")
        return c

    def _build(self):
        # ---- 设备 / 配置档
        devcard = self._card()
        self.box.append(devcard)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.dev_name = Gtk.Label(label="灯光设备", xalign=0)
        self.dev_name.add_css_class("heading")
        head.append(self.dev_name)
        self.dev_meta = Gtk.Label(label="", xalign=0, hexpand=True, ellipsize=3)
        self.dev_meta.add_css_class("hw-cap")
        head.append(self.dev_meta)
        rb = Gtk.Button(icon_name="view-refresh-symbolic")
        rb.add_css_class("flat")
        rb.set_tooltip_text("重新扫描（设备与配置档）")
        rb.connect("clicked", lambda *_: self.refresh(force=True))
        head.append(rb)
        devcard.append(head)

        prow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prow.append(Gtk.Label(label=T("配置档"), xalign=0))
        self.profile_dd = Gtk.DropDown.new_from_strings(["（无）"])
        self.profile_dd.set_size_request(180, -1)
        self.profile_dd.set_hexpand(True)
        self.profile_dd.connect("notify::selected", self._on_profile_selected)
        prow.append(self.profile_dd)
        for icon, tip, fn in ((T("document-save-symbolic"), T("把当前状态另存为配置档"), self._profile_save),
                              (T("user-trash-symbolic"), T("删除选中的配置档"), self._profile_delete)):
            b = Gtk.Button(icon_name=icon)
            b.add_css_class("flat")
            b.set_tooltip_text(tip)
            b.connect("clicked", fn)
            prow.append(b)
        devcard.append(prow)
        self._profile_loading = False

        # ---- 逐颗 LED
        ledcard = self._card()
        self.box.append(ledcard)
        lh = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lt = Gtk.Label(label=T("灯珠"), xalign=0)
        lt.add_css_class("heading")
        lh.append(lt)
        lsub = Gtk.Label(label=T("点击或拖拽灯珠上色（自动切换到 Direct 模式）"),
                         xalign=0, hexpand=True, ellipsize=3)
        lsub.add_css_class("hw-cap")
        lh.append(lsub)
        ledcard.append(lh)

        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tools.append(Gtk.Label(label=T("画笔"), xalign=0))
        self.brush_btn = Gtk.ColorDialogButton()
        rgba = Gdk.RGBA()
        rgba.parse(f"#{self.brush_color}")
        self.brush_btn.set_rgba(rgba)
        self.brush_btn.set_dialog(Gtk.ColorDialog())
        self.brush_btn.set_valign(Gtk.Align.CENTER)
        self.brush_btn.connect("notify::rgba",
                               lambda b, *_: setattr(self, "brush_color",
                                                     _rgba_hex(b.get_rgba()).lstrip("#")))
        tools.append(self.brush_btn)
        for hx in ("FFFFFF", "FF3B30", "FF9500", "FFD60A",
                   "34C759", "00C7BE", "0A84FF", "BF5AF2", "000000"):
            sw = Gtk.Button()
            sw.set_size_request(26, 26)
            sw.set_child(Gtk.DrawingArea())
            sw.get_child().set_size_request(18, 18)
            sw.get_child().set_draw_func(lambda a, cr, w, h, hx=hx: self._swatch(cr, w, h, hx))
            sw.set_tooltip_text(hx)
            sw.connect("clicked", lambda _b, hx=hx: self._set_brush(hx))
            tools.append(sw)
        size_l = Gtk.Label(label=T("粗细"), xalign=0, margin_start=6)
        tools.append(size_l)
        self.size_tg = Adw.ToggleGroup()
        for s in ("1", "2", "3"):
            t = Adw.Toggle(label=s)
            self.size_tg.add(t)
        self.size_tg.set_active(0)
        self.size_tg.connect("notify::active", self._on_size_changed)
        tools.append(self.size_tg)
        for label, fn, tip in ((T("填充"), self._fill_brush, T("所有灯珠 = 画笔色")),
                               (T("清除"), self._fill_black, T("所有灯珠熄灭")),
                               (T("彩虹"), self._fill_rainbow, T("全设备彩虹渐变"))):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.set_tooltip_text(tip)
            b.set_valign(Gtk.Align.CENTER)
            b.connect("clicked", fn)
            tools.append(b)
        ledcard.append(tools)

        self.zones_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        ledcard.append(self.zones_box)

        # ---- 模式
        modecard = self._card()
        self.box.append(modecard)
        mh = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        mt = Gtk.Label(label=T("模式"), xalign=0)
        mt.add_css_class("heading")
        mh.append(mt)
        msub = Gtk.Label(label=T("选中即生效（OpenRGB 语义）"), xalign=0, hexpand=True, ellipsize=3)
        msub.add_css_class("hw-cap")
        mh.append(msub)
        modecard.append(mh)

        self.mode_list = Gtk.ListBox()
        self.mode_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.mode_list.add_css_class("boxed-list")
        self.mode_list.connect("row-activated", self._on_mode_row)
        modecard.append(self.mode_list)

        self.param_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        modecard.append(self.param_box)

    @staticmethod
    def _swatch(cr, w, h, hx):
        r, g, b = _hex_rgb(hx)
        cr.set_source_rgb(r, g, b)
        _rounded_path(cr, 0.5, 0.5, w - 1, h - 1, 5)
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.set_line_width(1)
        _rounded_path(cr, 0.5, 0.5, w - 1, h - 1, 5)
        cr.stroke()

    def _set_brush(self, hx: str):
        self.brush_color = hx
        rgba = Gdk.RGBA()
        rgba.parse(hx)
        self.brush_btn.set_rgba(rgba)

    def _on_size_changed(self, *_):
        self.brush_size = self.size_tg.get_active() + 1

    # -------------------------------------------------------------- 刷新
    def toast(self, text):
        # toast() 定义在 HwDash(Application) 上，不在窗口上——
        # 必须经 get_application() 找到应用对象；直接调 w.toast 必炸 AttributeError
        w = self.get_root()
        app = w.get_application() if w is not None else None
        if app is not None and hasattr(app, "toast"):
            app.toast(text)

    def refresh(self, force: bool = False):
        if self._refreshing:
            return
        self._refreshing = True
        self.banner.set_title("正在扫描灯光设备…")
        self.banner.set_revealed(True)
        run_async(lambda: API_.rgb(refresh=force), self._on_rgb)

    def _on_rgb(self, info):
        self._refreshing = False
        if isinstance(info, Exception):
            info = {"ok": False, "error": str(info)}
        if not info.get("ok"):
            self.banner.set_title(f"{T('灯光不可用')}：{info.get('error', T('未知原因'))}")
            self.banner.set_revealed(True)
            return
        self.banner.set_revealed(False)
        self.devices = info.get("devices", [])
        self.profiles = info.get("profiles", [])
        if not self.devices:
            self.banner.set_title(T("没有可用的灯光设备"))
            self.banner.set_revealed(True)
            return
        self.dev_idx = min(self.dev_idx, len(self.devices) - 1)
        self._apply_device(rebuild=True)

    def _apply_device(self, rebuild: bool):
        dev = self.devices[self.dev_idx]
        self.dev_name.set_text(dev.get("name", "?"))
        meta = " · ".join(x for x in (dev.get("vendor"), dev.get("description"),
                                      dev.get("location")) if x)
        self.dev_meta.set_text(meta)
        self.led_colors = list(dev.get("colors", []))

        self._building = True
        if rebuild:
            # 灯区（摘挂 + 延迟销毁，见 _drain_box 说明）
            _drop_later(_drain_box(self.zones_box))
            self._zone_strips = []
            _zone_base = 0
            for z in dev.get("zones", []):
                zb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                nm = Gtk.Label(label=z["name"], xalign=0)
                nm.add_css_class("hw-cap")
                nm.add_css_class("heading")
                nm.set_opacity(0.9)
                h.append(nm)
                hint = z.get("hint", "")
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0, hexpand=True, ellipsize=3)
                    hl.add_css_class("hw-cap")
                    hl.set_opacity(0.7)
                    h.append(hl)
                cnt = Gtk.Label(label=f"{z['leds_count']} {T('颗')}", xalign=1)
                cnt.add_css_class("hw-cap")
                cnt.set_opacity(0.7)
                h.append(cnt)
                zb.append(h)
                strip = LedStrip(self, z, _zone_base)
                _zone_base += int(z["leds_count"])
                self._zone_strips.append(strip)
                zb.append(strip)
                self.zones_box.append(zb)
            # 模式列表（摘挂 + 延迟销毁）
            _drop_later(_drain_box(self.mode_list))
            first_radio = None
            for m in dev.get("modes", []):
                row = Adw.ActionRow(title=m["name"],
                                    subtitle=_flags_summary(m["flags"]))
                chk = Gtk.CheckButton()
                if first_radio is None:
                    first_radio = chk
                else:
                    chk.set_group(first_radio)
                chk.set_valign(Gtk.Align.CENTER)
                chk.connect("toggled", self._on_radio, m["idx"])
                row.add_prefix(chk)
                # 行激活必须转发到前缀 radio（libadwaita 的设计用法）。
                # 不能写成 set_activatable_widget(row)：行激活自身 =
                # mnemonic_activate(row) → activate(row) → 再进 activate 的
                # 无限 emission 套娃，PyGObject marshal 半路即段错误
                # （_gi segfault at 30，journal 多次实证）。
                row.set_activatable_widget(chk)
                row.mode_idx = m["idx"]
                row.radio = chk
                self.mode_list.append(row)
            # 配置档下拉在 _sync_profiles 里统一重建
        self._building = False

        self._active_mode = dev.get("active_mode", 0)
        self._cur_mode_idx = self._active_mode
        self._sync_mode_rows()
        self._build_params()
        self._sync_profiles()
        for s in self._zone_strips:
            s.queue_draw()

    # -- 模式 --------------------------------------------------------------
    def _cur_device(self) -> dict:
        return self.devices[self.dev_idx]

    def _cur_mode(self) -> dict:
        modes = self._cur_device().get("modes", [])
        return modes[self._cur_mode_idx] if 0 <= self._cur_mode_idx < len(modes) else {}

    def _sync_mode_rows(self):
        row = self.mode_list.get_row_at_index(0)
        while row is not None:
            row.radio.handler_block_by_func(self._on_radio)
            row.radio.set_active(row.mode_idx == self._active_mode)
            row.radio.handler_unblock_by_func(self._on_radio)
            row = self.mode_list.get_row_at_index(row.get_index() + 1)

    def _on_radio(self, chk, idx):
        if self._building or not chk.get_active():
            return
        self._cur_mode_idx = idx
        self._active_mode = idx
        self._build_params()
        self._send_mode()

    def _on_mode_row(self, _list, row):
        row.radio.set_active(True)

    def _build_params(self):
        """重建选中模式的参数编辑区（颜色数组 / 速度 / 亮度 / 方向）。"""
        # 旧参数控件（Scale/ColorDialogButton 等）在 emission 上下文里
        # 同步销毁会触发 _gi segfault —— 摘挂后延迟一拍再释放引用
        _drop_later(_drain_box(self.param_box))
        m = self._cur_mode()
        if not m:
            lbl = Gtk.Label(label=T("无模式信息"), xalign=0)
            lbl.add_css_class("hw-cap")
            self.param_box.append(lbl)
            return
        self._building = True
        flags = m.get("flags", 0)

        # 颜色数组（Direct 等无色模式自动隐藏）
        if m.get("colors_max", 0) > 0 and flags & MF_SPEC:
            crow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            crow.append(Gtk.Label(label="颜色", xalign=0))
            flow = Gtk.FlowBox()
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_max_children_per_line(8)
            flow.set_column_spacing(6)
            flow.set_row_spacing(4)
            flow.set_hexpand(True)
            for ci, hx in enumerate(m.get("colors", [])):
                cb = Gtk.ColorDialogButton()
                rgba = Gdk.RGBA()
                rgba.parse(f"#{str(hx).lstrip('#')}")
                cb.set_rgba(rgba)
                cb.set_dialog(Gtk.ColorDialog())
                cb.set_valign(Gtk.Align.CENTER)
                cb.connect("notify::rgba",
                           lambda b, ci=ci: self._on_mode_color(ci, _rgba_hex(b.get_rgba()).lstrip("#")))
                flow.append(cb)
            crow.append(flow)
            rnd = Gtk.Button(icon_name="media-playlist-shuffle-symbolic")
            rnd.add_css_class("flat")
            rnd.set_tooltip_text("随机颜色")
            rnd.set_valign(Gtk.Align.CENTER)
            rnd.connect("clicked", self._random_colors)
            crow.append(rnd)
            self.param_box.append(crow)

        # 速度
        if flags & (MF_SPEED | MF_EFFECT_SPEED):
            self.param_box.append(self._scale_row("速度", m.get("speed", 0),
                                                  self._on_speed))
        # 亮度
        if flags & MF_BRIGHT:
            self.param_box.append(self._scale_row("亮度", self._bright_pct(m),
                                                  self._on_bright))
        # 方向
        dir_flags = flags & (MF_DIR_LR | MF_DIR_UD | MF_DIR_HV)
        if dir_flags:
            self.param_box.append(self._dir_row(dir_flags, m.get("direction", 0)))

        self._building = False

    def _scale_row(self, label, value, cb):
        h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        l = Gtk.Label(label=label, xalign=0, width_chars=4)
        h.append(l)
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        sc.set_value(max(0, min(100, value)))
        sc.set_hexpand(True)
        sc.set_draw_value(True)
        sc.set_value_pos(Gtk.PositionType.RIGHT)
        sc.connect("value-changed", cb)
        h.append(sc)
        return h

    def _dir_row(self, dir_flags, cur):
        h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        h.append(Gtk.Label(label="方向", xalign=0, width_chars=4))
        tg = Adw.ToggleGroup()
        opts = []
        if dir_flags & MF_DIR_LR:
            opts += [(0, T("向左")), (1, T("向右"))]
        if dir_flags & MF_DIR_HV:
            opts += [(4, T("水平")), (5, T("垂直"))]
        if dir_flags & MF_DIR_UD:
            opts += [(2, T("向上")), (3, T("向下"))]
        for val, name in opts:
            t = Adw.Toggle(label=name)
            tg.add(t)
        tg.set_active(0)
        for i, (val, _name) in enumerate(opts):
            if val == cur:
                tg.set_active(i)
        tg.connect("notify::active",
                   lambda g, *_: self._on_direction(opts[g.get_active()][0]))
        h.append(tg)
        return h

    # -- 模式参数回调（350ms 防抖统一下发） --------------------------------
    def _touch(self):
        if self._building:
            return
        if self._param_timer:
            GLib.source_remove(self._param_timer)
        self._param_timer = GLib.timeout_add(350, self._send_mode)

    def _on_mode_color(self, idx, hx):
        m = self._cur_mode()
        colors = [c for c in m.get("colors", [])]
        while len(colors) <= idx:
            colors.append("FFFFFF")
        colors[idx] = hx
        m["colors"] = colors
        self._touch()

    def _random_colors(self, *_):
        m = self._cur_mode()
        n = max(1, len(m.get("colors", [])) or 1)
        cols = ["{:02X}{:02X}{:02X}".format(*(self._rand_rgb() for _ in range(3)))
                for _ in range(n)]
        m["colors"] = cols
        self._build_params()
        self._touch()

    @staticmethod
    def _rand_rgb():
        import random
        return random.randint(64, 255)

    def _on_speed(self, sc):
        m = self._cur_mode()
        m["speed"] = int(sc.get_value())
        self._touch()

    def _bright_pct(self, m) -> int:
        lo, hi = m.get("bright_min", 0), m.get("bright_max", 0)
        b = m.get("bright", 0)
        if hi > lo:
            return int(round((b - lo) / (hi - lo) * 100))
        return max(0, min(100, b))

    def _on_bright(self, sc):
        m = self._cur_mode()
        lo, hi = m.get("bright_min", 0), m.get("bright_max", 0)
        pct = int(sc.get_value())
        m["bright"] = int(round(lo + (hi - lo) * pct / 100)) if hi > lo else pct
        self._touch()

    def _on_direction(self, val):
        m = self._cur_mode()
        m["direction"] = val
        self._touch()

    def _send_mode(self):
        self._param_timer = None
        m = self._cur_mode()
        if not m:
            return
        payload = {"device": self.dev_idx, "mode": m["idx"],
                   "colors": m.get("colors") or None,
                   "speed": m.get("speed") if m.get("flags", 0) & (MF_SPEED | MF_EFFECT_SPEED) else None,
                   "brightness": m.get("bright") if m.get("flags", 0) & MF_BRIGHT else None}
        def done(res):
            if isinstance(res, Exception) or not res[0]:
                msg = str(res) if isinstance(res, Exception) else res[1]
                self.toast(f"模式下发失败：{msg}")
        run_async(lambda: API_.post("/api/rgb/mode", payload), done)

    # -- 逐颗 --------------------------------------------------------------
    def schedule_flush(self):
        if self._flush_pending:
            return
        now = time.monotonic()
        delay = max(0, 0.15 - (now - self._last_flush))
        self._flush_pending = True
        GLib.timeout_add(int(delay * 1000) + 5, self._do_flush)

    def _do_flush(self):
        self._flush_pending = False
        self.flush_now()
        return GLib.SOURCE_REMOVE

    def flush_now(self):
        self._last_flush = time.monotonic()
        cols = list(self.led_colors)
        if not cols:
            return
        run_async(lambda: API_.post("/api/rgb/leds",
                                    {"device": self.dev_idx, "colors": cols}),
                  None)

    def _fill(self, colors_fn):
        dev = self._cur_device()
        total = sum(z["leds_count"] for z in dev.get("zones", []))
        if not total:
            return
        cols = colors_fn(total)
        self.led_colors = cols
        for s in self._zone_strips:
            s.queue_draw()
        self.flush_now()

    def _fill_brush(self, *_):
        self._fill(lambda n: [self.brush_color] * n)

    def _fill_black(self, *_):
        self._fill(lambda n: ["000000"] * n)

    def _fill_rainbow(self, *_):
        import hwhw
        stops = [(0.0, "FF3D3D"), (0.25, "FFD60A"), (0.5, "34C759"),
                 (0.75, "0A84FF"), (1.0, "BF5AF2")]
        self._fill(lambda n: hwhw.gradient(stops, n).split(","))

    # -- 配置档 ------------------------------------------------------------
    def _sync_profiles(self):
        names = self.profiles or ["（无配置档）"]
        self._profile_loading = True
        model = Gtk.StringList.new(names)
        self.profile_dd.set_model(model)
        self._profile_loading = False

    def _on_profile_selected(self, dd, *_):
        if self._profile_loading or not self.profiles:
            return
        i = dd.get_selected()
        if i < 0 or i >= len(self.profiles):
            return
        name = self.profiles[i]
        def done(res):
            if isinstance(res, Exception) or not res[0]:
                msg = str(res) if isinstance(res, Exception) else res[1]
                self.toast(f"加载配置档失败：{msg}")
            else:
                self.toast(f"已加载配置档 {name}")
                self.refresh(force=True)
        run_async(lambda: API_.post("/api/rgb/profile",
                                    {"action": "load", "name": name}), done)

    def _profile_save(self, *_):
        dlg = Adw.MessageDialog(transient_for=self.get_root(),
                                heading=T("保存配置档"),
                                body=T("把当前所有设备的灯光状态保存为 OpenRGB 配置档。"))
        entry = Gtk.Entry(text="hw-dash", hexpand=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", T("取消"))
        dlg.add_response("ok", T("保存"))
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def on_resp(_d, r):
            name = entry.get_text().strip()
            if r == "ok" and name:
                def done(res):
                    ok = not isinstance(res, Exception) and res[0]
                    self.toast(T("已保存配置档") if ok else
                               f"{T('保存失败')}：{res if isinstance(res, Exception) else res[1]}")
                    if ok:
                        self.refresh(force=True)
                run_async(lambda: API_.post("/api/rgb/profile",
                                            {"action": "save", "name": name}), done)
            dlg.close()
        dlg.connect("response", on_resp)
        dlg.present()

    def _profile_delete(self, *_):
        i = self.profile_dd.get_selected()
        if not self.profiles or i < 0 or i >= len(self.profiles):
            self.toast(T("没有可删除的配置档"))
            return
        name = self.profiles[i]
        def done(res):
            ok = not isinstance(res, Exception) and res[0]
            self.toast(T("已删除") if ok else
                       f"{T('删除失败')}：{res if isinstance(res, Exception) else res[1]}")
            if ok:
                self.refresh(force=True)
        run_async(lambda: API_.post("/api/rgb/profile",
                                    {"action": "delete", "name": name}), done)



# ------------------------------------------------------------------ 应用


# ====================================================================== 页面：系统信息

class InfoPage(Gtk.Box):
    """CPU 实时频率（每核） / 内存条明细（dmidecode，含实时时钟）/ 主板 / BIOS / GPU。"""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.freq_tiles: dict[int, dict] = {}
        self._info = None
        self._loaded_info = False

        vclamp = Adw.Clamp(maximum_size=940, tightening_threshold=880)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vclamp.set_child(wrap)
        sc.set_child(vclamp)
        self.append(sc)

        # ---- 处理器
        cpu_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cpu_card.add_css_class("card")
        cpu_card.add_css_class("hw-card-pad")
        h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t = Gtk.Label(label=T("处理器"), xalign=0)
        t.add_css_class("heading")
        h.append(t)
        self.cpu_model_lbl = Gtk.Label(label="…", xalign=1, hexpand=True, ellipsize=1)
        self.cpu_model_lbl.add_css_class("hw-cap")
        h.append(self.cpu_model_lbl)
        cpu_card.append(h)

        self.freq_grid = Gtk.FlowBox()
        self.freq_grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.freq_grid.set_max_children_per_line(8)
        self.freq_grid.set_min_children_per_line(4)
        self.freq_grid.set_column_spacing(6)
        self.freq_grid.set_row_spacing(6)
        cpu_card.append(self.freq_grid)
        self.freq_meta = Gtk.Label(xalign=0)
        self.freq_meta.add_css_class("hw-cap")
        cpu_card.append(self.freq_meta)
        wrap.append(cpu_card)

        # ---- 实时占用（CPU / 内存 / GPU，全部尽力而为）
        live_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        live_card.add_css_class("card")
        live_card.add_css_class("hw-card-pad")
        hl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tl = Gtk.Label(label=T("实时占用"), xalign=0)
        tl.add_css_class("heading")
        hl.append(tl)
        self.live_meta = Gtk.Label(label="", xalign=1, hexpand=True)
        self.live_meta.add_css_class("hw-cap")
        hl.append(self.live_meta)
        live_card.append(hl)

        def _bar_row(title: str):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            lbl = Gtk.Label(label=f"{T(title)} —", xalign=0)
            lbl.add_css_class("hw-cap")
            box.append(lbl)
            bar = Gtk.ProgressBar()
            bar.set_show_text(False)
            box.append(bar)
            live_card.append(box)
            return lbl, bar

        self.cpu_lbl, self.cpu_bar = _bar_row("CPU 利用率")
        self.mem_lbl, self.mem_bar = _bar_row("内存")
        self.gpu_list = Gtk.ListBox()
        self.gpu_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.gpu_list.add_css_class("boxed-list")
        self.gpu_list.set_visible(False)
        live_card.append(self.gpu_list)
        self._gpu_rows: dict[str, Gtk.Label] = {}
        self._live_first = True
        wrap.append(live_card)

        # ---- 内存
        mem_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mem_card.add_css_class("card")
        mem_card.add_css_class("hw-card-pad")
        h2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t2 = Gtk.Label(label=T("内存"), xalign=0)
        t2.add_css_class("heading")
        h2.append(t2)
        self.mem_total_lbl = Gtk.Label(label="…", xalign=1, hexpand=True)
        self.mem_total_lbl.add_css_class("hw-cap")
        h2.append(self.mem_total_lbl)
        mem_card.append(h2)
        self.mem_list = Gtk.ListBox()
        self.mem_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.mem_list.add_css_class("boxed-list")
        mem_card.append(self.mem_list)
        wrap.append(mem_card)

        # ---- 主板 / BIOS / GPU
        sys_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sys_card.add_css_class("card")
        sys_card.add_css_class("hw-card-pad")
        t3 = Gtk.Label(label="主板 / BIOS / GPU", xalign=0)
        t3.add_css_class("heading")
        sys_card.append(t3)
        self.sys_list = Gtk.ListBox()
        self.sys_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.sys_list.add_css_class("boxed-list")
        sys_card.append(self.sys_list)
        wrap.append(sys_card)

    # -- 信息（静态，一次） -------------------------------------------------
    def _ensure_info(self):
        if self._loaded_info:
            return
        self._loaded_info = True
        run_async(lambda: API_._get("/api/info"), self._on_info)

    def _on_info(self, res):
        if isinstance(res, Exception) or not isinstance(res, dict):
            return
        self._info = res
        self.cpu_model_lbl.set_text(res.get("cpu_model") or "未知")
        self.cpu_model_lbl.set_tooltip_text(res.get("cpu_model") or "")

        dimms = res.get("dimms") or []
        total = res.get("total_mem_gb") or 0
        self.mem_total_lbl.set_text(
            f"总容量 {total} GB · {len(dimms)} 根" if dimms else "未读出内存信息")
        for d in dimms:
            bits = [d.get("type", "?")]
            if d.get("mtps"):
                bits.append(f"{d['mtps']} MT/s")
                bits.append(f"实时时钟 {d.get('clock_mhz', 0)} MHz")
            if d.get("part"):
                bits.append(d["part"])
            row = Adw.ActionRow(
                title=f"{d.get('locator', '?')} · {d.get('manufacturer', '?')} {d.get('size', '')}",
                subtitle=" · ".join(bits))
            self.mem_list.append(row)

        board = res.get("board") or {}
        bios = res.get("bios") or {}
        if board:
            self.sys_list.append(Adw.ActionRow(
                title=board.get("name", "?"),
                subtitle=f"主板 · {board.get('vendor', '')} {board.get('version', '')}".strip()))
        if bios:
            self.sys_list.append(Adw.ActionRow(
                title=f"BIOS {bios.get('version', '?')}",
                subtitle=f"{bios.get('vendor', '')} · {bios.get('date', '')}".strip()))
        for g in res.get("gpu") or []:
            m = re.search(r"\]:\s*(.+?)\s*\[", g)
            name = m.group(1) if m else g
            self.sys_list.append(Adw.ActionRow(title=name, subtitle="GPU · " + g.split(":", 1)[0]))

    # -- 实时频率 -----------------------------------------------------------
    def _update_live(self, st: dict):
        """实时占用：CPU 利用率 / 内存 / GPU（NVIDIA · AMD · Intel，缺啥显示啥）。"""
        live = st.get("live") or {}

        def pct_txt(v):
            return f"{v:.0f}%" if isinstance(v, (int, float)) else "—"

        cu = live.get("cpu_util")
        if isinstance(cu, (int, float)):
            self.cpu_bar.set_fraction(max(0.0, min(1.0, cu / 100.0)))
            self.cpu_lbl.set_text(f"{T('CPU 利用率')}　{cu:.0f}%")
        mem = live.get("mem")
        if isinstance(mem, dict) and mem.get("total_mb"):
            self.mem_bar.set_fraction(max(0.0, min(1.0, mem["used_mb"] / mem["total_mb"])))
            self.mem_lbl.set_text(f"{T('内存')}　{mem['used_mb']} / {mem['total_mb']} MB"
                                  f"（{mem['percent']:.0f}%）")
        gpus = live.get("gpus") or []
        if not gpus:
            self.gpu_list.set_visible(False)
            self.live_meta.set_text(T("未检测到独显/核显遥测"))
            return
        self.gpu_list.set_visible(True)
        seen = set()
        for g in gpus:
            name = str(g.get("name") or "GPU")[:28]
            seen.add(name)
            parts = [name]
            if g.get("util_percent") is not None:
                parts.append(f"{T('占用')} {g['util_percent']:.0f}%")
            if g.get("core_mhz") is not None:
                parts.append(f"{T('核心')} {g['core_mhz']:.0f} MHz")
            if g.get("mem_mhz") is not None:
                parts.append(f"{T('显存')} {g['mem_mhz']:.0f} MHz")
            if g.get("vram_total_mb"):
                parts.append(f"{T('显存')} {g.get('vram_used_mb') or 0}/{g['vram_total_mb']} MB")
            if g.get("temp") is not None:
                parts.append(f"{g['temp']:.0f}°C")
            row = self._gpu_rows.get(name)
            if row is None:
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                lbl = Gtk.Label(label=" · ".join(parts), xalign=0, ellipsize=3, hexpand=True)
                lbl.add_css_class("hw-cap")
                box.append(lbl)
                self.gpu_list.append(box)
                self._gpu_rows[name] = lbl
            else:
                row.set_text(" · ".join(parts))
        for name in list(self._gpu_rows):
            if name not in seen:
                self._gpu_rows.pop(name)

    def update(self, st: dict):
        self._ensure_info()
        self._update_live(st)
        freqs = st.get("freqs") or []
        if not freqs:
            self.freq_meta.set_text(T("读不到 cpufreq 频率"))
            return
        if not self.freq_tiles:
            for i in range(len(freqs)):
                tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                tile.add_css_class("card")
                tile.add_css_class("hw-tile")
                cap = Gtk.Label(label=f"CPU {i}", xalign=0)
                cap.add_css_class("hw-cap")
                tile.append(cap)
                val = Gtk.Label(label="—", xalign=0)
                val.add_css_class("hw-value")
                tile.append(val)
                self.freq_tiles[i] = val
                self.freq_grid.append(tile)
        for i, f in enumerate(freqs):
            t = self.freq_tiles.get(i)
            if t:
                t.set_text(f"{f / 1000:.2f} GHz")
        self.freq_meta.set_text(
            f"实时频率 · 最低 {min(freqs) / 1000:.2f} · 平均 {sum(freqs) / len(freqs) / 1000:.2f} · "
            f"最高 {max(freqs) / 1000:.2f} GHz")


class HwDash(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.hwdash.Panel",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.pages: dict[str, Gtk.Widget] = {}

    # -- 首次启动免责声明 ---------------------------------------------------
    ACK_PATH = os.path.expanduser("~/.config/hw-dash/disclaimer-ack")

    def _maybe_disclaimer(self, win):
        """首次启动弹免责声明；确认后写入 ack 文件，之后不再打扰。"""
        try:
            if os.path.exists(self.ACK_PATH):
                return
        except Exception:
            return
        dlg = Adw.MessageDialog(
            heading=T("使用前请阅读"),
            body=T("本面板可以接管风扇转速、修改灯光状态。请知悉：\n\n"
                 "· 风扇曲线 / 手动模式会接管散热 —— 请确保调速合理、温度监控正常；"
                 "过热时硬件会降频甚至强制关机保护，但长期高温会加速老化\n"
                 "· 本软件按现状提供，作者不对因使用或误用造成的任何硬件或数据问题负责\n"
                 "· 电压 / 温度 / 频率等只读功能没有风险"),
        )
        dlg.add_response("quit", T("退出"))
        dlg.add_response("ack", T("我已了解，继续使用"))
        dlg.set_response_appearance("ack", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_close_response("ack")
        dlg.set_transient_for(win)

        def on_resp(_d, resp):
            if resp == "quit":
                os._exit(0)
            try:
                os.makedirs(os.path.dirname(self.ACK_PATH), exist_ok=True)
                with open(self.ACK_PATH, "w", encoding="utf-8") as fh:
                    fh.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
            except OSError:
                pass

        dlg.connect("response", on_resp)
        dlg.present()

    def _build_pages(self, stack):
        for name, title, icon in (("temp", T("温度"), "temperature-symbolic"),
                                  ("fan", T("风扇"), "fan-symbolic"),
                                  ("volt", T("电压"), "speedometer-symbolic"),
                                  ("info", T("信息"), "computer-symbolic"),
                                  ("color", T("灯光"), "applications-graphics-symbolic")):
            page = {"temp": TempPage, "fan": FanPage, "volt": VoltPage,
                    "info": InfoPage, "color": ColorPage}[name]()
            page.set_margin_top(12)
            page.set_margin_bottom(12)
            page.set_margin_start(12)
            page.set_margin_end(12)
            stack.add_titled_with_icon(page, name, title, icon)
            self.pages[name] = page

    def _toggle_lang(self, *_a):
        set_lang("en" if current_lang() == "zh" else "zh")
        if getattr(self, "_lang_btn", None):
            self._lang_btn.set_label(_lang_btn_label())
        stack = self.stack
        for name in list(self.pages):
            p = self.pages.pop(name)
            stack.remove(p)
        self._build_pages(stack)
        stack.set_visible_child(self.pages["temp"])
        win = getattr(self, "win", None)
        if win is not None:
            win.set_title(T("硬件面板"))
        try:
            self.pages["color"].refresh()
        except Exception:
            pass

    def do_activate(self):
        GLib.set_prgname("hwdash")
        win = Adw.ApplicationWindow(application=self, title=T("硬件面板"))
        win.set_icon_name("org.hwdash.Panel")
        self._maybe_disclaimer(win)
        # 默认窗口 = 主屏的 72%（宽高比与分辨率天然一致）；最小尺寸也按屏幕
        # 收缩，任何分辨率下都完整可见。GTK 窗口本身支持拖边自由缩放。
        geo = None
        mons = Gdk.Display.get_default().get_monitors()
        for i in range(mons.get_n_items()):
            m = mons.get_item(i)
            g = m.get_geometry()
            if geo is None or g.width * g.height > geo.width * geo.height:
                geo = g
        if geo and geo.width >= 640 and geo.height >= 480:
            dw, dh = int(geo.width * 0.72), int(geo.height * 0.72)
            win.set_default_size(dw, dh)
            win.set_size_request(min(760, dw), min(520, dh))
        else:
            win.set_default_size(1080, 760)
            win.set_size_request(760, 520)

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        overlay = Adw.ToastOverlay()
        win.set_content(overlay)
        self.toast_overlay = overlay

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        overlay.set_child(root)

        self.banner = Adw.Banner(revealed=False)
        root.append(self.banner)

        tv = Adw.ToolbarView()
        root.append(tv)

        header = Adw.HeaderBar()
        stack = Adw.ViewStack(vexpand=True)
        self.stack = stack
        self._build_pages(stack)
        stack.connect("notify::visible-child", self._stack_changed)

        switch = Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switch)
        lang_btn = Gtk.Button(label=_lang_btn_label())
        lang_btn.add_css_class("flat")
        lang_btn.set_tooltip_text("切换界面语言 / Switch UI language")
        lang_btn.connect("clicked", self._toggle_lang)
        header.pack_end(lang_btn)
        self._lang_btn = lang_btn
        tv.add_top_bar(header)
        tv.set_content(stack)

        self.win = win
        win.present()

        threading.Thread(target=self._poll_worker, daemon=True).start()

        if SNAPSHOT_DIR:
            GLib.timeout_add(3000, self._snapshot_seq)

    def _stack_changed(self, stack, _ps):
        if stack.get_visible_child() is self.pages.get("color"):
            GLib.timeout_add(150, lambda: (self.pages["color"].refresh(), False)[1])

    def toast(self, text: str):
        self.toast_overlay.add_toast(Adw.Toast.new(text))

    # -- 轮询 --------------------------------------------------------------
    def _poll_worker(self):
        while True:
            st = API_.state()
            GLib.idle_add(self._apply_state, st)
            time.sleep(POLL_SEC)

    def _apply_state(self, st):
        if st is None:
            return GLib.SOURCE_REMOVE
        if API_.online:
            self.banner.set_revealed(False)
        else:
            self.banner.set_title(
                T("守护进程未运行 —— 只读模式（风扇与灯光不可调）。")
                + T("启动：sudo systemctl start hwdashd"))
            self.banner.set_revealed(True)
        self.pages["temp"].update(st)
        self.pages["fan"].update(st)
        self.pages["volt"].update(st)
        self.pages["info"].update(st)
        return GLib.SOURCE_REMOVE

    # -- 自截图（无头自检用） ----------------------------------------------
    def _snapshot_seq(self):
        self._shots = ["temp", "fan", "volt", "info", "color"]
        self._shot_i = -1
        self._seed_hist()
        GLib.idle_add(self._next_shot)
        return GLib.SOURCE_REMOVE

    def _seed_hist(self):
        """自检时给温度曲线与电压曲线预填 5 分钟合成历史。"""
        page = self.pages["temp"]
        vh = self.pages["volt"].hist["vcore"]
        now0 = time.time()
        for k in range(160):
            vh.append((now0 - (160 - k) * 2,
                       round(1.02 + 0.06 * math.sin(k / 18.0), 4)))
        st = API_._last_state or {}
        sensors = st.get("sensors", [])[:3]
        now = time.time()
        for i, s in enumerate(sensors):
            base = (38, 30, 34)[i % 3]
            amp = (9, 2, 4)[i % 3]
            h = page.hist.setdefault(s["key"], deque(maxlen=2200))
            for k in range(160):
                t = now - (160 - k) * 2
                v = base + amp * math.sin(k / 22.0) + (i + 1) * 0.8
                h.append((t, round(v, 1)))

    def _next_shot(self, *_a):
        self._shot_i += 1
        if self._shot_i >= len(self._shots):
            print("[snapshot] 完成，退出")
            self.quit()
            return GLib.SOURCE_REMOVE
        name = self._shots[self._shot_i]
        if name == "fan" and API_.online:
            # 临时把 pwm3 切到曲线模式，让编辑器出现在截图里；截完交还 BIOS
            API_.post("/api/fan/mode", {"n": 3, "mode": "curve"})
            GLib.timeout_add(2600, lambda: (self._show_and_shoot(name), False)[1])
            return GLib.SOURCE_REMOVE
        self._show_and_shoot(name)
        return GLib.SOURCE_REMOVE

    def _show_and_shoot(self, name):
        self.stack.set_visible_child(self.pages[name])
        GLib.timeout_add(1400, self._shoot, name)

    def _shoot(self, name):
        try:
            # 截整窗而不是单页：页面控件不画背景，单截会丢主题底色
            _render_widget_png(self.win,
                               os.path.join(SNAPSHOT_DIR, f"{name}.png"), 1080, 760)
            print(f"[snapshot] {name}: OK")
        except Exception as e:  # noqa: BLE001
            print(f"[snapshot] {name}: {type(e).__name__}: {e}")
        if name == "fan" and API_.online:
            API_.post("/api/fan/mode", {"n": 3, "mode": "bios"})
        GLib.idle_add(self._next_shot)
        return GLib.SOURCE_REMOVE


def _render_widget_png(widget: Gtk.Widget, path: str, fallback_w: int, fallback_h: int):
    """把一个控件渲染成 PNG（Gsk.CairoRenderer -> cairo surface -> PNG）。"""
    paintable = Gtk.WidgetPaintable.new(widget)
    w = paintable.get_intrinsic_width() or fallback_w
    h = paintable.get_intrinsic_height() or fallback_h
    w = max(320, min(int(w), 1600))
    h = max(240, min(int(h), 2600))

    snap = Gtk.Snapshot()
    paintable.snapshot(snap, float(w), float(h))
    node = snap.to_node()
    if node is None:
        raise RuntimeError("Gtk.Snapshot.to_node() 返回 None")

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    # Gsk.RenderNode.draw 是官方提供的"把渲染节点画到任意 cairo 上下文"的
    # 调试接口（gsk_render_node_draw），比 Gsk.CairoRenderer 简单且 introspect 可用
    node.draw(cr)
    surface.write_to_png(path)


def main():
    global SNAPSHOT_DIR
    if "--snapshot" in sys.argv:
        SNAPSHOT_DIR = sys.argv[sys.argv.index("--snapshot") + 1]
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    Adw.init()
    GLib.set_application_name("硬件面板")
    if SNAPSHOT_DIR:
        # 无头自检时强制深色（与用户桌面一致，且曲线配色按深底设计）
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    app = HwDash()
    sys.exit(app.run([]))




# ====================================================================== 页面：电压

class VoltPage(Gtk.Box):
    """每核心电压（MSR 0x198）+ VCore 曲线 + 主板电压轨道（voltmon 同源）。"""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.hist: dict[str, deque] = {"vcore": deque(maxlen=2200)}
        self.tiles: dict[int, dict] = {}
        self.rail_rows: dict[str, dict] = {}
        self._cur_max_cpu: int | None = None
        self._tiles_built = False

        # ---- 卡 1：每核心电压瓷砖
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        card.add_css_class("hw-card-pad")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t = Gtk.Label(label="CPU 每核心电压", xalign=0)
        t.add_css_class("heading")
        head.append(t)
        cap = Gtk.Label(label="MSR 0x198 · IA32_PERF_STATUS", xalign=1, hexpand=True)
        cap.add_css_class("hw-cap")
        head.append(cap)
        self.max_lbl = Gtk.Label(label="—", xalign=1)
        self.max_lbl.add_css_class("hw-value-big")
        head.append(self.max_lbl)
        card.append(head)

        self.core_grid = Gtk.FlowBox()
        self.core_grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.core_grid.set_max_children_per_line(8)
        self.core_grid.set_min_children_per_line(4)
        self.core_grid.set_column_spacing(6)
        self.core_grid.set_row_spacing(6)
        card.append(self.core_grid)

        self.vmeta = Gtk.Label(xalign=0, wrap=True)
        self.vmeta.add_css_class("hw-cap")
        card.append(self.vmeta)

        # ---- 卡 2：VCore 曲线
        card2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card2.add_css_class("card")
        card2.add_css_class("hw-card-pad")
        h2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t2 = Gtk.Label(label="核心电压曲线（最高值）", xalign=0)
        t2.add_css_class("heading")
        h2.append(t2)
        self.now_lbl = Gtk.Label(label="—", xalign=1, hexpand=True)
        self.now_lbl.add_css_class("hw-value")
        h2.append(self.now_lbl)
        card2.append(h2)
        self.chart = TempChart(self.hist)
        self.chart.visible = {"vcore"}
        self.chart.colors = {"vcore": PALETTE[4]}
        self.chart.window = 300
        self.chart.unit = " V"
        self.chart.y_step = 0.1
        self.chart.y_margin = 0.04
        self.chart.y_floor = 0.6
        self.chart.y_ceil = 1.3
        card2.append(self.chart)

        # ---- 轨道列表
        self.rail_group = group = Adw.PreferencesGroup(
            title=T("主板电压轨道"),
            description=T("主板 Super-I/O ADC · 与基准测试台同源 · 报警位可能不可信"))
        self.rail_list = Gtk.ListBox()
        self.rail_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.rail_list.add_css_class("boxed-list")
        group.add(self.rail_list)

        # 整页限宽 + 滚动容器
        vclamp = Adw.Clamp(maximum_size=940, tightening_threshold=880)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        wrap.append(card)
        wrap.append(card2)
        wrap.append(group)
        vclamp.set_child(wrap)
        sc.set_child(vclamp)
        self.append(sc)

    # -- 每核心 ------------------------------------------------------------
    def _build_tiles(self, cores):
        for c in cores:
            n = c["cpu"]
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            tile.add_css_class("card")
            tile.add_css_class("hw-tile")
            cap = Gtk.Label(label=f"CPU {n}", xalign=0)
            cap.add_css_class("hw-cap")
            tile.append(cap)
            val = Gtk.Label(label="—", xalign=0)
            val.add_css_class("hw-value")
            tile.append(val)
            self.tiles[n] = {"tile": tile, "val": val}
            self.core_grid.append(tile)
        self._tiles_built = True

    def _update_cores(self, cores, summary):
        if not cores:
            self.max_lbl.set_text("—")
            err = summary.get("error")
            self.vmeta.set_text(f"{T('每核心电压不可用')}：{err}" if err else T("没有可用的 MSR 读数"))
            return
        if not self._tiles_built:
            self._build_tiles(cores)
        vmax = max(cores, key=lambda c: c["v"])
        vmin = min(cores, key=lambda c: c["v"])
        new_max_cpu = vmax["cpu"]
        for c in cores:
            tile = self.tiles.get(c["cpu"])
            if not tile:
                continue
            tile["val"].set_text(f"{c['v']:.3f}")
            val = tile["val"]
            is_max = c["cpu"] == new_max_cpu
            if is_max:
                val.add_css_class("hw-vmax")
            else:
                val.remove_css_class("hw-vmax")
        if self._cur_max_cpu is not None and self._cur_max_cpu != new_max_cpu:
            old = self.tiles.get(self._cur_max_cpu)
            if old:
                old["val"].remove_css_class("hw-vmax")
        self._cur_max_cpu = new_max_cpu
        self.max_lbl.set_text(f"{vmax['v']:.3f} V")
        parts = [f"平均 {summary.get('avg', 0):.3f} V",
                 f"最低 {vmin['v']:.3f}（CPU {vmin['cpu']}）",
                 f"最高 {vmax['v']:.3f}（CPU {vmax['cpu']}）"]
        extra = []
        for c in cores:
            if c.get("min_v") is not None and (c["max_v"] - c["min_v"]) > 0.05:
                extra.append(f"CPU {c['cpu']} {c['min_v']:.2f}–{c['max_v']:.2f}")
        if extra:
            parts.append("会话波动：" + ", ".join(extra[:4]))
        self.vmeta.set_text("  ·  ".join(parts))
        # 曲线：取每核最高值
        self.hist["vcore"].append((time.time(), vmax["v"]))
        self.now_lbl.set_text(f"{vmax['v']:.3f} V")
        self.chart.queue_draw()

    # -- 轨道 --------------------------------------------------------------
    def _rail_row(self, r):
        key = f"{r['chip']}:{r['channel']}"
        row = Adw.ActionRow(title=r["label"] or r["channel"],
                            subtitle=f"{r['chip']} · {r['channel']}")
        val = Gtk.Label(label="—", xalign=1)
        val.add_css_class("hw-value")
        row.add_suffix(val)
        self.rail_rows[key] = {"row": row, "val": val, "cls": None}
        return row

    def _update_rails(self, rails):
        for r in rails:
            key = f"{r['chip']}:{r['channel']}"
            entry = self.rail_rows.get(key)
            if entry is None:
                self.rail_list.append(self._rail_row(r))
                entry = self.rail_rows[key]
            v = r.get("v")
            lbl = entry["val"]
            if v is None or not r.get("sane", True):
                lbl.set_text("—")
                continue
            lbl.set_text(f"{v:.2f} V")
            bits = []
            if r.get("session_min") is not None:
                bits.append(f"会话 {r['session_min']:.2f}–{r['session_max']:.2f}")
            if r.get("limit_min") is not None:
                bits.append(f"限值 {r['limit_min']:.2f}–{r['limit_max']:.2f}")
            if r.get("alarm_suspect"):
                bits.append("报警位不可信")
            elif r.get("alarm"):
                bits.append("报警中")
            entry["row"].set_subtitle(
                f"{r['chip']} · {r['channel']}" + ("  —  " + " · ".join(bits) if bits else ""))

    def update(self, st: dict):
        volts = st.get("voltages") or {}
        self._update_cores(volts.get("cores") or [], volts)
        self._update_rails(volts.get("rails") or [])
        # 泛化：每核心电压仅 Intel（MSR 0x198 语义）；错误信息原样呈现，
        # 同时用实际芯片名替换硬编码描述。
        err = volts.get("error")
        chip = next((r.get("chip") for r in volts.get("rails") or [] if r.get("chip")), "")
        if err:
            self.rail_group.set_description(f"⚠ {err}")
        else:
            base = f"{chip} ADC · " if chip else T("主板 Super-I/O ADC · ")
            self.rail_group.set_description(base + T("与基准测试台同源 · 报警位可能不可信"))





if __name__ == "__main__":
    main()
