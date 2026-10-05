"""openrgb_sdk — OpenRGB SDK（net protocol v3）TCP 客户端，纯标准库。

为什么需要它：
  * OpenRGB CLI 没有逐颗（Per-LED）写色的能力，逐颗必须走 SDK；
  * 常驻 SDK server + 常驻连接让灯光操作从「每次冷启动 OpenRGB 进程（秒级）」
    变成毫秒级，且避免两个 OpenRGB 实例并行探测同一条 SMBus/i2c 互踩。
  * 因此面板**绝不再调用 OpenRGB CLI**：SDK 连不上时用 rgb_ensure_server()
    拉起 systemd 服务 openrgb-sdk.service（openrgb --server，root，仅监听 127.0.0.1）。

线上格式（实测校验 2026-10-05，OpenRGB 1.0，见文件尾探针）：
  包头 16 字节：magic b"ORGB" + u32 设备号 + u32 命令码 + u32 数据长度（小端）。
  字符串一律 u16 长度前缀（含 NUL）+ 字节；RGBColor 固定 4 字节 R,G,B,0x00。
  有响应的命令只有 0/1/40；写命令（1050+）server 静默，要确认就重新拉控制器数据。
  server 会主动推送命令 100（设备列表变更），读响应时要跳过。
协议版本协商（命令 40）：请求带客户端支持的最高版本，响应是服务端最高版本，
实际编码版本由 REQUEST_CONTROLLER_DATA(1) 请求里的 u32 决定 —— 本实现固定用 3
（带模式亮度字段，且不带 v4+ 的 zone segments 复杂度）。

命令行探针：
  python3 openrgb_sdk.py --probe            打印全部控制器结构
  python3 openrgb_sdk.py --mode 3           设备 0 切到模式 3（随后切回 Direct）
  python3 openrgb_sdk.py --paint ZONE       对灯区 ZONE 逐颗涂彩虹 2 秒后还原
"""
from __future__ import annotations

import socket
import subprocess
import threading
import time
from struct import pack, unpack, unpack_from

ORGB_HOST = "127.0.0.1"
ORGB_PORT = 6742
ORGB_SERVICE = "openrgb-sdk.service"

_MAGIC = b"ORGB"
_PROTO_VER = 3  # 0/1 基础 + vendor + profile 控制 + 模式亮度

# 命令码（OpenRGB NetworkProtocol.h，实测校验）
CMD_COUNT = 0              # -> 响应 u32 控制器数
CMD_DATA = 1               # -> 响应完整控制器数据（v1+ 请求带 u32 协议版本）
CMD_VERSION = 40           # -> 响应 u32 服务端最高协议版本
CMD_CLIENT_NAME = 50       # 无响应；NUL 结尾字符串
CMD_SET_CLIENT_FLAGS = 52  # u32 flags -> 响应 SET_SERVER_FLAGS(u32)；仅 v6+ 会话
CMD_SET_SERVER_FLAGS = 53  # server -> client 响应帧
CMD_ACK = 10               # v6+ 写命令回执：u32 acked_pkt_id + u32 status
CMD_LIST_UPDATED = 100     # server 主动推送，读响应时跳过
CMD_PROFILE_LIST = 150     # -> 响应配置档列表
CMD_SAVE_PROFILE = 151     # v6+ 带回执；NUL 结尾名字；需 local client 位
CMD_LOAD_PROFILE = 152     # 同上；加载后需重新拉全部控制器数据
CMD_DELETE_PROFILE = 153   # 同上
CMD_RESIZE_ZONE = 1000     # u32 zone + u32 new_size
CMD_UPDATE_LEDS = 1050     # 无响应；u32 size + u16 n + RGBColor×n
CMD_UPDATE_ZONE_LEDS = 1051
CMD_UPDATE_SINGLE_LED = 1052
CMD_SET_CUSTOM_MODE = 1100
CMD_UPDATE_MODE = 1101     # u32 size + i32 mode_idx + Mode 数据块
CMD_SAVE_MODE = 1102

# 客户端标志位（v6+ SET_CLIENT_FLAGS）：请求 local client 权限。
# profile 读写删在 server 端要求该位 + 本地回环连接，否则静默返回 NOT_ALLOWED。
NET_CLIENT_FLAG_REQUEST_LOCAL_CLIENT = 1 << 16

# NetPacketStatus（部分）
STATUS_OK = 0

