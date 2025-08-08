#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fan controller with temperature curves and verbose logging.

- 风扇路数：
  pcie / chassis / cpu / hdd
- 温度来源：
  * LSI 9361-8i: storcli /c0 show temperature -> ROC
  * 其他 PCIe: sensors -j 的 jc42-* temp1_input
  * CPU: sensors -j 的 k10temp-* Tctl_input
  * HDD(仅 SATA): smartctl -A 读取 190/194 温度，忽略 NVMe
- 日志：
  * 详尽记录每一步（采样/计算/下发），RotatingFileHandler
  * 单文件最大 5MB，最多 3 份（当前 + 2 个历史）
- 需要工具：ipmitool, lm-sensors, smartmontools, (可选) storcli
- 建议 root 运行
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Tuple, Optional

# ------------------ 你的修正后 RAW 命令（最后一字节为速度 0x00~0x64） ------------------
# ipmitool raw 0x2e 0x44 0xfd 0x19 0x00 <IDX> 0x01 0xNN
FAN_CMDS = {
    "pcie":   ["0x2e","0x44","0xfd","0x19","0x00","0x04","0x01"],
    "chassis":["0x2e","0x44","0xfd","0x19","0x00","0x02","0x01"],
    "cpu":    ["0x2e","0x44","0xfd","0x19","0x00","0x00","0x01"],
    "hdd":    ["0x2e","0x44","0xfd","0x19","0x00","0x03","0x01"],
}

# ------------------ 默认温度曲线 (°C -> %) 可通过 --config 覆盖 ------------------
DEFAULT_CURVES = {
    "pcie":   [(40,20), (65,50), (75,80), (80,100)],
    "cpu":    [(40,15), (55,40), (70,70), (80,100)],
    "hdd":    [(30,20), (40,35), (50,60), (55,100)],
}

# 最小转速、防抖、增压、兜底
MIN_PCT = { "pcie": 15, "chassis": 15, "cpu": 15, "hdd": 20 }
BOOST_CHASSIS = 10   # 机箱风扇对 PCIE/CPU 气流增压
SMOOTH_STEP = 5      # 每周期最大变化百分比
POLL_INTERVAL = 5    # 秒
CRITICAL = { "pcie": 85, "cpu": 85, "hdd": 57 }  # 超过拉满 100%

# 日志默认：/var/log/fanctl.log（没权限自动回退到 ./fanctl.log）
DEFAULT_LOG = "/var/log/fanctl.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5MB
LOG_BACKUPS = 2                   # 2 个历史 + 1 个当前 = 共 3 份

STATE_LAST = {k: None for k in FAN_CMDS.keys()}
log = logging.getLogger("fanctl")

def setup_logging(log_file: Optional[str], max_bytes: int, backups: int, foreground: bool, level: int):
    log_path = log_file or DEFAULT_LOG
    # 如果 /var/log 不可写，回退
    if os.path.dirname(log_path) and not os.access(os.path.dirname(log_path), os.W_OK):
        log_path = os.path.abspath("./fanctl.log")

    log.setLevel(level)
    fmt = logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    if foreground:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        ch.setLevel(level)
        log.addHandler(ch)

    log.info("==== fanctl started ====")
    log.info("log_file=%s max_bytes=%d backups=%d foreground=%s", log_path, max_bytes, backups, foreground)

def run(cmd: List[str], timeout: int = 8, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=check, text=True)

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

# -------------------------- 传感器读取 --------------------------

def read_storcli_roc() -> Optional[float]:
    exe = which("storcli64") or which("storcli")
    if not exe:
        log.debug("storcli not found")
        return None
    try:
        cmd = [exe, "/c0", "show", "temperature"]
        log.debug("exec: %s", shlex.join(cmd))
        out = run(cmd).stdout
        m = re.search(r"ROC temperature.*?(\d+)", out)
        val = float(m.group(1)) if m else None
        log.info("storcli ROC temp = %s", val)
        return val
    except Exception as e:
        log.warning("storcli read failed: %s", e)
        return None

def sensors_json() -> Optional[dict]:
    if not which("sensors"):
        log.warning("sensors not found")
        return None
    try:
        cmd = ["sensors","-j"]
        log.debug("exec: %s", shlex.join(cmd))
        out = run(cmd).stdout
        data = json.loads(out)
        return data
    except Exception as e:
        log.warning("sensors -j failed: %s", e)
        return None

def get_cpu_tctl(sj: Optional[dict]) -> Optional[float]:
    if not sj: return None
    for chip, data in sj.items():
        if chip.startswith("k10temp-"):
            tctl = data.get("Tctl", {})
            for k, v in tctl.items():
                if k.endswith("_input"):
                    try:
                        val = float(v)
                        log.info("CPU Tctl = %s", val)
                        return val
                    except: pass
    log.debug("CPU Tctl not found")
    return None

