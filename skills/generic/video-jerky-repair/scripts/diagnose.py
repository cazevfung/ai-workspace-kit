#!/usr/bin/env python3
"""视频卡顿/掉帧诊断：给定一个视频文件，输出 JSON。

只读，不改素材。三个必需键：
  dup_ratio       重复帧比例（与前一帧几乎逐像素相同的帧占全部帧的比例）
  vfr             是否可变帧率（true / false）
  recommendation  建议路线，取值见下

recommendation 的取值与含义：
  cfr-normalize   时间戳不均匀（VFR）：先归一成恒定帧率，别急着补帧
  dedup-fill      有大量重复帧（掉帧/一拍二/编码器补帧）：按位置原地插帧补位
  interpolate     重复帧不多但运动不连贯（帧率偏低）：整体补帧到更高帧率
  none            没检出上述问题：不要修，修了只会引入伪影

判据都写在 JSON 里（dups_short_ratio / motion / fps），可以人工复核。

依赖：ffmpeg、ffprobe 在 PATH。numpy 可选（没有就退化成纯 Python 逐字节比对，慢一些）。
用法：
    python diagnose.py <视频> [--dup-thresh 0.35] [--fps-hint 25] [--pretty]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

try:  # numpy 只是加速，不是硬依赖
    import numpy as np
except Exception:  # pragma: no cover
    np = None


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        sys.exit(f"找不到 {tool}，请先安装并在 PATH 里")
    return path


def ffprobe_json(path: str, args: list[str]) -> dict:
    cmd = [need("ffprobe"), "-v", "error", "-of", "json"] + args + [path]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        sys.exit(f"ffprobe 失败：{out.stderr.strip()}")
    return json.loads(out.stdout or "{}")


def probe_stream(path: str) -> dict:
    data = ffprobe_json(path, [
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,r_frame_rate,"
        "avg_frame_rate,time_base,nb_frames,duration",
    ])
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    sys.exit(f"{path} 里没有视频轨")


def frame_times(path: str, limit: int = 5000) -> list[float]:
    """取前 limit 帧的显示时间戳，用来判 VFR。"""
    data = ffprobe_json(path, [
        "-select_streams", "v:0",
        "-show_entries", "frame=best_effort_timestamp_time",
        "-read_intervals", f"%+#{limit}",
    ])
    times = []
    for f in data.get("frames", []):
        t = f.get("best_effort_timestamp_time")
        if t is not None:
            times.append(float(t))
    return times


def is_vfr(times: list[float]) -> tuple[bool, float]:
    """时间戳间隔是否明显不均匀。返回 (是否 VFR, 间隔相对离散度)。"""
    if len(times) < 3:
        return False, 0.0
    deltas = [round(times[i + 1] - times[i], 6) for i in range(len(times) - 1)]
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        return False, 0.0
    mean = sum(deltas) / len(deltas)
    var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    rel = (var ** 0.5) / mean if mean else 1.0
    # 抖动超过 1% 或出现两种以上明显不同的间隔，就当 VFR
    distinct = {round(d / mean, 3) for d in deltas}
    return (rel > 0.01 or len(distinct) > 1), round(rel, 6)


def frame_diffs(path: str, width: int = 64, height: int = 80, fps: str | None = None):
    """把画面缩成小灰度图逐帧求「与上一帧的平均绝对差」。

    返回 (diffs, frames)。diffs[i] 是第 i+1 帧与第 i 帧的差。
    """
    vf = f"scale={width}:{height}:flags=area,format=gray"
    if fps:
        vf = f"fps={fps}," + vf
    cmd = [need("ffmpeg"), "-v", "error", "-i", path, "-an", "-sn",
           "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    n = width * height
    diffs: list[float] = []
    count = 0
    prev = None
    while True:
        buf = p.stdout.read(n)
        if len(buf) < n:
            break
        count += 1
        if np is not None:
            cur = np.frombuffer(buf, dtype=np.uint8).astype(np.int16)
            if prev is not None:
                diffs.append(float(np.abs(cur - prev).mean()))
            prev = cur
        else:  # 纯 Python 兜底
            if prev is not None:
                diffs.append(sum(abs(a - b) for a, b in zip(buf, prev)) / n)
            prev = buf
    p.stdout.close()
    err = p.stderr.read().decode("utf-8", "replace").strip()
    p.wait()
    if p.returncode != 0:
        sys.exit(f"ffmpeg 解码失败：{err}")
    return diffs, count


def run_lengths(flags: list[bool]) -> list[int]:
    out = [0] * len(flags)
    i = 0
    while i < len(flags):
        if flags[i]:
            j = i
            while j < len(flags) and flags[j]:
                j += 1
            for k in range(i, j):
                out[k] = j - i
            i = j
        else:
            i += 1
    return out


def diagnose(path: str, dup_thresh: float, fps_hint: float | None) -> dict:
    if not os.path.exists(path):
        sys.exit(f"文件不存在：{path}")
    st = probe_stream(path)
    times = frame_times(path)
    vfr, jitter = is_vfr(times)

    num, den = (st.get("avg_frame_rate") or "0/1").split("/")
    avg_fps = float(num) / float(den) if float(den) else 0.0
    num, den = (st.get("r_frame_rate") or "0/1").split("/")
    r_fps = float(num) / float(den) if float(den) else 0.0
    fps = round(avg_fps or r_fps or (fps_hint or 25.0), 3)

    diffs, frames = frame_diffs(path)
    flags = [d < dup_thresh for d in diffs]
    rl = run_lengths(flags)
    dup_frames = sum(1 for f in flags if f)
    short_dup = sum(1 for i, f in enumerate(flags) if f and rl[i] <= 4)
    long_dup = sum(1 for i, f in enumerate(flags) if f and rl[i] >= 5)
    total = len(diffs) + 1 if frames == 0 else frames
    dup_ratio = round(dup_frames / total, 4) if total else 0.0
    short_ratio = round(short_dup / dup_frames, 4) if dup_frames else 0.0
    long_ratio = round(long_dup / dup_frames, 4) if dup_frames else 0.0
    motion = round(sum(diffs) / len(diffs), 3) if diffs else 0.0

    # 推荐路线：先看时间轴，再看重复帧，最后才谈补帧
    if vfr:
        rec = "cfr-normalize"
        why = (f"时间戳间隔不均匀（相对抖动 {jitter}），先把帧率归一，"
               f"否则任何补帧都会按错误的间隔插")
    elif dup_ratio >= 0.15:
        rec = "dedup-fill"
        why = (f"重复帧占 {dup_ratio:.1%}（其中短游程 {short_ratio:.0%}）——"
               f"帧内容没变但时间在走，看起来就是卡；按重复帧位置原地插帧补位")
    elif fps <= 24 and motion <= 3.0:
        rec = "interpolate"
        why = (f"重复帧不多（{dup_ratio:.1%}），但帧率只有 {fps} 且画面平均运动量偏低"
               f"（{motion}）——运动不连贯，整体补帧到更高帧率")
    else:
        rec = "none"
        why = (f"没检出明显病因：重复帧 {dup_ratio:.1%}、VFR={vfr}、"
               f"{fps} fps、平均运动量 {motion}")

    return {
        "file": os.path.abspath(path),
        "dup_ratio": dup_ratio,
        "vfr": bool(vfr),
        "recommendation": rec,
        "why": why,
        "details": {
            "dup_thresh": dup_thresh,
            "frames": total,
            "duplicate_frames": dup_frames,
            "dups_short_ratio": short_ratio,
            "dups_long_ratio": long_ratio,
            "motion": motion,
            "fps": fps,
            "r_frame_rate": st.get("r_frame_rate"),
            "avg_frame_rate": st.get("avg_frame_rate"),
            "timing_jitter": jitter,
            "codec": st.get("codec_name"),
            "size": f"{st.get('width')}x{st.get('height')}",
            "duration": st.get("duration"),
            "numpy": np is not None,
        },
    }


def main() -> int:
    try:  # Windows 控制台默认不是 UTF-8，中文说明会变乱码
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="视频卡顿/掉帧诊断（只读）")
    ap.add_argument("video")
    ap.add_argument("--dup-thresh", type=float, default=0.35,
                    help="相邻两帧平均绝对差低于该值算重复帧（默认 0.35）")
    ap.add_argument("--fps-hint", type=float, default=None)
    ap.add_argument("--pretty", action="store_true")
    a = ap.parse_args()
    result = diagnose(a.video, a.dup_thresh, a.fps_hint)
    print(json.dumps(result, ensure_ascii=False,
                     indent=2 if a.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