# mode.flags 位（OpenRGB mode.h ModeFlags，与实测值吻合）+ 中文标签（UI 用）
MODE_FLAG_LABEL = {
    0x0001: "速度",
    0x0002: "方向（左/右）",
    0x0004: "方向（上/下）",
    0x0008: "方向（水平/垂直）",
    0x0010: "亮度",
    0x0020: "自定义颜色",
    0x0040: "随机颜色",
    0x0100: "逐颗颜色",
}

ZONE_TYPE_NAME = {0: "single", 1: "matrix", 2: "linear"}


class OpenRGBError(RuntimeError):
    """SDK 通信失败（连接断开 / 协议不符）。"""


def _port_open(host: str = ORGB_HOST, port: int = ORGB_PORT, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def rgb_ensure_server(wait: float = 30.0) -> dict:
    """确保 OpenRGB SDK server 在跑；不在则 systemctl 拉起并等端口就绪。

    返回 {"ok": bool, "started": bool, "waited": 秒}。拉起需要 root（守护进程满足）。
    """
    if _port_open():
        return {"ok": True, "started": False, "waited": 0.0}
    try:
        subprocess.run(["systemctl", "start", ORGB_SERVICE],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if _port_open():
            return {"ok": True, "started": True,
                    "waited": round(time.monotonic() - t0, 1)}
        time.sleep(0.4)
    return {"ok": False, "started": False, "waited": round(wait, 1)}


def pack_rgb(colors) -> bytes:
    """['#RRGGBB', ...] 或 [(r,g,b), ...] -> RGBColor 流（每颗 4 字节 R,G,B,0）。"""
    out = bytearray()
    for c in colors:
        if isinstance(c, str):
            v = int(c.lstrip("#"), 16) & 0xFFFFFF
            out += bytes(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF, 0))
        else:
            out += bytes((max(0, min(255, int(c[0]))),
                          max(0, min(255, int(c[1]))),
                          max(0, min(255, int(c[2]))), 0))
    return bytes(out)


def rgb_tuples(colors) -> list[tuple[int, int, int]]:
    """任意颜色列表 -> [(r, g, b), ...]。"""
    out = []
    for c in colors:
        if isinstance(c, str):
            v = int(c.lstrip("#"), 16) & 0xFFFFFF
            out.append(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))
        else:
            out.append((max(0, min(255, int(c[0]))),
                        max(0, min(255, int(c[1]))),
                        max(0, min(255, int(c[2])))))
    return out