def get_jc42_max(sj: Optional[dict]) -> Optional[float]:
    if not sj: return None
    vals = []
    for chip, data in sj.items():
        if chip.startswith("jc42-"):
            sec = data.get("temp1", {}) if "temp1" in data else data
            for k, v in sec.items():
                if k.endswith("_input"):
                    try: vals.append(float(v))
                    except: pass
    if vals:
        val = max(vals)
        log.info("JC42 max = %s from %s", val, vals)
        return val
    log.debug("JC42 temps not found")
    return None

def list_sata_disks() -> List[str]:
    if not which("lsblk"):
        log.warning("lsblk not found")
        return []
    try:
        out = run(["lsblk","-dn","-o","NAME,TRAN"]).stdout.strip().splitlines()
        devs = []
        for line in out:
            parts = line.split()
            if len(parts)>=2 and parts[1].lower()=="sata":
                devs.append("/dev/"+parts[0])
        log.info("SATA disks: %s", devs)
        return devs
    except Exception as e:
        log.warning("lsblk failed: %s", e)
        return []

def smartctl_temp(dev: str) -> Optional[float]:
    if not which("smartctl"):
        log.warning("smartctl not found")
        return None
    try:
        cmd = ["smartctl","-A", dev]
        log.debug("exec: %s", shlex.join(cmd))
        out = run(cmd).stdout
        cand = []
        for line in out.splitlines():
            if re.search(r"\b(Temperature|Airflow_Temperature)\b", line, re.I):
                nums = [int(x) for x in re.findall(r"(\d+)", line)]
                if nums:
                    cand.append(nums[-1])
        val = float(max(cand)) if cand else None
        log.info("HDD temp %s = %s (candidates=%s)", dev, val, cand)
        return val
    except Exception as e:
        log.warning("smartctl failed on %s: %s", dev, e)
        return None

# -------------------------- 曲线/控制 --------------------------

