"""hwhw — 共享硬件抽象层（被守护进程 hwdashd 与图形界面 hwdash 同时 import）

只依赖标准库。分三部分：
  1. 传感器发现（hwmon 温度 / 风扇 / PWM 通道）
  2. PWM 读写（写需要 root；守护进程以 root 运行）
  3. RGB（通过 OpenRGB SDK 网络协议，见 openrgb_sdk.py；不直接 hidraw、不再跑 CLI）
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- 传感器发现

HWMON_ROOT = "/sys/class/hwmon"

# 内核驱动名 -> 展示名
DRIVER_LABEL = {
    "coretemp": "CPU",
    "nvme": "NVMe",
    "it8628": "主板 Super-I/O",
    "it87": "主板 Super-I/O",
    "gigabyte_wmi": "主板 (WMI)",
    "r8169_0_8400:00": "网卡",
    "acpitz_0": "ACPI 温度 0",
    "acpitz_1": "ACPI 温度 1",
    "acpi_fan": "ACPI 风扇",
}

# 通过满载相关性测试实测出来的 it8628 通道含义（2026-10-04）。
# 值为 None 表示"未识别"，界面上让用户自己改名。
IT8628_GUESS = {
    1: ("主板环境", None),
    2: ("主板区域 2", None),
    3: ("CPU", "随满载 36→96°C，与 coretemp Package 同步"),
    4: ("主板区域 4", None),
    5: ("CPU 供电", "随满载 34→42°C，疑似 VRM"),
    6: ("主板区域 6", None),
}

FAN_LABEL = {
    1: "CPU_FAN",
    2: "CPU_OPT / 水泵",
    3: "SYS_FAN 1",
    4: "SYS_FAN 2",
    5: "SYS_FAN 3",
    6: "SYS_FAN / 水泵",
}

ZONE_HINT = {
    "ARGB_V2_1": "5V ARGB 排针 1",
    "ARGB_V2_2": "5V ARGB 排针 2",
    "ARGB_V2_3": "5V ARGB 排针 3",
    "Chipset Accent": "芯片组灯（板载）",
    "LED_C": "12V RGB 灯带排针",
    "I/O Cover": "I/O 罩（板载）",
}


@dataclass
class Sensor:
    key: str          # 稳定标识，例如 "it8628:temp3"
    hwmon: str        # 驱动名，例如 "it8628"
    channel: str      # "temp3"
    group: str        # 展示分组
    label: str        # 用户可改的显示名
    value: float = 0.0  # °C
    note: str = ""


@dataclass
class FanChannel:
    n: int
    label: str
    has_pwm: bool = False
    has_tach: bool = False
    duty: int = 0          # 0..255
    enable: int = 2        # 0=全速 1=手动 2=BIOS自动
    rpm: int = 0
    auto_start: int | None = None   # 与 duty 共用的那个寄存器
    mode: str = "bios"     # bios | curve | manual
    duty_override: int | None = None
    curve: list = field(default_factory=list)   # [[temp, duty0..255], ...]


def _read(path: str):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path: str):
    v = _read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _read_float(path: str):
    v = _read(path)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _glob_hwmons():
    out = []
    try:
        names = sorted(os.listdir(HWMON_ROOT))
    except OSError:
        return out
    for name in names:
        d = os.path.join(HWMON_ROOT, name)
        drv = _read(os.path.join(d, "name"))
        if drv:
            out.append((name, d, drv))
    return out


def scan_sensors(overrides: dict | None = None) -> list[Sensor]:
    """枚举所有温度通道。overrides: {key: {"label":..., "group":...}}

    去重规则（本机实测得出）：
      * gigabyte_wmi 的 temp1-6 与 it87 的 temp1-6 是同一批 Super-I/O 探头的两条读取
        路径，数值逐位相同 —— 只保留 it87。
      * nvme 的 "Sensor 1" 常与 "Composite" 同值 —— 只留 Composite 与不同的那个。
    """
    overrides = overrides or {}
    raw: list[Sensor] = []
    drivers = {drv for _hw, _d, drv in _glob_hwmons()}
    for _hw, d, drv in _glob_hwmons():
        group = DRIVER_LABEL.get(drv, drv)
        try:
            files = os.listdir(d)
        except OSError:
            continue
        for f in files:
            m = re.fullmatch(r"temp(\d+)_input", f)
            if not m:
                continue
            ch = f"temp{m.group(1)}"
            val = _read_float(os.path.join(d, f))
            if val is None:
                continue
            # 千分之一度 -> 度
            celsius = val / 1000.0 if val > 1000 else val
            key = f"{drv}:{ch}"
            label = _read(os.path.join(d, f"temp{m.group(1)}_label")) or ""
            note = ""
            if drv.startswith("it8") and not label:
                guess = IT8628_GUESS.get(int(m.group(1)))
                if guess:
                    label, note = guess
            if not label:
                label = f"{group} {ch}"
            if drv == "coretemp" and label.startswith("Core "):
                # 每核心只保留 Package，界面太挤
                continue
            ov = overrides.get(key)
            if ov:
                label = ov.get("label", label)
                group = ov.get("group", group)
                note = ov.get("note", note)
            raw.append(Sensor(key, drv, ch, group, label, round(celsius, 1), note))

    sensors: list[Sensor] = []
    have_it = any(d.startswith("it8") for d in drivers)
    for s in raw:
        if s.hwmon == "gigabyte_wmi" and have_it:
            continue          # 与 it87 完全重复
        if s.hwmon == "nvme" and s.label == "Sensor 1":
            comp = next((x.value for x in raw if x.hwmon == "nvme" and x.label == "Composite"), None)
            if comp is not None and abs(comp - s.value) < 0.6:
                continue      # 与 Composite 同值
        sensors.append(s)

    # 排序：CPU 优先，然后按分组名 + 通道名
    def sk(s: Sensor):
        pri = 0 if s.key.startswith("coretemp") else (1 if s.key.startswith("it8") else 2)
        return (pri, s.group, s.channel)
    sensors.sort(key=sk)
    return sensors


def find_fan_chip() -> str | None:
    """找到带 pwm 通道的风扇 hwmon 目录（按能力找，不硬编码 hwmonN —— 编号会随启动顺序变化）。

    泛化：任何驱动暴露 pwmN + pwmN_enable 的芯片都算数（it87 / nct67xx /
    nct6775 / …）；有多个时优先 it87 系（本应用对其语义验证最充分），
    其余按名称排序取第一个。找不到返回 None，调用方按"无风扇控制"降级。
    """
    candidates = []
    for _hw, d, drv in _glob_hwmons():
        try:
            files = os.listdir(d)
        except OSError:
            continue
        has_pwm = any(re.fullmatch(r"pwm\d+", f) for f in files)
        has_en = any(re.fullmatch(r"pwm\d+_enable", f) for f in files)
        if has_pwm and has_en:
            candidates.append((0 if drv.startswith("it8") else 1, drv, d))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[0][2]


def scan_fans(chip_dir: str, state: dict | None = None) -> list[FanChannel]:
    state = state or {}
    drv = _read(os.path.join(chip_dir, "name")) or "it87"
    out: list[FanChannel] = []
    for n in range(1, 9):
        pwm = os.path.join(chip_dir, f"pwm{n}")
        if not os.path.exists(pwm):
            continue
        fc = FanChannel(n=n, label=FAN_LABEL.get(n, f"FAN {n}"))
        fc.has_pwm = True
        fc.has_tach = os.path.exists(os.path.join(chip_dir, f"fan{n}_input"))
        st = state.get(str(n), {})
        fc.mode = st.get("mode", "bios")
        fc.curve = st.get("curve") or [[40, 30], [60, 60], [80, 100]]
        fc.duty_override = st.get("duty_override")
        out.append(fc)
    refresh_fans(chip_dir, out)
    return out


def refresh_fans(chip_dir: str, fans: list[FanChannel]) -> None:
    for fc in fans:
        fc.duty = _read_int(os.path.join(chip_dir, f"pwm{fc.n}")) or 0
        fc.enable = _read_int(os.path.join(chip_dir, f"pwm{fc.n}_enable"))
        if fc.enable is None:
            fc.enable = 2
        rpm = _read_int(os.path.join(chip_dir, f"fan{fc.n}_input"))
        fc.rpm = rpm if rpm is not None else 0
        a = _read_int(os.path.join(chip_dir, f"pwm{fc.n}_auto_start"))
        fc.auto_start = a


# ---------------------------------------------------------------- PWM 写入

class PwmError(RuntimeError):
    pass


def set_mode(chip_dir: str, n: int, mode: str, duty: int | None = None) -> None:
    """mode: bios（交还 BIOS 自动）| manual（固定占空比）"""
    en = os.path.join(chip_dir, f"pwm{n}_enable")
    pwm = os.path.join(chip_dir, f"pwm{n}")
    try:
        if mode == "bios":
            with open(en, "w") as f:
                f.write("2")
            return
        # 写占空比必须先切手动（自动模式下内核返回 EBUSY）
        with open(en, "w") as f:
            f.write("1")
        if duty is not None:
            duty = max(0, min(255, int(duty)))
            with open(pwm, "w") as f:
                f.write(str(duty))
    except PermissionError as e:
        raise PwmError(f"权限不足：{e}（守护进程需要以 root 运行）") from e
    except OSError as e:
        raise PwmError(f"写入 pwm{n} 失败：{e}") from e


def set_duty(chip_dir: str, n: int, duty: int) -> None:
    duty = max(0, min(255, int(duty)))
    try:
        with open(os.path.join(chip_dir, f"pwm{n}"), "w") as f:
            f.write(str(duty))
    except PermissionError as e:
        raise PwmError(f"权限不足：{e}") from e
    except OSError as e:
        raise PwmError(f"写入占空比失败：{e}") from e


def snapshot(chip_dir: str, fans: list[FanChannel]) -> dict:
    """记录 pwm*_enable 与 pwm* 的当前值。

    必须快照：在新一代 iTE 芯片上「手动占空比」与「自动模式起始占空比」是同一个
    寄存器，写占空比会覆盖 BIOS 配的起始值。见 README。
    """
    snap = {}
    for fc in fans:
        snap[fc.n] = {
            "enable": _read_int(os.path.join(chip_dir, f"pwm{fc.n}_enable")),
            "duty": _read_int(os.path.join(chip_dir, f"pwm{fc.n}")),
        }
    return snap


def restore(chip_dir: str, snap: dict) -> None:
    for n, s in snap.items():
        try:
            with open(os.path.join(chip_dir, f"pwm{n}_enable"), "w") as f:
                f.write("1")
            if s.get("duty") is not None:
                with open(os.path.join(chip_dir, f"pwm{n}"), "w") as f:
                    f.write(str(s["duty"]))
            en = s.get("enable")
            with open(os.path.join(chip_dir, f"pwm{n}_enable"), "w") as f:
                f.write(str(en if en is not None else 2))
        except OSError:
            pass


# ---------------------------------------------------------------- 曲线求值

def eval_curve(points: list, celsius: float) -> int:
    """分段线性插值。points: [[temp, duty0..255], ...]（温度升序）-> 占空比 0..255"""
    pts = sorted((float(t), max(0, min(255, int(d)))) for t, d in points)
    if not pts:
        return 0
    if celsius <= pts[0][0]:
        return int(pts[0][1])
    if celsius >= pts[-1][0]:
        return int(pts[-1][1])
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        if t0 <= celsius <= t1:
            if t1 == t0:
                return int(d1)
            f = (celsius - t0) / (t1 - t0)
            return int(round(d0 + (d1 - d0) * f))
    return int(pts[-1][1])


# ---------------------------------------------------------------- RGB (OpenRGB)

def _user_home() -> str:
    """守护进程以 root 运行，但 OpenRGB 的包装脚本在**登录用户**的家目录下。
    优先级：HWDASH_USER_HOME 环境变量（systemd 单元里设）> SUDO_USER 的家目录 > 当前 $HOME。
    """
    env = os.environ.get("HWDASH_USER_HOME")
    if env and os.path.isdir(env):
        return env
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd
            return pwd.getpwnam(sudo_user).pw_dir
        except (ImportError, KeyError):
            pass
    return os.path.expanduser("~")


USER_HOME = _user_home()


# RGB 走 OpenRGB SDK 网络协议（openrgb_sdk.py）。CLI 冷启动路径已删除：
# 常驻 server 与临时 CLI 实例会并行探测同一条 SMBus/i2c，有互踩风险。


def gradient(stops: list[tuple[float, str]], count: int) -> str:
    """把 [(位置0..1, 'RRGGBB'), ...] 生成为 count 个 LED 的颜色串。

    stops 需按位置升序。返回 'RRGGBB,RRGGBB,...'（大写）。
    """
    if count <= 0:
        return ""
    st = sorted(stops, key=lambda s: s[0])
    if not st:
        return ""
    if len(st) == 1:
        return ",".join([st[0][1].upper()] * count)

    def hx(c):
        c = c.lstrip("#")
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))

    out = []
    for i in range(count):
        pos = i / max(1, count - 1) if count > 1 else 0.0
        if pos <= st[0][0]:
            out.append(st[0][1].upper()); continue
        if pos >= st[-1][0]:
            out.append(st[-1][1].upper()); continue
        for (p0, c0), (p1, c1) in zip(st, st[1:]):
            if p0 <= pos <= p1:
                a, b = hx(c0), hx(c1)
                f = 0.0 if p1 == p0 else (pos - p0) / (p1 - p0)
                out.append("".join(f"{int(round(a[k] + (b[k] - a[k]) * f)):02X}" for k in range(3)))
                break
    return ",".join(out)


# ---------------------------------------------------------------- 电压 (voltmon + MSR)

VOLTMON_DIR = os.path.join(USER_HOME, "Applications", "benchmarks", "voltage")


def _voltmon():
    """直接 import 用户的 voltmon 库 —— 面板与 CLI/webui 读数同源，永不漂移。"""
    if VOLTMON_DIR not in sys.path:
        sys.path.insert(0, VOLTMON_DIR)
    import voltmon
    return voltmon


def scan_voltages() -> list[dict]:
    """Super-I/O 电压轨道（it8628 的 9 路 ADC，毫伏 -> 伏）。"""
    try:
        vm = _voltmon()
        data = vm.collect()
    except Exception:  # noqa: BLE001
        return []
    rails = []
    for chip in data.get("chips", []):
        for r in chip.get("rails", []):
            rails.append({
                "chip": chip.get("chip", ""), "channel": r.get("channel", ""),
                "label": r.get("label") or "", "v": r.get("v"),
                "limit_min": r.get("min_v"), "limit_max": r.get("max_v"),
                "alarm": bool(r.get("alarm")), "sane": bool(r.get("sane")),
                "alarm_suspect": bool(r.get("alarm_suspect")),
            })
    # 本机实测修正（2026-10-05 负载交叉验证）：空闲/全核负载/单核加速三态
    # 采样下，it8628 没有任何轨道随 CPU 负载跳变（in0 在全核负载时反而微降
    # 1.44->1.38V），即 VCore 未接 SIO——每核心 MSR 0x198 读数才是 VCore
    # 唯一来源。voltmon.rail_guess 按电压猜出的 "Vcore?"（in0，1.4V 级恒定
    # 轨，更像 DRAM VDD/VDDQ）会误导，按实测语义覆盖。
    for r in rails:
        if str(r["chip"]).lower().startswith("it8628"):
            r["label"] = _IT8628_LABELS.get(r["channel"], r["label"])
    return rails


_IT8628_LABELS = {
    "in0": "1.4V轨(疑VDD/VDDQ)",
    "in1": "2.0V轨(疑VCCIN)",
    "in2": "2.0V轨(疑VCCIN2)",
    "in3": "2.0V轨(疑VCCIN3)",
    "in4": "低压轨(~0.6V)",
    "in5": "0.8V轨(疑VCCSA)",
    "in6": "1.3V轨(偶跳变)",
}


def cpu_vendor() -> str:
    """AuthenticAMD / GenuineIntel / 其他（读 /proc/cpuinfo，启动后缓存）。"""
    global _CPU_VENDOR
    if _CPU_VENDOR is None:
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("vendor_id"):
                        _CPU_VENDOR = line.split(":")[-1].strip()
                        break
        except OSError:
            pass
        _CPU_VENDOR = _CPU_VENDOR or "unknown"
    return _CPU_VENDOR


_CPU_VENDOR = None


def read_core_voltages() -> tuple[list[dict], str]:
    """每个逻辑 CPU 读 MSR 0x198 IA32_PERF_STATUS[47:32]（单位 1/8192 V）。

    需要 root（守护进程满足）。返回 (cores, first_error)。
    泛化：MSR 0x198 是 Intel 语义；AMD/其他架构直接给出明确错误而不是
    静默读出无意义数字。
    """
    vendor = cpu_vendor()
    if vendor != "GenuineIntel":
        return [], (f"每核心电压（MSR 0x198）仅支持 Intel 处理器；"
                    f"检测到 {vendor or '未知厂商'}，此页每核心部分不可用"
                    f"（主板轨道不受影响）")
    try:
        vm = _voltmon()
        msr_reg, divisor = vm.MSR_PERF_STATUS, vm.MSR_VOLT_DIVISOR
    except Exception:  # noqa: BLE001
        msr_reg, divisor = 0x198, 8192.0
    cores: list[dict] = []
    first_err = ""
    for path in sorted(glob.glob("/dev/cpu/*/msr"),
                       key=lambda p: int(p.split("/")[3]) if p.split("/")[3].isdigit() else 999):
        try:
            n = int(path.split("/")[3])
        except (ValueError, IndexError):
            continue
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as e:
            if not first_err:
                first_err = f"/dev/cpu/{n}/msr 打不开: {e.strerror}"
            continue
        try:
            raw = os.pread(fd, 8, msr_reg)
            val = int.from_bytes(raw, "little")
            mv = ((val >> 32) & 0xFFFF) / divisor * 1000.0
            # 合理核心电压窗口：低于 0.3V 视为寄存器未填充
            if 300 <= mv <= 1800:
                cores.append({"cpu": n, "v": round(mv / 1000.0, 4)})
        except OSError as e:
            if not first_err:
                first_err = f"cpu{n} 读 0x198 失败: {e.strerror}"
        finally:
            os.close(fd)
    return cores, first_err


# ---------------------------------------------------------------- CPU 频率 / 系统信息

def cpu_freqs_mhz() -> list[float]:
    """每个逻辑 CPU 的当前频率（MHz），来自 cpufreq。"""
    freqs = []
    for p in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"),
                    key=lambda x: int(re.search(r"cpu(\d+)", x).group(1))):
        v = _read_int(p)
        if v:
            freqs.append(v / 1000.0)
    if not freqs:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("cpu MHz"):
                        freqs.append(float(line.split(":", 1)[1]))
        except (OSError, ValueError):
            pass
    return freqs


def sysinfo() -> dict:
    """静态系统信息：CPU 型号 / 内存条明细（dmidecode）/ 主板 / BIOS / GPU。

    调用一次几百毫秒，守护进程启动时缓存。
    """
    info = {"cpu_model": "", "cpu_cores": "", "dimms": [], "total_mem_gb": 0,
            "board": {}, "bios": {}, "gpu": []}

    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    info["cpu_threads"] = len(glob.glob("/sys/devices/system/cpu/cpu[0-9]*"))

    try:
        out = subprocess.run(["/usr/sbin/dmidecode", "-t", "17"],
                             capture_output=True, text=True, timeout=20).stdout
        for block in out.split("\n\n"):
            if "Memory Device" not in block or "Size:" not in block:
                continue

            def field(name):
                m = re.search(rf"^\s*{name}:\s*(.+)$", block, re.M)
                return m.group(1).strip() if m else ""

            size = field("Size")
            if "No Module" in size or "Not Specified" in size or not size:
                continue
            gb = 0
            m = re.match(r"(\d+)\s*(GB|MB)", size)
            if m:
                gb = int(m.group(1)) * (1 if m.group(2) == "GB" else 0)
            mtps = field("Configured Memory Speed") or field("Speed")
            mm = re.search(r"(\d+)\s*MT/s", mtps)
            mtps_n = int(mm.group(1)) if mm else 0
            info["total_mem_gb"] += gb
            info["dimms"].append({
                "locator": field("Locator"),
                "manufacturer": field("Manufacturer"),
                "part": field("Part Number"),
                "size": size,
                "type": field("Type") or "Unknown",
                "mtps": mtps_n,
                "clock_mhz": round(mtps_n / 2.0) if mtps_n else 0,
                "rank": field("Rank"),
            })
    except (OSError, subprocess.SubprocessError):
        pass

    for key, path in (("vendor", "board_vendor"), ("name", "board_name"),
                      ("version", "board_version")):
        v = _read(f"/sys/class/dmi/id/{path}")
        if v:
            info["board"][key] = v
    for key, path in (("vendor", "bios_vendor"), ("version", "bios_version"),
                      ("date", "bios_date")):
        v = _read(f"/sys/class/dmi/id/{path}")
        if v:
            info["bios"][key] = v

    try:
        out = subprocess.run(["lspci", "-nn"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if re.search(r"VGA compatible|3D controller|Display controller", line):
                info["gpu"].append(line.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return info


# ------------------------------------------------------------------ 实时利用率

_CPU_TICKS: tuple[int, int] | None = None   # (idle, total) 上次 /proc/stat 快照


def read_cpu_util() -> float | None:
    """整机 CPU 利用率（%），由 /proc/stat 相邻两次采样差值计算。

    守护进程按固定节奏轮询，天然构成采样对；第一次调用返回 None。
    """
    global _CPU_TICKS
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("cpu "):
                    parts = [int(x) for x in line.split()[1:9]]
                    idle = parts[3] + parts[4]          # idle + iowait
                    total = sum(parts)
                    break
            else:
                return None
    except (OSError, ValueError):
        return None
    if _CPU_TICKS is None:
        _CPU_TICKS = (idle, total)
        return None
    d_idle, d_total = idle - _CPU_TICKS[0], total - _CPU_TICKS[1]
    _CPU_TICKS = (idle, total)
    if d_total <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))


def read_mem() -> dict | None:
    """内存占用（MemAvailable 语义，与 free/free -m 一致）。"""
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, val = line.partition(":")
                info[key.strip()] = int(val.strip().split()[0])  # kB
    except (OSError, ValueError, IndexError):
        return None
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    if not total:
        return None
    used = total - avail
    return {"total_mb": total // 1024, "used_mb": used // 1024,
            "percent": round(used / total * 100, 1),
            "swap_total_mb": info.get("SwapTotal", 0) // 1024,
            "swap_used_mb": (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) // 1024}


def read_gpus() -> list[dict]:
    """尽量枚举独立/核显：NVIDIA（nvidia-smi）、amdgpu、i915。全都没有就返回 []。

    每项：{name, util_percent, temp, core_mhz, mem_mhz, vram_used_mb, vram_total_mb}
    缺哪个字段就是 None —— 界面按需显示，不猜测。
    """
    out: list[dict] = []
    import shutil
    import subprocess
    # ---- NVIDIA
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,"
                 "memory.total,clocks.gr,clocks.mem",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().splitlines():
                p = [x.strip() for x in line.split(",")]
                if len(p) >= 7:
                    def _f(v):
                        try:
                            return float(v)
                        except ValueError:
                            return None
                    out.append({"name": p[0], "vendor": "NVIDIA",
                                "temp": _f(p[1]), "util_percent": _f(p[2]),
                                "vram_used_mb": _f(p[3]), "vram_total_mb": _f(p[4]),
                                "core_mhz": _f(p[5]), "mem_mhz": _f(p[6])})
        except (OSError, subprocess.SubprocessError):
            pass
    # ---- amdgpu / i915 / xe（sysfs 尽力而为）
    try:
        cards = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        cards = []
    for card in cards:
        if not card.startswith("card") or card.endswith("._dev_") :
            continue
        base = f"/sys/class/drm/{card}"
        try:
            vendor = open(os.path.join(base, "device/vendor"), encoding="utf-8").read().strip()
        except OSError:
            continue
        if vendor == "0x1002":          # AMD
            item = {"name": "AMD GPU", "vendor": "AMD", "card": card}
            try:
                item["util_percent"] = float(open(os.path.join(
                    base, "device/gpu_busy_percent"), encoding="utf-8").read().strip())
            except (OSError, ValueError):
                pass
            try:
                for ln in open(os.path.join(base, "device/pp_dpm_sclk"), encoding="utf-8"):
                    if ln.rstrip().endswith("*"):
                        item["core_mhz"] = float(ln.split(":")[1].replace("Mhz", "").strip())
            except (OSError, ValueError, IndexError):
                pass
            try:
                for ln in open(os.path.join(base, "device/pp_dpm_mclk"), encoding="utf-8"):
                    if ln.rstrip().endswith("*"):
                        item["mem_mhz"] = float(ln.split(":")[1].replace("Mhz", "").strip())
            except (OSError, ValueError, IndexError):
                pass
            try:
                item["vram_used_mb"] = int(open(os.path.join(
                    base, "device/mem_info_vram_used"), encoding="utf-8").read()) // 1048576
                item["vram_total_mb"] = int(open(os.path.join(
                    base, "device/mem_info_vram_total"), encoding="utf-8").read()) // 1048576
            except (OSError, ValueError):
                pass
            out.append(item)
        elif vendor == "0x8086":        # Intel 核显
            item = {"name": "Intel iGPU", "vendor": "Intel", "card": card}
            for f in ("gt_cur_freq_mhz", "gt0/rp0_freq_mhz"):
                try:
                    item["core_mhz"] = float(open(os.path.join(
                        base, "device", f), encoding="utf-8").read().strip())
                    break
                except (OSError, ValueError):
                    continue
            out.append(item)
    return out


def read_live() -> dict:
    """一次抓齐"实时利用率"块：CPU / 内存 / GPU。全部尽力而为，缺就缺。"""
    return {"cpu_util": read_cpu_util(), "mem": read_mem(), "gpus": read_gpus()}
