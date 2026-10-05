#!/usr/bin/env python3
"""hwdashd — 硬件面板守护进程（以 root 运行）

职责：
  * 每 2 秒采集温度、转速、占空比
  * 按用户设定的「温度 -> 占空比」曲线自动调速（曲线模式）
  * 持有 PWM 寄存器与 RGB，作为**唯一写者**（避免与 CoolerControl 之类的
    守护进程抢同一组寄存器）
  * 通过 127.0.0.1 上的小 JSON API 服务图形界面

安全设计：
  * 只监听 127.0.0.1，并校验 Host 头（防 DNS rebinding）
  * 启动时快照所有 PWM 通道，收到 SIGTERM/SIGINT 时完整还原
    （在新一代 iTE 芯片上「手动占空比」与「自动模式起始占空比」是同一个
     寄存器，不还原会永久改掉 BIOS 配的风扇曲线）
  * 任何写操作都做范围钳制
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hwhw  # noqa: E402
import openrgb_sdk  # noqa: E402

CONFIG_DIR = "/etc/hw-dash"
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
# PWM 原始值快照放在 /run（tmpfs，开机自动清空）。
# 这样同一台机器内崩溃重启会复用同一份原始值，而跨开机则重新取 BIOS 刚写好的值
# —— 绝不会把「我们自己写的曲线值」误当成原始值。
SNAPSHOT_PATH = "/run/hw-dash/pwm-snapshot.json"
BIND_HOST, BIND_PORT = "127.0.0.1", 8788
INTERVAL = 2.0
TEMP_MIN, TEMP_MAX = 10.0, 110.0

# 新通道的默认曲线：40°C 起 12%、60°C 24%、80°C 60%、90°C 满速
DEFAULT_CURVE = [[40, 30], [60, 60], [80, 155], [90, 255]]

DEFAULT_CONFIG = {
    "fans": {},          # {"1": {"mode":"bios","curve":[[40,30],[60,70],[85,255]],"duty":None}}
    "sensors": {},       # {"it8628:temp3": {"label":"CPU","group":"主板"}}
    "rgb": {},           # 记住每个灯区上次设定，便于界面回显
}


# ------------------------------------------------------------------ 配置

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o644)


# ------------------------------------------------------------------ 核心

class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = load_config()
        self.chip = hwhw.find_fan_chip()
        self.fans: list[hwhw.FanChannel] = []
        self.sensors: list[hwhw.Sensor] = []
        self.snap: dict = {}
        self.rgb_cache: dict = {}
        self.rgb_ok: bool | None = None
        self.rgb_error = ""
        # Direct 模式亮度：IT5711 对灯条区不应用 mode.brightness（OpenRGB 固有
        # 行为，见 RGBController_GigabyteRGBFusion2USB::DeviceUpdateLEDs——
        # Direct 只走 SetStripColors 原色直写），由 daemon 缩放灯条颜色实现。
        # rgb_direct_bright: 用户设定过的 Direct 亮度（未设定=255，不缩放）。
        # rgb_led_logical: 逻辑色（未缩放），保证亮度可逆、预览所见即所得。
        self.rgb_direct_bright: dict[int, int] = {}
        self.rgb_led_logical: dict[int, list[str]] = {}
        self._sdk = openrgb_sdk.OpenRGBSDK(name="hwdashd")
        self.v_mm: dict = {}     # 每逻辑 CPU 会话 min/max（电压）
        self.rail_mm: dict = {}  # 每轨道会话 min/max
        self.boot = time.time()
        self.info = self._load_info()
        self._discover(initial=True)
        # SDK 探测毫秒级，但 openrgb server 冷启动要数秒 —— 后台预热兜底
        # （openrgb-sdk.service 开机自启，正常情况下此刻端口已在监听）
        threading.Thread(target=lambda: self.rgb_probe(force=True), daemon=True).start()

    @staticmethod
    def _load_info() -> dict:
        try:
            return hwhw.sysinfo()
        except Exception as ex:  # noqa: BLE001
            print(f"[hwdashd] sysinfo 失败: {ex!r}", flush=True)
            return {"cpu_model": "", "cpu_threads": 0, "dimms": [],
                    "total_mem_gb": 0, "board": {}, "bios": {}, "gpu": []}

    # -- 发现 ------------------------------------------------------------
    def _discover(self, initial: bool = False) -> None:
        if self.chip is None or not os.path.isdir(self.chip):
            self.chip = hwhw.find_fan_chip()
        if self.chip:
            self.fans = hwhw.scan_fans(self.chip, self.cfg.get("fans"))
            # 每个通道都要有曲线，界面上才有东西可拖
            changed = False
            for fc in self.fans:
                entry = self.cfg.setdefault("fans", {}).setdefault(str(fc.n), {})
                if not entry.get("curve"):
                    entry["curve"] = [list(p) for p in DEFAULT_CURVE]
                    changed = True
                # CPU 通道（pwm1）出厂即曲线模式 —— 拖折点调速是本面板的核心
                # 交互，默认就该可拖；其余通道保持 BIOS 以免吓到用户
                entry.setdefault("mode", "curve" if fc.n == 1 else "bios")
                entry.setdefault("duty", None)
            if changed:
                save_config(self.cfg)
        self.sensors = hwhw.scan_sensors(self.cfg.get("sensors"))
        if initial and self.chip:
            self.snap = self._load_or_take_snapshot()
            print(f"[hwdashd] 风扇芯片 {self.chip}  ({len(self.fans)} 个 PWM 通道)", flush=True)
            print(f"[hwdashd] 温度传感器 {len(self.sensors)} 路", flush=True)
            print(f"[hwdashd] PWM 原始值快照: {self.snap}", flush=True)
            # 恢复持久化的曲线通道：重启/开机后 pwmN_enable 一律回到 BIOS
            # 自动(2)，内核在自动模式下拒绝手动占空比写入（EBUSY），曲线会
            # 变成每 2 秒一条错误刷屏而永不生效 —— 必须先重新断言 manual。
            for fc in self.fans:
                entry = self.cfg.get("fans", {}).get(str(fc.n), {})
                if entry.get("mode") != "curve":
                    continue
                curve = entry.get("curve") or []
                if not curve:
                    continue
                want = hwhw.eval_curve(curve, self.cpu_temp())
                try:
                    hwhw.set_mode(self.chip, fc.n, "manual", want)
                    fc.duty = want
                    print(f"[hwdashd] pwm{fc.n}: 已恢复曲线控制 (duty={want})", flush=True)
                except hwhw.PwmError as e:
                    print(f"[hwdashd] pwm{fc.n}: 启动恢复曲线失败: {e}", flush=True)

    def _load_or_take_snapshot(self) -> dict:
        try:
            with open(SNAPSHOT_PATH) as f:
                snap = json.load(f)
            if snap:
                return snap
        except (OSError, ValueError):
            pass
        snap = {str(k): v for k, v in hwhw.snapshot(self.chip, self.fans).items()}
        try:
            os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
            with open(SNAPSHOT_PATH, "w") as f:
                json.dump(snap, f)
        except OSError:
            pass
        return snap

    # -- 状态 ------------------------------------------------------------
    def cpu_temp(self) -> float:
        """曲线默认以 CPU 温度为输入。优先 coretemp Package，其次 it87 temp3。"""
        for s in self.sensors:
            if s.key == "coretemp:temp1":
                return s.value
        for s in self.sensors:
            if s.key == "it8628:temp3":
                return s.value
        return 40.0

    def state(self) -> dict:
        with self.lock:
            if self.chip and not os.path.isdir(self.chip):
                self._discover()
            elif self.chip:
                hwhw.refresh_fans(self.chip, self.fans)
            self.sensors = hwhw.scan_sensors(self.cfg.get("sensors"))
            cput = self.cpu_temp()
            fans = []
            for fc in self.fans:
                curve = self.cfg.get("fans", {}).get(str(fc.n), {}).get("curve") or []
                fans.append({
                    "n": fc.n,
                    "label": fc.label,
                    "duty": fc.duty,
                    "enable": fc.enable,
                    "rpm": fc.rpm,
                    "auto_start": fc.auto_start,
                    "has_tach": fc.has_tach,
                    "mode": fc.mode,
                    "duty_override": fc.duty_override,
                    "curve": curve,
                    "curve_duty_now": hwhw.eval_curve(curve, cput) if curve else None,
                })
            return {
                "ts": time.time(),
                "uptime": time.time() - self.boot,
                "chip": self.chip,
                "cpu_temp": cput,
                "sensors": [s.__dict__ for s in self.sensors],
                "fans": fans,
                "voltages": self.voltages(),
                "freqs": hwhw.cpu_freqs_mhz(),
                "rgb": {"ok": self.rgb_ok, "error": self.rgb_error},
            }

    # -- 电压 ------------------------------------------------------------
    def voltages(self) -> dict:
        """每核心 MSR 电压 + Super-I/O 轨道，带会话 min/max。"""
        cores, err = hwhw.read_core_voltages()
        for c in cores:
            mm = self.v_mm.setdefault(c["cpu"], [c["v"], c["v"]])
            mm[0] = min(mm[0], c["v"])
            mm[1] = max(mm[1], c["v"])
            c["min_v"], c["max_v"] = mm[0], mm[1]
        rails = hwhw.scan_voltages()
        for r in rails:
            if r.get("v") is None:
                continue
            key = f"{r['chip']}:{r['channel']}"
            mm = self.rail_mm.setdefault(key, [r["v"], r["v"]])
            mm[0] = min(mm[0], r["v"])
            mm[1] = max(mm[1], r["v"])
            r["session_min"], r["session_max"] = mm[0], mm[1]
        vals = [c["v"] for c in cores]
        return {"cores": cores, "rails": rails, "error": err,
                "avg": round(sum(vals) / len(vals), 4) if vals else None,
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None}

    # -- 风扇控制 --------------------------------------------------------
    def fan_mode(self, n: int, mode: str, duty: int | None = None) -> tuple[bool, str]:
        if not self.chip:
            return False, "未找到风扇芯片"
        if mode not in ("bios", "curve", "manual"):
            return False, f"未知模式 {mode}"
        fc = next((f for f in self.fans if f.n == n), None)
        if fc is None:
            return False, f"没有 pwm{n}"
        entry = self.cfg.setdefault("fans", {}).setdefault(str(n), {})
        try:
            if mode == "bios":
                hwhw.set_mode(self.chip, n, "bios")
                entry["mode"] = "bios"
                entry["duty"] = None
                # 交还 BIOS 时把占空比寄存器还原成启动时的值，
                # 否则自动模式的起始转速会被我们之前写的值顶掉
                s = self.snap.get(str(n)) or self.snap.get(n)
                if s and s.get("duty") is not None:
                    hwhw.set_mode(self.chip, n, "manual", s["duty"])
                    hwhw.set_mode(self.chip, n, "bios")
                fc.mode = "bios"
                fc.duty_override = None
            elif mode == "manual":
                d = 128 if duty is None else duty
                hwhw.set_mode(self.chip, n, "manual", d)
                entry["mode"] = "manual"
                entry["duty"] = d
                fc.mode = "manual"
                fc.duty_override = d
            else:  # curve
                if not entry.get("curve"):
                    entry["curve"] = [[40, 30], [60, 70], [85, 255]]
                hwhw.set_mode(self.chip, n, "manual", hwhw.eval_curve(entry["curve"], self.cpu_temp()))
                entry["mode"] = "curve"
                entry["duty"] = None
                fc.mode = "curve"
                fc.duty_override = None
        except hwhw.PwmError as e:
            return False, str(e)
        save_config(self.cfg)
        hwhw.refresh_fans(self.chip, self.fans)
        return True, "ok"

    def fan_curve(self, n: int, points: list) -> tuple[bool, str]:
        pts = []
        for p in points:
            try:
                t = max(TEMP_MIN, min(TEMP_MAX, float(p[0])))
                d = max(0, min(255, int(p[1])))
            except (TypeError, ValueError, IndexError):
                return False, f"非法曲线点 {p}"
            pts.append([round(t, 1), d])
        pts.sort()
        # 去重：同一温度只保留最后一个
        dedup: dict[float, int] = {}
        for t, d in pts:
            dedup[t] = d
        pts = [[t, dedup[t]] for t in sorted(dedup)]
        entry = self.cfg.setdefault("fans", {}).setdefault(str(n), {})
        entry["curve"] = pts
        save_config(self.cfg)
        fc = next((f for f in self.fans if f.n == n), None)
        if fc:
            fc.curve = pts
            # 拖曲线要即时可见：不等 2s 周期，凡是 curve 模式马上按新曲线写占空比
            if fc.mode == "curve" and self.chip:
                want = hwhw.eval_curve(pts, self.cpu_temp())
                if want != fc.duty:
                    try:
                        hwhw.set_duty(self.chip, n, want)
                        fc.duty = want
                    except hwhw.PwmError as e:
                        print(f"[hwdashd] pwm{n}: 曲线即时应用失败: {e}", flush=True)
        return True, "ok"

    def apply_curves(self) -> None:
        cput = self.cpu_temp()
        for fc in self.fans:
            if fc.mode != "curve":
                continue
            entry = self.cfg.get("fans", {}).get(str(fc.n), {})
            curve = entry.get("curve") or []
            if not curve or not self.chip:
                continue
            want = hwhw.eval_curve(curve, cput)
            if want != fc.duty:
                try:
                    hwhw.set_duty(self.chip, fc.n, want)
                    fc.duty = want
                except hwhw.PwmError as e:
                    # enable 被外部翻回自动模式时内核报 EBUSY。每通道至多
                    # 每 30s 记录一次并重断言 manual，避免错误刷爆 journal。
                    now = time.monotonic()
                    if now - getattr(fc, "_last_recover", 0.0) < 30.0:
                        continue
                    fc._last_recover = now
                    print(f"[hwdashd] pwm{fc.n}: {e}，尝试重新断言手动模式", flush=True)
                    try:
                        hwhw.set_mode(self.chip, fc.n, "manual", want)
                        fc.duty = want
                        print(f"[hwdashd] pwm{fc.n}: 已恢复曲线控制", flush=True)
                    except hwhw.PwmError as e2:
                        print(f"[hwdashd] pwm{fc.n}: 恢复失败: {e2}", flush=True)

    # -- RGB（OpenRGB SDK，openrgb_sdk.py）--------------------------------
    def _ensure_sdk(self) -> None:
        """拿到可用 SDK 连接；server 不在则由 systemd 拉起再连。

        切勿无条件调 self._sdk.connect() —— 那会每次请求都拆掉持久连接
        重建，server 端每拆一次记一条 "recv_select failed receiving magic"
        （UI 轮询时即每秒刷一条）。这里用毫秒级 controller_count() 做
        健康检查，连接活着就直接复用；死了才走 systemd 拉起 + 全新握手。
        """
        try:
            self._sdk.controller_count()
            return
        except openrgb_sdk.OpenRGBError:
            pass
        info = openrgb_sdk.rgb_ensure_server(wait=25)
        if not info.get("ok"):
            raise openrgb_sdk.OpenRGBError("OpenRGB SDK server 无法启动")
        self._sdk.connect()

    @staticmethod
    def _list_profiles() -> list[str]:
        """OpenRGB 1.0 配置档为 profiles/名字.json（旧版 .orp），两种都认。"""
        pdir = os.path.join(hwhw.USER_HOME, ".config", "OpenRGB", "profiles")
        try:
            names: set[str] = set()
            for f in os.listdir(pdir):
                if f.endswith(".json"):
                    names.add(f[:-5])
                elif f.endswith(".orp"):
                    names.add(f[:-4])
            return sorted(names)
        except OSError:
            return []

    def rgb_probe(self, force: bool = False) -> dict:
        """SDK 全量探测：设备 / 模式 / 灯区 / 灯珠 / 当前颜色。毫秒级，可随点随刷。"""
        if self.rgb_cache and not force:
            return self.rgb_cache
        try:
            self._ensure_sdk()
            devs = self._sdk.controllers()
        except openrgb_sdk.OpenRGBError as ex:
            self.rgb_ok = False
            self.rgb_error = str(ex)
            self.rgb_cache = {}
            return {"ok": False, "error": str(ex), "devices": [], "profiles": []}
        self.rgb_ok = True
        self.rgb_error = ""
        for d in devs:
            for z in d["zones"]:
                z.setdefault("hint", hwhw.ZONE_HINT.get(z["name"], ""))
            # 首次见到该设备时把 server 侧颜色登记为逻辑色基线；
            # 之后输出一律替换为逻辑色（设备上是被亮度缩放过的）。
            if d["idx"] not in self.rgb_led_logical:
                self.rgb_led_logical[d["idx"]] = list(d["colors"])
            elif self.rgb_led_logical[d["idx"]]:
                d["colors"] = list(self.rgb_led_logical[d["idx"]])
        self.rgb_cache = {"ok": True, "devices": devs,
                          "profiles": self._list_profiles()}
        return self.rgb_cache

    def _rgb_write(self, fn) -> tuple[bool, str]:
        try:
            self._ensure_sdk()
            fn()
            return True, "ok"
        except openrgb_sdk.OpenRGBError as ex:
            self.rgb_ok = False
            self.rgb_error = str(ex)
            return False, str(ex)

    # -- Direct 模式亮度（灯条颜色缩放） ------------------------------------
    def _strip_mask(self, dev: int) -> list[bool] | None:
        """led_idx → True=灯条区（Direct 下设备不应用亮度）/ False=单灯区。"""
        info = self.rgb_probe()
        try:
            zones = info["devices"][dev]["zones"]
        except (KeyError, IndexError, TypeError):
            return None
        mask: list[bool] = []
        for z in zones:
            strip = z.get("type") != "single"
            mask.extend([strip] * int(z.get("leds_count") or 0))
        return mask or None

    @staticmethod
    def _scale_hex(c: str, f: float) -> str:
        h = c.lstrip("#").ljust(6, "0")
        ch = (min(255, round(int(h[i:i + 2], 16) * f)) for i in (0, 2, 4))
        return "#{:02X}{:02X}{:02X}".format(*ch)

    def _direct_scale(self, dev: int) -> float:
        return self.rgb_direct_bright.get(dev, 255) / 255

    def _scale_for_direct(self, dev: int, colors: list[str],
                          led_from: int = 0) -> list[str]:
        """灯条 LED 按 Direct 亮度系数缩放，单灯区原样（设备侧自行应用）。"""
        f = self._direct_scale(dev)
        if f >= 0.999:
            return list(colors)
        mask = self._strip_mask(dev)
        out = list(colors)
        for i, c in enumerate(out):
            idx = led_from + i
            if mask is None or (idx < len(mask) and mask[idx]):
                out[i] = self._scale_hex(c, f)
        return out

    def _remember_logical(self, dev: int, colors: list[str],
                          led_from: int = 0) -> None:
        lst = self.rgb_led_logical.setdefault(dev, [])
        for i, c in enumerate(colors):
            idx = led_from + i
            while len(lst) <= idx:
                lst.append("#000000")
            lst[idx] = c

    def rgb_set_mode(self, req: dict) -> tuple[bool, str]:
        """激活一个模式并顺带下发颜色 / 速度 / 亮度（OpenRGB「选模式即生效」语义）。"""
        dev = int(req.get("device") or 0)
        midx = int(req.get("mode"))
        info = self.rgb_probe() if self.rgb_cache else self.rgb_probe(force=True)
        if not info.get("ok"):
            return False, info.get("error", "SDK 不可用")
        if dev >= len(info["devices"]) or midx >= len(info["devices"][dev]["modes"]):
            return False, "模式索引越界"
        m = info["devices"][dev]["modes"][midx]
        colors = req.get("colors")
        speed = req.get("speed")
        bright = req.get("brightness")
        ok, msg = self._rgb_write(lambda: self._sdk.set_mode(
            dev, m, colors=colors, speed=speed, brightness=bright))
        if ok:
            # OpenRGB server 对写命令走异步队列，立即回读会拿到旧值（Active/bright
            # 滞后一拍）。若在这里清空 cache，UI 下一次拉取就会回读旧值把滑块
            # 打回去 —— 用户看到的就是"亮度调不动"。因此改为把本次下发的值
            # 直接写进 cache，真实状态等下次 force 探测自然收敛。
            d = info["devices"][dev]
            d["active_mode"] = midx
            if speed is not None and m.get("flags", 0) & 0x201:
                m["speed"] = int(speed)
            if bright is not None and m.get("flags", 0) & 0x10:
                m["bright"] = int(bright)
                # Direct 模式：设备不缩放灯条颜色，daemon 侧按新亮度重发逻辑色
                if m.get("value") == 65535 and d["active_mode"] == midx:
                    self.rgb_direct_bright[dev] = int(bright)
                    logical = self.rgb_led_logical.get(dev)
                    if logical:
                        scaled = self._scale_for_direct(dev, logical, 0)
                        self._rgb_write(
                            lambda s=scaled: self._sdk.update_leds(dev, s))
            if colors:
                base = list(m.get("colors") or [])
                base = [c.lstrip("#").upper() for c in colors] + base[len(colors):]
                m["colors"] = [f"#{c}" for c in base]
            self.rgb_cache = info
        return ok, msg

    def rgb_set_custom(self, req: dict) -> tuple[bool, str]:
        """切到 Direct/Custom 模式（逐颗编辑的入口，OpenRGB 点 LED 时同样自动切）。"""
        dev = int(req.get("device") or 0)
        ok, msg = self._rgb_write(lambda: self._sdk.set_custom_mode(dev))
        if ok:
            self.rgb_cache = {}
        return ok, msg

    def rgb_zone_leds(self, req: dict) -> tuple[bool, str]:
        """整灯区逐颗写色；自动先切 Direct（与 OpenRGB 一致）。"""
        dev = int(req.get("device") or 0)
        zone = int(req.get("zone"))
        colors = req.get("colors") or []
        led_from = self._zone_led_offset(dev, zone)
        self._remember_logical(dev, colors, led_from)
        scaled = self._scale_for_direct(dev, colors, led_from)

        def w():
            self._sdk.set_custom_mode(dev)
            self._sdk.update_zone_leds(dev, zone, scaled)
        ok, msg = self._rgb_write(w)
        if ok:
            self.rgb_cache = {}
        return ok, msg

    def rgb_leds(self, req: dict) -> tuple[bool, str]:
        """整设备全部灯珠写色；自动先切 Direct。"""
        dev = int(req.get("device") or 0)
        colors = req.get("colors") or []
        self._remember_logical(dev, colors, 0)
        scaled = self._scale_for_direct(dev, colors, 0)

        def w():
            self._sdk.set_custom_mode(dev)
            self._sdk.update_leds(dev, scaled)
        ok, msg = self._rgb_write(w)
        if ok:
            self.rgb_cache = {}
        return ok, msg

    def rgb_led(self, req: dict) -> tuple[bool, str]:
        """单颗写色；自动先切 Direct。"""
        dev = int(req.get("device") or 0)
        led = int(req.get("led"))
        color = req.get("color") or "FF0000"
        self._remember_logical(dev, [color], led)
        scaled = self._scale_for_direct(dev, [color], led)[0]

        def w():
            self._sdk.set_custom_mode(dev)
            self._sdk.update_single_led(dev, led, scaled)
        ok, msg = self._rgb_write(w)
        if ok:
            self.rgb_cache = {}
        return ok, msg

    def _zone_led_offset(self, dev: int, zone: int) -> int:
        """灯区 zone 之前累计的灯珠数（用于逻辑色数组定位）。"""
        info = self.rgb_probe()
        try:
            zones = info["devices"][dev]["zones"]
            return sum(int(z.get("leds_count") or 0) for z in zones[:zone])
        except (KeyError, IndexError, TypeError):
            return 0

    def rgb_profile(self, req: dict) -> tuple[bool, str]:
        action = str(req.get("action") or "")
        name = str(req.get("name") or "")[:64]
        if action not in ("save", "load", "delete") or not name:
            return False, "需要 action(save/load/delete) 与 name"
        ok, msg = self._rgb_write(lambda: self._sdk.profile(action, name))
        if ok:
            if action == "load":
                self.rgb_cache = {}   # 设备状态可能整体变了，让 UI 重新拉
            self.rgb_cache = {}
        return ok, msg

    # -- 传感器改名 ------------------------------------------------------
    def sensor_rename(self, req: dict) -> tuple[bool, str]:
        key = str(req.get("key") or "")
        if not key:
            return False, "缺少 key"
        entry = self.cfg.setdefault("sensors", {}).setdefault(key, {})
        if "label" in req:
            entry["label"] = str(req["label"])[:40]
        if "group" in req:
            entry["group"] = str(req["group"])[:40]
        if "note" in req:
            entry["note"] = str(req["note"])[:120]
        save_config(self.cfg)
        self.sensors = hwhw.scan_sensors(self.cfg.get("sensors"))
        return True, "ok"

    # -- 退出还原 --------------------------------------------------------
    def shutdown(self) -> None:
        print("[hwdashd] 收到退出信号，还原 PWM 寄存器…", flush=True)
        if self.chip and self.snap:
            # 先把所有通道交还 BIOS 自动
            for fc in self.fans:
                try:
                    hwhw.set_mode(self.chip, fc.n, "bios")
                except hwhw.PwmError:
                    pass
            hwhw.restore(self.chip, self.snap)
            print("[hwdashd] 已还原为启动时的值", flush=True)
        try:
            os.remove(SNAPSHOT_PATH)
        except OSError:
            pass
        sys.exit(0)


ENGINE: Engine | None = None


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hwdashd/1.0"

    def log_message(self, fmt, *args):        # 静音访问日志
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip()
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "只接受本机访问"})
        path = urlparse(self.path).path
        e = ENGINE
        if path == "/api/state":
            return self._send(200, e.state())
        if path == "/api/rgb":
            return self._send(200, e.rgb_probe(force="refresh" in self.path))
        if path == "/api/info":
            return self._send(200, e.info)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "只接受本机访问"})
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return self._send(400, {"error": "请求体不是合法 JSON"})
        e = ENGINE
        try:
            with e.lock:
                if path == "/api/fan/mode":
                    ok, msg = e.fan_mode(int(req["n"]), str(req["mode"]), req.get("duty"))
                elif path == "/api/fan/curve":
                    ok, msg = e.fan_curve(int(req["n"]), req.get("points") or [])
                elif path == "/api/sensor":
                    ok, msg = e.sensor_rename(req)
                elif path == "/api/rgb/mode":
                    ok, msg = e.rgb_set_mode(req)
                elif path == "/api/rgb/custom":
                    ok, msg = e.rgb_set_custom(req)
                elif path == "/api/rgb/leds":
                    ok, msg = e.rgb_leds(req)
                elif path == "/api/rgb/zone":
                    ok, msg = e.rgb_zone_leds(req)
                elif path == "/api/rgb/led":
                    ok, msg = e.rgb_led(req)
                elif path == "/api/rgb/profile":
                    ok, msg = e.rgb_profile(req)
                elif path == "/api/fans/all-bios":
                    ok, msg = True, ""
                    for fc in list(e.fans):
                        o, m = e.fan_mode(fc.n, "bios")
                        ok = ok and o
                    msg = "ok" if ok else "部分通道失败"
                else:
                    return self._send(404, {"error": "not found"})
        except (KeyError, ValueError, TypeError) as ex:
            return self._send(400, {"error": f"参数错误: {ex}"})
        return self._send(200 if ok else 400, {"ok": ok, "message": msg})


def main() -> None:
    global ENGINE
    if os.geteuid() != 0:
        print("[hwdashd] 必须以 root 运行（写 PWM 寄存器需要）", file=sys.stderr)
        sys.exit(1)
    ENGINE = Engine()
    signal.signal(signal.SIGTERM, lambda *_: ENGINE.shutdown())
    signal.signal(signal.SIGINT, lambda *_: ENGINE.shutdown())

    def loop():
        while True:
            try:
                with ENGINE.lock:
                    ENGINE.apply_curves()
            except Exception as ex:            # 守护进程绝不能因为一次异常退出
                print(f"[hwdashd] 循环异常: {ex!r}", file=sys.stderr)
            time.sleep(INTERVAL)

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    srv.daemon_threads = True
    print(f"[hwdashd] 监听 http://{BIND_HOST}:{BIND_PORT}  (Ctrl-C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    ENGINE.shutdown()


if __name__ == "__main__":
    main()
