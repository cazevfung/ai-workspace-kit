#!/usr/bin/env python3
"""重复帧检测 + 原地插帧补位。

病因：视频里「与前一帧逐像素相同」的帧是掉帧、一拍二或编码器补帧留下的空位。
帧内容没变、时间却在走，看起来就是卡。

做法：只在那些空位上补一帧真正运动中的中间帧，其余帧原样保留，时间轴长度不变。
不是整体补帧到高帧率，也不是整体变速。

实现：
  1. 用本目录的 diagnose.py 同样的度量找出重复帧（游程相同的连续重复帧算一个空位组）；
  2. 把整段拆成 PNG 帧；
  3. 每个空位组只取它前后两张「真帧」，交给 rife-ncnn-vulkan 在这两张之间生成
     （空位长度 + 2）张均匀中间帧，取中间那几张填进原位；
  4. 按原帧率重新装帧。

为什么用 RIFE 而不是 ffmpeg 的 minterpolate：minterpolate 按「输入声明的帧率」决定
插几帧，摘掉重复帧之后声明帧率仍是原帧率，它不会补；逐空位跑 RIFE 才能做到只补
该补的位置、补几张由空位长度决定。

依赖：ffmpeg/ffprobe 在 PATH，rife-ncnn-vulkan 可执行文件（nihui 版，免装 CUDA）。

用法：
    python dedup_fill.py <输入> <输出> --rife <rife-ncnn-vulkan.exe>
        [--model rife-v4.6] [--fps 25] [--dup-thresh 0.35] [--crf 14]
        [--tmp .tmp/dedup]
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diagnose import frame_diffs, need, run_lengths  # noqa: E402


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"命令失败：{' '.join(cmd)}\n{r.stderr[-1500:]}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--rife", required=True, help="rife-ncnn-vulkan 可执行文件")
    ap.add_argument("--model", default="rife-v4.6")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--dup-thresh", type=float, default=0.35)
    ap.add_argument("--crf", type=int, default=14)
    ap.add_argument("--tmp", default=".tmp/dedup")
    a = ap.parse_args()

    if not os.path.exists(a.src):
        sys.exit(f"文件不存在：{a.src}")
    rife_exe = os.path.abspath(a.rife)
    model_dir = os.path.join(os.path.dirname(rife_exe), a.model)

    diffs, frames = frame_diffs(a.src)
    flags = [False] + [d < a.dup_thresh for d in diffs]
    flags = flags[:frames]
    rl = run_lengths(flags)
    dups = [i for i, f in enumerate(flags) if f]
    print(f"帧数 {frames}，重复帧 {len(dups)}（{len(dups)/frames:.1%}），"
          f"短游程 {sum(1 for i in dups if rl[i] <= 4)}"
          f"，长游程 {sum(1 for i in dups if rl[i] >= 5)}")

    work = os.path.abspath(a.tmp)
    if os.path.isdir(work):
        shutil.rmtree(work)
    src_dir = os.path.join(work, "src")
    fill_dir = os.path.join(work, "fill")
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(fill_dir, exist_ok=True)

    run(["ffmpeg", "-v", "error", "-i", a.src, "-vsync", "0",
         os.path.join(src_dir, "%06d.png")])
    src_files = sorted(glob.glob(os.path.join(src_dir, "*.png")))
    if len(src_files) != frames:
        sys.exit(f"拆帧数量对不上：视频 {frames} 帧，落盘 {len(src_files)} 张")

    # 空位组：[start, end]（含），组内每一帧都与前一帧相同
    gaps = []
    i = 0
    while i < len(flags):
        if flags[i]:
            j = i
            while j < len(flags) and flags[j]:
                j += 1
            gaps.append((i, j - 1))
            i = j
        else:
            i += 1

    fills: dict[int, str] = {}
    for gi, (lo, hi) in enumerate(gaps):
        left = lo - 1          # 左真帧
        right = hi + 1         # 右真帧
        n_fill = hi - lo + 1   # 这个空位要补几帧
        if left < 0 or right >= frames:
            continue           # 段首/段尾的空位没有两侧真帧，补不了，原样保留
        pair_dir = os.path.join(work, f"pair{gi:04d}")
        out_dir = os.path.join(work, f"pair{gi:04d}_out")
        os.makedirs(pair_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)
        shutil.copy(src_files[left], os.path.join(pair_dir, "0001.png"))
        shutil.copy(src_files[right], os.path.join(pair_dir, "0002.png"))
        want = n_fill + 2
        run([rife_exe, "-i", pair_dir, "-o", out_dir, "-n", str(want),
             "-m", model_dir])
        made = sorted(glob.glob(os.path.join(out_dir, "*.png")))
        if len(made) < want:
            sys.exit(f"空位 {gi}：要 {want} 张，实际 {len(made)} 张")
        for k in range(n_fill):
            fills[lo + k] = made[1 + k]   # 0 是左真帧，最后一张是右真帧

    seq_dir = os.path.join(work, "seq")
    os.makedirs(seq_dir, exist_ok=True)
    n_out = 0
    for idx in range(frames):
        if flags[idx] and idx in fills:
            srcp = fills[idx]
        elif flags[idx]:
            srcp = src_files[idx]
        else:
            srcp = src_files[idx]
        n_out += 1
        shutil.copy(srcp, os.path.join(seq_dir, f"{n_out:06d}.png"))

    run(["ffmpeg", "-v", "error", "-y", "-framerate", f"{a.fps:g}",
         "-i", os.path.join(seq_dir, "%06d.png"), "-r", f"{a.fps:g}",
         "-fps_mode", "cfr", "-c:v", "libx264", "-crf", str(a.crf),
         "-preset", "medium", "-pix_fmt", "yuv420p", a.dst])
    print(f"写出 {a.dst}：{n_out} 帧 @ {a.fps:g} fps，补了 "
          f"{sum(1 for i in range(frames) if flags[i] and i in fills)} 帧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
