# claims.json 格式与三类口径

这份文件写给两拨人：要把结案材料里的数字登记下来的人，和验收时想一条命令核掉算术的人。
它是 `tools/claims_gate.py` 的说明书，不是规则——规则怎么用、什么时候必须用，是另一件事。

## 一句话

`claims.json` 是一张断言表：每条断言说「这份文档里的这个数，应该等于多少」，
附一句「这个数是从哪儿来的」。闸门逐条复算，对不上就退出 1 并点出 `文件:行号`。

## 闸门判什么、不判什么

判：数量、合计、比例、摘要、体积、锚点、清单条数。这七样都能由脚本从源数据重算一遍。

不判：口径合理不合理、这个数该不该出现在文档里、证据有没有可能失败、
某句话说得对不对。**语义判断一件都不做。** 它判的只是「写下来的数和源数据对不对得上」。

## 期望值必须来自源数据

**`expect` 必须带一条 `source`，写明这个期望值是怎么来的：从句命令的输出来的，
还是从源数据的第几行抄来的。缺 `source` 直接按格式错误退出 2。**

这一条是硬要求，理由不是礼貌：手填的期望值等于把错数从文档搬到 `claims.json`，
搬过去之后闸门就成了唯一一处「知道正确答案」的地方——而它并不知道。
数字错了但两处写的是同一个错数，闸门会安静地绿灯。

所以正确的做法只有两种：

1. **用生成命令产出**：`python tools/claims_gate.py --claims claims.json --emit`
   把每条的实测值打出来，把需要的那个抄回去；`sha256sum` / `wc -c` / `grep -c` 同理。
2. **从源数据抄**：从源数据的某一行的原值抄，并在 `source` 里写清是哪一行。

不要「看文档里写着 10，就把 10 登记成期望值」——那不是核对，是把文档抄了一遍。

## 放在哪里

**`claims.json` 放在项目根，一份对应整个项目的断言。** `file` 一律相对
`claims.json` 所在目录解析，不是相对当前工作目录。

这样规定的原因是：文档会随目录调整搬家，而断言表跟项目走；`file` 若相对
shell 的当前目录解析，同一份 `claims.json` 在仓库根跑和在子目录跑会核到两份
不同的文件，而输出里没有任何一处能看出这件事。

原文核的文档也是 `.md` 时同理——放在同一目录下，`file` 直接写文件名即可。

## 形状

```json
{
  "version": 1,
  "note": "这份表核的是一份结案材料",
  "claims": [
    { "id": "C1", "kind": "count", "file": "结案.md", "line": 20,
      "pattern": "^- 组", "regex": true, "expect": 3,
      "source": "`grep -c '^- 组' 结案.md` 的输出" }
  ]
}
```

顶层三个字段：`version` 只收 `1`；`note` 可选，写这份表核的是什么；
`claims` 是非空数组。

每条断言必给的四个字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 这条断言的编号，报错时会点名。同一份文件里不要求唯一，但重复了会分不清是哪条 |
| `kind` | 七种之一，见下 |
| `file` | 被核的文件，相对 `claims.json` 所在目录 |
| `source` | 期望值从哪儿来。见上一节 |

`line` 可选：**报错时定位到的那一行**，通常写「期望值在文档里被陈述的那一行」。
不写就按第 1 行报——整份文件的断言（`sha256` / `size`）本来就没有具体行号，
用同一个形状输出比多一种格式好。

## 四类断言、七种 kind

### 数量类：count / sum / ratio

| kind | 额外字段 | 复算什么 |
| --- | --- | --- |
| `count` | `pattern`、`regex`（默认 true）、`expect` | 命中 `pattern` 的行数 |
| `sum` | `pattern`、`expect` | 每行第一个捕获组当整数，全部相加 |
| `ratio` | `numerator`、`denominator`、`digits`（默认 2）、`expect` | 两个模式的行数之比，按 `digits` 位小数四舍五入 |

- `regex` 写 `false` 时 `pattern` 按子串找，不编译正则。
- `sum` 的 `pattern` **必须正好带一个捕获组**；组数是 0 或 2 以上无从确定加什么，按格式错误处理。
- `ratio` 的分母命中 0 次时这条判为不成立（不是崩溃）：比例无从算起这件事本身要说出来。

```json
{ "id": "C2", "kind": "sum", "file": "结案.md", "line": 20,
  "pattern": "^- 组.：(\\d+) 份$", "expect": 10,
  "source": "把三行的份数 3+5+2 相加，与第 20 行写的合计 10 比" }
```

### 摘要类：sha256 / size

| kind | 额外字段 | 复算什么 |
| --- | --- | --- |
| `sha256` | `expect`（64 位小写十六进制） | 文件字节的实际摘要 |
| `size` | `expect`（字节数） | 文件的实际字节数 |

`sha256` 的期望值只收 64 位小写十六进制，大小写不一致会让同一个摘要在两份文件里
看起来不同，所以先让格式对上再比。