class OpenRGBSDK:
    """OpenRGB net-protocol v3 客户端。线程安全（互斥锁，连接惰性建立、断线重连）。"""

    def __init__(self, host: str = ORGB_HOST, port: int = ORGB_PORT,
                 name: str = "hwdash", timeout: float = 6.0):
        self.host, self.port, self.name, self.timeout = host, port, name, timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    # ---------- 连接 ----------
    def connect(self) -> None:
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        self._close_locked()
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise OpenRGBError(f"连不上 OpenRGB SDK {self.host}:{self.port}: {e}") from e
        s.settimeout(self.timeout)
        self._sock = s
        try:
            self._raw(self._msg(0, CMD_CLIENT_NAME, self.name.encode() + b"\0"))
            # REQUEST_PROTOCOL_VERSION(40)+u32 会把**会话**协议版本设为该值
            # （ProcessRequest_ClientProtocolVersion），v3 编码由此而来——
            # 不只是探测；profile 等本地特权操作则需升到 v6（见 profile()）。
            self._call_locked(0, CMD_VERSION, pack("<I", _PROTO_VER))
        except OpenRGBError:
            self._close_locked()
            raise

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    # ---------- 帧收发 ----------
    @staticmethod
    def _msg(dev: int, cmd: int, payload: bytes = b"") -> bytes:
        return _MAGIC + pack("<III", dev, cmd, len(payload)) + payload

    def _raw(self, frame: bytes, sock: socket.socket | None = None) -> None:
        sock = sock if sock is not None else self._sock
        if not sock:
            raise OpenRGBError("SDK 未连接")
        try:
            sock.sendall(frame)
        except OSError as e:
            if sock is self._sock:
                self._close_locked()   # 传输即坏：置空让下次请求重连
            raise OpenRGBError(f"SDK 发送失败: {e}") from e

    def _recv_exact(self, n: int, sock: socket.socket | None = None) -> bytes:
        sock = sock if sock is not None else self._sock
        if not sock:
            raise OpenRGBError("SDK 未连接")
        buf = b""
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except (OSError, socket.timeout) as e:
                if sock is self._sock:
                    self._close_locked()
                raise OpenRGBError(f"SDK 接收失败: {e}") from e
            if not chunk:
                if sock is self._sock:
                    self._close_locked()
                raise OpenRGBError("SDK 连接被对端关闭")
            buf += chunk
        return buf

    def _call_locked(self, dev: int, cmd: int, payload: bytes = b"") -> bytes:
        """发送请求并等待匹配响应；跳过 server 推送的 LIST_UPDATED(100) 与乱序帧。"""
        self._raw(self._msg(dev, cmd, payload))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            magic, rdev, rcmd, rsize = unpack("<4sIII", self._recv_exact(16))
            if magic != _MAGIC:
                self._close_locked()
                raise OpenRGBError("SDK 响应 magic 不符")
            body = self._recv_exact(rsize) if rsize else b""
            if rcmd == CMD_LIST_UPDATED:
                continue
            if rcmd == cmd and rdev == dev:
                return body
        self._close_locked()   # 流已错位，丢弃重连最稳妥
        raise OpenRGBError("SDK 响应超时")

    def _ensure_locked(self) -> None:
        if not self._sock:
            self._connect_locked()

    # ---------- 只读探测 ----------
    def controller_count(self) -> int:
        with self._lock:
            self._ensure_locked()
            blob = self._call_locked(0, CMD_COUNT)
        return unpack("<I", blob[:4])[0] if len(blob) >= 4 else 0

    def controller(self, idx: int) -> dict:
        with self._lock:
            self._ensure_locked()
            blob = self._call_locked(idx, CMD_DATA, pack("<I", _PROTO_VER))
        try:
            return self._parse_controller(idx, blob)
        except OpenRGBError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OpenRGBError(f"控制器 {idx} 数据解析失败: {exc}") from exc

    def controllers(self) -> list[dict]:
        n = self.controller_count()
        return [self.controller(i) for i in range(n)]

    @staticmethod
    def _parse_controller(idx: int, blob: bytes) -> dict:
        off = 0

        def u32() -> int:
            nonlocal off
            v = unpack_from("<I", blob, off)[0]
            off += 4
            return v

        def i32() -> int:
            nonlocal off
            v = unpack_from("<i", blob, off)[0]
            off += 4
            return v

        def u16() -> int:
            nonlocal off
            v = unpack_from("<H", blob, off)[0]
            off += 2
            return v

        def cstr() -> str:
            nonlocal off
            n = u16()
            s = blob[off:off + n]
            off += n
            return s.split(b"\0", 1)[0].decode("utf-8", "replace")

        def color() -> str:
            nonlocal off
            c = blob[off:off + 3].hex().upper()
            off += 4
            return f"#{c}"

        u32()  # data_size（块自身字节数，校验用）
        dev: dict = {"idx": idx, "type": i32(), "name": cstr(),
                     "vendor": cstr(), "description": cstr(), "version": cstr(),
                     "serial": cstr(), "location": cstr(),
                     "active_mode": 0, "modes": [], "zones": [], "leds": [], "colors": []}
        nmodes = u16()
        dev["active_mode"] = i32()
        for m in range(nmodes):
            mode = {"idx": m, "name": cstr(), "value": i32(), "flags": u32(),
                    "speed_min": u32(), "speed_max": u32(),
                    "bright_min": u32(), "bright_max": u32(),
                    "colors_min": u32(), "colors_max": u32(),
                    "speed": u32(), "bright": u32(),
                    "direction": u32(), "color_mode": u32()}
            mode["colors"] = [color() for _ in range(u16())]
            dev["modes"].append(mode)
        for z in range(u16()):
            zone = {"idx": z, "name": cstr(), "type": ZONE_TYPE_NAME.get(i32(), "linear"),
                    "leds_min": u32(), "leds_max": u32(), "leds_count": u32(),
                    "matrix": None}
            mlen = u16()
            if mlen >= 8:
                mh, mw = u32(), u32()
                zone["matrix"] = [[unpack_from("<I", blob, off + 4 * (r * mw + c))[0]
                                   for c in range(mw)] for r in range(mh)]
                off += mlen - 8
            elif mlen:
                off += mlen
            dev["zones"].append(zone)
        for i in range(u16()):
            dev["leds"].append({"idx": i, "name": cstr(), "value": u32()})
        dev["colors"] = [color() for _ in range(u16())]
        if off != len(blob):
            raise OpenRGBError(f"控制器 {idx} 数据长度不匹配（消费 {off}/{len(blob)}）")
        return dev

    # ---------- 写操作（server 静默；需要确认由调用方回读） ----------
    def set_mode(self, dev: int, mode: dict, colors=None, speed=None,
                 brightness=None, direction=None) -> None:
        """激活模式并顺带下发参数。mode 取自 controller()["modes"] 的一项。"""
        colors = [str(c) for c in (colors if colors is not None else mode["colors"])]
        if not colors and mode["colors_max"] > 0:
            colors = ["FF0000"]
        cmax = max(mode["colors_max"], len(colors))
        lo, hi = mode["speed_min"], mode["speed_max"]
        sp = int(mode["speed"] if speed is None else speed)
        if hi > lo:
            sp = max(lo, min(hi, sp))
        blo, bhi = mode["bright_min"], mode["bright_max"]
        br = int(mode["bright"] if brightness is None else brightness)
        if bhi > blo:
            br = max(blo, min(bhi, br))
        body = pack("<H", len(mode["name"].encode("utf-8")) + 1)
        body += mode["name"].encode("utf-8") + b"\0"
        body += pack("<i", mode["value"]) + pack("<I", mode["flags"])
        body += pack("<II", lo, hi) + pack("<II", blo, bhi)
        body += pack("<II", mode["colors_min"], cmax)
        body += pack("<II", sp, br)
        body += pack("<I", mode["direction"] if direction is None else direction)
        body += pack("<I", mode["color_mode"])
        body += pack("<H", len(colors)) + pack_rgb(colors)
        # 自描述 data_size 含自身 4 字节 + mode_idx 4 字节 + mode 块
        with self._lock:
            self._ensure_locked()
            self._raw(self._msg(dev, CMD_UPDATE_MODE,
                                pack("<I", 8 + len(body)) + pack("<i", mode["idx"]) + body))

    def set_custom_mode(self, dev: int) -> None:
        with self._lock:
            self._ensure_locked()
            self._raw(self._msg(dev, CMD_SET_CUSTOM_MODE))

    def update_leds(self, dev: int, colors) -> None:
        """整设备写全部灯珠颜色（需先处于 Direct/Custom 模式）。

        payload = 自描述 data_size(u32，含自身) + u16 数量 + RGBColor×n。
        """
        body = pack("<H", len(colors)) + pack_rgb(colors)
        with self._lock:
            self._ensure_locked()
            self._raw(self._msg(dev, CMD_UPDATE_LEDS, pack("<I", 4 + len(body)) + body))

    def update_zone_leds(self, dev: int, zone_idx: int, colors) -> None:
        body = pack("<IH", zone_idx, len(colors)) + pack_rgb(colors)
        with self._lock:
            self._ensure_locked()
            self._raw(self._msg(dev, CMD_UPDATE_ZONE_LEDS, pack("<I", 4 + len(body)) + body))

    def update_single_led(self, dev: int, led_idx: int, rgb) -> None:
        r, g, b = rgb_tuples([rgb])[0]
        with self._lock:
            self._ensure_locked()
            self._raw(self._msg(dev, CMD_UPDATE_SINGLE_LED, pack("<i", led_idx) + pack_rgb([(r, g, b)])))

    def resize_zone(self, dev: int, zone_idx: int, size: int) -> None:
        with self._lock:
            self._ensure_locked()
            self._raw(self._msg(dev, CMD_RESIZE_ZONE, pack("<II", zone_idx, size)))

    def profile(self, action: str, name: str) -> None:
        """action: save / load / delete。load 之后调用方应重新拉全部控制器数据。

        server 端（NetworkServer.cpp）对 profile 读写删要求
        `client_is_local_client`，而该位只在 **协议 v6** 会话里通过
        SET_CLIENT_FLAGS(52) 带 NET_CLIENT_FLAG_REQUEST_LOCAL_CLIENT 才能拿到；
        v3 会话一律静默 NOT_ALLOWED 且无回执（ACK 仅 v6+ 发送）。
        因此这里用独立短连接走 v6 握手——主连接保持 v3 不受影响——
        并读取 ACK 确认真实执行结果。
        """
        cmd = {"save": CMD_SAVE_PROFILE, "load": CMD_LOAD_PROFILE,
               "delete": CMD_DELETE_PROFILE}.get(action)
        if cmd is None:
            raise OpenRGBError(f"未知 profile 操作: {action}")
        with self._lock:
            s = socket.create_connection((self.host, self.port),
                                         timeout=self.timeout)
            try:
                s.settimeout(self.timeout)

                def xchg(c: int, payload: bytes = b"",
                         expect: int | None = None) -> bytes:
                    """在短连接上收发一帧，返回匹配响应体（expect=响应命令码）。"""
                    s.sendall(self._msg(0, c, payload))
                    want = c if expect is None else expect
                    deadline = time.monotonic() + self.timeout
                    while time.monotonic() < deadline:
                        magic, rdev, rcmd, rsize = unpack(
                            "<4sIII", self._recv_exact(16, sock=s))
                        body = self._recv_exact(rsize, sock=s) if rsize else b""
                        if magic != _MAGIC:
                            raise OpenRGBError("profile 握手 magic 不符")
                        if rcmd == want and rdev == 0:
                            return body
                    raise OpenRGBError("profile 握手响应超时")

                s.sendall(self._msg(0, CMD_CLIENT_NAME,
                                    self.name.encode("utf-8") + b"\0"))  # 无响应
                # 会话协议版本由 REQUEST_PROTOCOL_VERSION(40)+u32 设定
                # （ProcessRequest_ClientProtocolVersion），不是 DATA！
                xchg(CMD_VERSION, pack("<I", 6))
                xchg(CMD_SET_CLIENT_FLAGS,
                     pack("<I", NET_CLIENT_FLAG_REQUEST_LOCAL_CLIENT),
                     expect=CMD_SET_SERVER_FLAGS)        # 响应帧是 53 号

                # 写操作本身无直接响应，结果经 ACK(10) 返回
                s.sendall(self._msg(0, cmd, name.encode("utf-8") + b"\0"))
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    magic, rdev, rcmd, rsize = unpack(
                        "<4sIII", self._recv_exact(16, sock=s))
                    body = self._recv_exact(rsize, sock=s) if rsize else b""
                    if magic != _MAGIC:
                        raise OpenRGBError("profile 响应 magic 不符")
                    if rcmd == CMD_ACK and rsize >= 8:
                        acked, status = unpack("<II", body[:8])
                        if acked == cmd:
                            if status != STATUS_OK:
                                raise OpenRGBError(
                                    f"配置档{action}被 server 拒绝（status={status}，"
                                    f"需 v6 + local client + 回环连接）")
                            return
                    # 其余帧（LIST_UPDATED 等）跳过
                raise OpenRGBError("配置档操作响应超时")
            finally:
                try:
                    s.close()
                except OSError:
                    pass


