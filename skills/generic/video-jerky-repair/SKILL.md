---
name: video-jerky-repair
description: 录屏或动画成品「看着卡」又说不清是哪一环出问题时用：先判病因（VFR、重复帧、运动不连贯），再按病因选方案，给出能直接粘的命令。用户说「这段视频卡顿/掉帧/一顿一顿」「转完码反而更卡」「补帧补出鬼影」「帧率不匀」时使用；不用于采集侧录法、超分/去噪/音频。
---

# 视频卡顿/掉帧的后期修复

## 一句话原则

**先判病因，再动手。** 卡顿至少有三种互斥的成因，用错方案会越修越糟：在重复帧上做补帧会插出一串静止画面，在 VFR 上补帧会按错误的间隔插，在没有丢失帧的素材上补帧只会把整段糊掉。

## 第 0 步：先看三个数

```bash
# 帧率、总帧数、时长
ffprobe -v error -select_streams v:0 \
  -show_entries stream=r_frame_rate,avg_frame_rate,nb_frames,duration \
  -of default=nw=1 video.mp4
```

判据只有一条：`r_frame_rate`（容器认为的帧率）与 `avg_frame_rate`（实际平均帧率）**不等就按 VFR 处理**，相等才有资格谈丢帧。

要重复帧比例这类更细的数字，用本 skill 的诊断脚本（只读，输出 JSON）：

```bash
python scripts/diagnose.py video.mp4 --pretty
```

输出里的 `dup_ratio`、`vfr`、`recommendation` 就是第 2 步的分诊依据；`details` 里有 `motion`（相邻帧平均差，越小画面越静）、`dups_short_ratio`（重复帧里「夹在运动中」的比例）。

## 第 1 步：分诊表

| 病因 | 现象 | 判据 | 处置 |
| --- | --- | --- | --- |
| **VFR**（时间戳不匀） | 帧数不少、帧率却忽高忽低，慢镜头抖动 | `r_frame_rate != avg_frame_rate`，或相邻帧时间戳间隔不一致 | 先归一成 CFR，别补帧 |
| **重复帧**（掉帧/一拍二/编码器补帧） | 画面「顿」一下，逐帧看有几帧完全一样 | `dup_ratio >= 0.15`，且重复帧里 `dups_short_ratio` 不低（说明夹在运动里） | 按重复帧位置原地插帧补位 |
| **运动不连贯**（帧率低、真丢帧） | 每个帧都不同，但一步跨太大，像翻页 | `dup_ratio` 低、`motion` 高、帧率 <= 24 | 整体补帧到更高帧率 |
| 都不是 | 只是编码烂/码率低 | 上面三条都不成立 | 别修帧，去动编码参数 |

**重复帧要分清两种，这一步判错，后面全白做：**

- **夹在运动里的重复帧**（游程 1–4 帧，前后帧差很大）：这是真丢帧/一拍二，看起来卡，补位有效。
- **整段静止导致的重复帧**（游程几十上百帧）：这是定格镜头、标题卡或转场停顿，不是病。补位补不出运动，只会得到一串几乎相同的帧——**这类段落不要当主案例**，也别拿「重复帧比例降不下来」当失败。

## 第 2 步：按病因给命令

### A. VFR → 归一成恒定帧率

```bash
# 先探时间戳是否真不匀（同一秒内间隔不一致就是 VFR）
ffprobe -v error -select_streams v:0 -show_entries frame=pts_time \
  -of csv=p=0 video.mp4 | head -30

# 归一：按帧重新采样到目标帧率，时间戳转为恒定
ffmpeg -i in.mp4 -vf "fps=30:round=near" -fps_mode cfr \
  -c:v libx264 -crf 16 -preset medium -pix_fmt yuv420p out_cfr.mp4
```

`fps` 里 `round=near` 比默认更少丢帧；`-fps_mode cfr` 才是真正把时间戳写成恒定，只写 `-r` 在某些来源上仍会留下 VFR 的尾巴。

### B. 重复帧 → 原地插帧补位

```bash
# 只在这段里补，时间轴长度不变；--rife 指 rife-ncnn-vulkan 可执行文件
python scripts/dedup_fill.py seg.mp4 seg_fixed.mp4 \
  --rife /path/to/rife-ncnn-vulkan(.exe) --model rife-v4.6 --fps 25
```

它是怎么做的：按重复帧位置分组，每组只取前后两张「真帧」，交给 RIFE 在这两张之间生成「空位长度 + 2」张均匀中间帧，填回原位，其余帧原样保留。

**为什么不用 ffmpeg 的 `minterpolate` 一把梭**：`minterpolate` 按输入声明的帧率决定插几帧。用 `select` 摘掉重复帧之后，流声明的帧率还是原帧率，它判定「已经够了」而一帧都不补（实测：摘掉重复帧后跑 `minterpolate=fps=25`，重复帧数量几乎没变）。逐空位跑 RIFE 才能做到只补该补的位置。

### C. 运动不连贯 → 整体补帧