**一个真会踩到的坑：核对文本文件要先看换行符。** 本仓库从 2026-11-23（AW-137）起把
全仓文本钉成 LF（`.gitattributes` 里 `* text=auto eol=lf`），所以新检出里 `.md` / `.py`
的工作区字节与库里的 blob 一致。仍然会变的是两类：标了 `-text` 的文件（`skills/**`、
`.agents/skills/skill-routing/index/**`），以及**规则落地之前检出、还没就地重检出**的
旧工作区——那里的文本还是 CRLF。所以摘要与体积这两类断言，要么指向二进制交付物
（图片、视频、导出包——这本来就是它们的主战场），要么在登记前先确认这份检出已按
`README.md` 的「行尾口径」重检出过。`tests/fixtures/claims/export.bin` 就是前一种：
它含 NUL 字节，git 按二进制处理，换行符转换不会动它。

```json
{ "id": "C6", "kind": "sha256", "file": "export.bin",
  "expect": "5d00a3f0f33169f8064fefa1af8c670cc0b611d93bb1fcac5287ef162fa6532f",
  "source": "对 export.bin 跑 sha256sum 的输出" }
```

### 位置类：anchor

| kind | 额外字段 | 复算什么 |
| --- | --- | --- |
| `anchor` | `line`、`contains` | 第 `line` 行是否存在、是否含 `contains` |

`anchor` **不登记 `expect`**：它本身就是「这一行是不是这样」的核对，实测即结论。

这是四类里唯一真正核对「行号」的一类。它抓两种错：行号超出文件长度（文档被删了几行，
锚点还指着旧位置），以及这一行的文字被改掉了。

```json
{ "id": "C4", "kind": "anchor", "file": "结案.md", "line": 20,
  "contains": "比例 0.75", "source": "锚点本身就是源数据" }
```

### 清单类：list

| kind | 额外字段 | 复算什么 |
| --- | --- | --- |
| `list` | `section`、`marker`（默认 `^[-*] `）、`expect` | 某个标题节下命中 `marker` 的行数 |

**识别口径（写死在这里，验收按这一条判）：清单条目 = 节内行首记号命中的行。**

- 节从标题行开始算：找到第一行 `line.lstrip("#").strip() == section` 的标题，
  到下一个以 `#` 开头的行（或文件末尾）为止；标题行本身不算条目。
- 条目按行首记号认，默认 `- ` 与 `* ` 两种，可用 `marker` 换成别的正则
  （编号清单就写 `^\\d+\\. `）。
- 找不到这一节时这条判为不成立，并说明「没有找到标题为 X 的节」。

选行首记号而不是「markdown 表格数据行」的理由：表格里对齐行（`| --- |`）、
单元格里的 `|` 转义、以及有空单元格的行，都需要额外约定才算得清「哪一行算数据」；
行首记号在文档里没有歧义，一句话就能说清。**代价是表格里的清单数不了**——
那类清单请用 `count` 配 `^\\|` 之类的模式，并在 `source` 里写清它数的是表格行。

```json
{ "id": "C5", "kind": "list", "file": "结案.md", "line": 20,
  "section": "二、清单", "expect": 4,
  "source": "数 `二、清单` 节下以 `- ` 开头的行" }
```

## 一份完整的 claims.json

`tests/fixtures/claims/good.json` 是可直接跑的完整示例：一份文档上跑七条断言，
数量类三条、摘要类两条、位置类一条、清单类一条。想看它长什么样：

```bash
python tools/claims_gate.py --claims tests/fixtures/claims/good.json
```

要核的文档与它放在同一目录（`tests/fixtures/claims/`），`file` 就写文件名。

## 怎么跑

```bash
python tools/claims_gate.py --claims claims.json            # 核对
python tools/claims_gate.py --claims claims.json --emit     # 只打印实测值，供回填期望值
python tools/claims_gate.py --help                          # 七种 kind 与四类的名字
```

`--emit` 不读 `expect`、也不比：它按 `id` / `kind` / 实测值三列打印出来，
填期望值时从这里抄，或从这里看出该填什么。这是「期望值来自生成命令」这条的落地方式。

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 全部对上 |
| 1 | 有断言对不上（输出每条都带 `文件:行号`） |
| 2 | 用法错误，或 `claims.json` 本身的形状有问题（JSON 坏了、kind 不认识、缺 `source`、摘要不是 64 位十六进制、`sum` 的捕获组数不对） |

对不上与文件坏了分开两个码，是因为修法不同：1 要去改文档或改登记值（谁错了要人看），
2 只要把 `claims.json` 写对。

被核的文件不存在算 1 而不是 2：一份指向不存在文件的断言，本来就是不成立的，
而「源数据没了」正是这类闸门该喊出来的事。

## 正反样本

`tests/fixtures/claims/` 下四份反例，各自只错一处，用来证明闸门抓得住：

| 文件 | 错在哪 | 报出什么 |
| --- | --- | --- |
| `bad-count.json` | 文档说 3 个分组，登记成 4 | `report.md:20: count 不一致：登记 4，实测 3` |
| `bad-hash.json` | 拿空输入的摘要当 `report.md` 的摘要 | `report.md:1: sha256 不一致：…` |
| `bad-anchor.json` | 锚点指向第 999 行，文件只有 20 行 | `report.md:999: …只有 20 行` |
| `bad-list.json` | 清单实际 4 条，登记成 5 | `report.md:20: list 不一致：登记 5，实测 4` |

四份都能用一条命令确认：

```bash
for f in bad-count bad-hash bad-anchor bad-list; do
  python tools/claims_gate.py --claims tests/fixtures/claims/$f.json; echo "exit=$?"
done
```