# ---------------------------------------------------------------- 探针

if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="OpenRGB SDK 探针")
    ap.add_argument("--probe", action="store_true", help="打印全部控制器结构")
    ap.add_argument("--mode", type=int, default=None, metavar="N",
                    help="设备 0 切到模式 N，2 秒后切回 Direct")
    ap.add_argument("--paint", type=int, default=None, metavar="ZONE",
                    help="对灯区 ZONE 逐颗涂彩虹 2 秒后还原")
    args = ap.parse_args()

    if not _port_open():
        print(json.dumps(rgb_ensure_server(), ensure_ascii=False))
    sdk = OpenRGBSDK()
    try:
        sdk.connect()
        devs = sdk.controllers()
        if args.probe or not (args.mode is not None or args.paint is not None):
            print(json.dumps(devs, ensure_ascii=False, indent=1))
        if args.mode is not None and devs:
            d = devs[0]
            orig = d["active_mode"]
            sdk.set_mode(0, d["modes"][args.mode])
            time.sleep(1.0)
            now = sdk.controller(0)["active_mode"]
            print(f"set_mode({args.mode}) -> active_mode={now} "
                  f"({'生效' if now == args.mode else '未生效!'})")
            time.sleep(1.0)
            sdk.set_mode(0, d["modes"][orig])
            print(f"已切回原模式 {orig}（{d['modes'][orig]['name']}）")
        if args.paint is not None and devs:
            d = devs[0]
            z = d["zones"][args.paint]
            base = z["leds_min"]
            orig = d["colors"][base:base + z["leds_count"]]
            sdk.set_custom_mode(0)
            n = z["leds_count"]
            rainbow = ["#{:06X}".format(int(0xFFFFFF * i / max(1, n - 1)) & 0xFFFFFF)
                       for i in range(n)]
            for k in range(4):
                sdk.update_zone_leds(0, args.paint, rainbow[k % 2:] + rainbow[:k % 2])
                time.sleep(0.5)
            sdk.update_zone_leds(0, args.paint, orig)
            print(f"paint: 灯区 {z['name']}（{n} 颗）涂彩虹并已还原")
    except OpenRGBError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        sdk.close()