def lerp_curve(points: List[Tuple[float,float]], x: float) -> float:
    points = sorted(points, key=lambda t: t[0])
    if x <= points[0][0]: return points[0][1]
    if x >= points[-1][0]: return points[-1][1]
    for (x0,y0),(x1,y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1==x0: return y1
            t = (x - x0)/(x1 - x0)
            return y0 + t*(y1 - y0)
    return points[-1][1]

def clamp(v, lo, hi): return max(lo, min(hi, v))

def smooth(prev: Optional[int], target: int, step: int=SMOOTH_STEP) -> int:
    if prev is None: return target
    if target > prev: return min(prev + step, target)
    if target < prev: return max(prev - step, target)
    return prev

def set_fan_pct(name: str, pct: int, dry: bool=False) -> None:
    pct = clamp(int(round(pct)), 0, 100)
    hexpct = f"0x{pct:02x}"
    cmd = ["ipmitool","raw"] + FAN_CMDS[name] + [hexpct]
    log.info("SET %s -> %d%% (%s)", name, pct, " ".join(cmd))
    STATE_LAST[name] = pct
    if dry:
        log.info("[DRY-RUN] skip ipmitool for %s", name)
        return
    try:
        r = run(cmd, check=False)
        if r.returncode != 0:
            log.warning("ipmitool %s -> %d%% failed: %s | %s", name, pct, r.stdout.strip(), r.stderr.strip())
        else:
            log.info("ipmitool %s -> %d%% ok: %s", name, pct, r.stdout.strip())
    except Exception as e:
        log.error("ipmitool %s -> %d%% exception: %s", name, pct, e)

def compute_targets(curves, sj, dry, verbose=False) -> Dict[str,int]:
    # 采样
    log.info("=== sampling begin ===")
    roc = read_storcli_roc()
    jcmax = get_jc42_max(sj)
    cpu = get_cpu_tctl(sj)

    hdds = list_sata_disks()
    htemps = []
    for d in hdds:
        t = smartctl_temp(d)
        if t is not None:
            htemps.append(t)
    hdd = max(htemps) if htemps else None

    log.info("sampled: roc=%s jc42_max=%s cpu=%s hdd_max=%s", roc, jcmax, cpu, hdd)

    # 计算 PCIe 热点
    pcie_hot = None
    for v in (roc, jcmax):
        if v is not None:
            pcie_hot = v if pcie_hot is None else max(pcie_hot, v)
    log.info("pcie_hot = %s", pcie_hot)

    # 曲线映射
    pcie_pct = lerp_curve(curves["pcie"], pcie_hot) if pcie_hot is not None else MIN_PCT["pcie"]
    cpu_pct  = lerp_curve(curves["cpu"], cpu)       if cpu       is not None else MIN_PCT["cpu"]
    hdd_pct  = lerp_curve(curves["hdd"], hdd)       if hdd       is not None else MIN_PCT["hdd"]

    # 机箱 = max(PCIE, CPU) + 加成
    chassis_pct = clamp(max(cpu_pct, pcie_pct) + BOOST_CHASSIS, 0, 100)

    # —— 软上限：先卡 50%，后面兜底可突破 ——
    SOFT_CAP = 50
    before_cap = chassis_pct
    chassis_pct = min(chassis_pct, SOFT_CAP)
    if chassis_pct < before_cap:
        log.info("apply soft cap: chassis %d%% -> %d%% (cap=%d%%)", before_cap, chassis_pct, SOFT_CAP)

    log.info("curve raw: pcie=%s cpu=%s hdd=%s chassis=%s(+boost,soft-cap)",
             pcie_pct, cpu_pct, hdd_pct, chassis_pct)

    # 兜底（可突破软上限）
    if (pcie_hot is not None and pcie_hot >= CRITICAL["pcie"]):
        pcie_pct = 100
        chassis_pct = max(chassis_pct, 100)
        log.warning("pcie critical -> pcie=100%% & chassis>=100%%")
    if (cpu is not None and cpu >= CRITICAL["cpu"]):
        cpu_pct = 100
        chassis_pct = max(chassis_pct, 100)
        log.warning("cpu critical -> cpu=100%% & chassis>=100%%")
    if (hdd is not None and hdd >= CRITICAL["hdd"]):
        hdd_pct = 100
        log.warning("hdd critical -> hdd=100%%")

    # 最小转速下限
    pcie_pct   = max(pcie_pct,   MIN_PCT["pcie"])
    cpu_pct    = max(cpu_pct,    MIN_PCT["cpu"])
    hdd_pct    = max(hdd_pct,    MIN_PCT["hdd"])
    chassis_pct= max(chassis_pct,MIN_PCT["chassis"])

    # 平滑
    targets = {
        "pcie":    smooth(STATE_LAST["pcie"],    int(round(pcie_pct))),
        "cpu":     smooth(STATE_LAST["cpu"],     int(round(cpu_pct))),
        "hdd":     smooth(STATE_LAST["hdd"],     int(round(hdd_pct))),
        "chassis": smooth(STATE_LAST["chassis"], int(round(chassis_pct))),
    }
    log.info("targets smoothed: %s", targets)
    log.info("=== sampling end ===")
    return targets

def load_curves(path: Optional[str]) -> Dict[str, List[Tuple[float,float]]]:
    if not path: return DEFAULT_CURVES
    with open(path, "r", encoding="utf-8") as f:
        curves = json.load(f)
    for k in ["pcie","cpu","hdd"]:
        if k not in curves: raise ValueError(f"curve '{k}' missing")
        if not isinstance(curves[k], list): raise ValueError(f"curve '{k}' must be list of [temp, pct]")
    return curves

def usage():
    print(f"""Usage:
  sudo {sys.argv[0]} [--interval {POLL_INTERVAL}] [--config /path/curves.json] [--dry-run] [--once]
                    [--foreground] [--log-file /var/log/fanctl.log] [--log-max-mb 5] [--log-backups 2]
                    [--verbose]
  sudo {sys.argv[0]} --set FAN PCT    # FAN in pcie|chassis|cpu|hdd, PCT 0-100

Notes:
- 日志按大小轮转：默认 5MB，备份 2 个（总 3 份：当前+2 历史）。
- 如果想要“总计 4 份（当前+3 历史）”，把 --log-backups 设为 3 即可。
""")

def main():
    import argparse
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL)
    ap.add_argument("--config", type=str, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--foreground", action="store_true", help="同时在前台打印日志")
    ap.add_argument("--log-file", type=str, default=None)
    ap.add_argument("--log-max-mb", type=int, default=5)
    ap.add_argument("--log-backups", type=int, default=LOG_BACKUPS)
    ap.add_argument("--set", nargs=2, metavar=("FAN","PCT"))
    args, unknown = ap.parse_known_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(args.log_file, args.log_max_mb*1024*1024, args.log_backups, args.foreground, level)

    if args.set:
        fan, pct = args.set[0].lower(), int(args.set[1])
        if fan not in FAN_CMDS:
            log.error("FAN must be one of: %s", ", ".join(FAN_CMDS.keys()))
            sys.exit(1)
        log.info("manual set: %s -> %d%% (dry=%s)", fan, pct, args.dry_run)
        set_fan_pct(fan, pct, dry=args.dry_run)
        return

    try:
        curves = load_curves(args.config)
        log.info("curves=%s", curves)
        log.info("min=%s boost_chassis=%s step=%s", MIN_PCT, BOOST_CHASSIS, SMOOTH_STEP)
    except Exception as e:
        log.error("load curves failed: %s", e)
        sys.exit(1)

    while True:
        sj = sensors_json()
        targets = compute_targets(curves, sj, args.dry_run, verbose=args.verbose)
        # 先机箱再分区
        for fan in ("chassis","pcie","cpu","hdd"):
            set_fan_pct(fan, targets[fan], dry=args.dry_run)
        if args.once: break
        log.info("sleep %ds ...", max(1, args.interval))
        time.sleep(max(1, args.interval))

if __name__ == "__main__":
    if len(sys.argv)==1:
        usage()
    main()