```bash
# 拆帧（PNG 无损，避免二次压缩糊掉细节）
ffmpeg -v error -i in.mp4 -vsync 0 frames/%06d.png

# 补到 2 倍帧率（-n 是「目标总帧数」，不是倍数：给 N*2）
rife-ncnn-vulkan -i frames -o interp -n <N*2> -m rife-v4.6

# 装回 2 倍帧率的视频
ffmpeg -framerate 50 -i interp/%08d.png -r 50 -fps_mode cfr \
  -c:v libx264 -crf 16 -preset medium -pix_fmt yuv420p out.mp4
```

纯 ffmpeg 也能做，慢、质量差一档、但不用装东西：

```bash
ffmpeg -i in.mp4 -vf "minterpolate=fps=50:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1" \
  -c:v libx264 -crf 16 -preset medium -pix_fmt yuv420p out.mp4
```

### 备选：帧混合 + 假运动模糊

```bash
# 复制成双倍帧率，再对相邻三帧做时间平均（权重自动归一，别自己写 scale）
ffmpeg -i in.mp4 -vf "fps=50,tmix=frames=3" -r 50 -fps_mode cfr \
  -c:v libx264 -crf 16 -preset medium -pix_fmt yuv420p out_blend.mp4
```

它让画面「顺」了，代价是每个运动物体都带一道残影。**只在没有细节可看的内容上可以接受**；画面里有文字、UI、人物轮廓时不要用。

## 判据：四个方案怎么选

| 情况 | 选 |
| --- | --- |
| VFR | CFR 归一（A）。不归一，别的都别做 |
| 重复帧多且夹在运动里 | 原地补位（B）。帧率不变、只动该动的位置，压缩友好 |
| 重复帧多但整段静止 | 不修。定格镜头本来就没有运动信息 |
| 每帧都不同、只是跨太大 | 整体补帧（C）。补到 2 倍帧率 |
| 有文字/UI/精细线条 | 不要用帧混合（伪影最明显），用 B 或 C |
| 要压体积、要兼容老设备 | 优先 B：输出帧率不变，体积只涨几个百分点 |

实测经验（同类竖版 1200×1500、25 fps 的手记素材）：B 能把「真正看得见的掉帧」从十几帧降到个位数，且同一帧对比里没有鬼影；C 也能降，但输出翻倍、体积涨 10% 以上；帧混合会把运动物体糊成双影。

## 常见踩坑

1. **跨镜头切换点补帧**：切换点前后是两张无关的画面，插值会插出「两张叠在一起」的鬼影。先按切换点切开再补，或补完把切换点前那一张生成的中间帧丢掉。
2. **`minterpolate` 不补**：见上文 B 节。它的判断依据是流声明的帧率，不是实际的时间戳空洞。
3. **`tmix` 忘了归一**：`tmix` 的 `scale` 是**倍率**（默认 0 表示自动按权重和归一）。写 `scale=1` 等于关闭归一，输出直接过曝成一片色块。
4. **重复帧比例当唯一指标**：50 fps 成品每帧运动量只有原来一半，用同一个「相邻帧差 < 阈值算重复」的口径去量，会把正常插出来的帧也算成重复帧。要么折算到原帧率再量（`-vf fps=25` 后重量），要么看「前后有运动的重复帧」这一项。
5. **重复帧阈值一刀切**：静止镜头的压缩噪声也可能有 0.2 的平均差，阈值调太低会把静止画面判成运动。0.3–0.4 之间比较稳；判不准就把 `--dup-thresh` 调一点再跑一次诊断，看 `motion` 是否合理。
6. **在 NAS/网络盘上直接转码**：慢一个数量级还容易中断。先整条复制到本地再处理。
7. **补帧后丢音频**：过滤链默认不处理音频，`-c:a copy` 或显式重编码，否则成品是哑的。
8. **PNG 中间帧占空间**：一段 10 秒的 1200×1500 竖版能到几百 MB 到 1 GB，跑完记得清临时目录。

## 自检（交付前跑一遍）

```bash
# 1. 时长、帧数有没有变（补位方案应当时长不变）
ffprobe -v error -show_entries format=duration -show_entries stream=nb_frames -of default=nw=1 out.mp4

# 2. 重复帧比例
python scripts/diagnose.py out.mp4 --pretty

# 3. 同一时间码抠一张图，和原片并排看有没有鬼影/糊化
ffmpeg -v error -ss 3.2 -i out.mp4 -frames:v 1 -vf "crop=400:400:400:600,scale=800:-1:flags=neighbor" out.png
ffmpeg -v error -ss 3.2 -i in.mp4  -frames:v 1 -vf "crop=400:400:400:600,scale=800:-1:flags=neighbor" src.png
```

三条都过再交付：时长不变（或按方案预期变化）、`dup_ratio` 下降、放大图里文字/边缘没有双影。

## 附：诊断脚本输出的含义

```json
{"dup_ratio": 0.18, "vfr": false, "recommendation": "dedup-fill",
 "why": "...", "details": {"motion": 3.9, "dups_short_ratio": 0.54, ...}}
```

- `dup_ratio`：与前一帧几乎相同的帧占全部帧的比例
- `vfr`：时间戳是否不均匀
- `recommendation`：`cfr-normalize` / `dedup-fill` / `interpolate` / `none`，对应上面的 A/B/C/不修
- `dups_short_ratio`：重复帧里游程 <= 4 帧的比例，越高越像「真丢帧」，越低越像「定格」
