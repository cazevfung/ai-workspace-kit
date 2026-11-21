# 通用 skill 的索引基线：快照、词表与校验

这些 `SKILL.md` 在 `~/.agents/skills/` 下，live 侧是指向本仓库 `skills/` 的 junction；
真正入库的正文在 `skills/`。本目录记的是**另一件事**：每个接入技能的 description 该是哪一段、
必含哪几个中文触发词、以及「有人把它改了」怎么当场发现。触发词与长度上限的来由不需要另一份需求文档——`expected-terms.json` 里的 `approved` 就是定稿原文，本目录自己的校验脚本会先红。

| 路径 | 内容 |
| --- | --- |
| `snapshot/<skill>/SKILL.md` | live 的**逐字节副本**（连 `description:` 行一起）。它只管一件事：正文有没有被改掉。不是「改前原样」，也不承担回滚历史 description 的职责 |
| `expected-terms.json` | 每个 skill 的 `approved`（description 的定稿原文，唯一权威）、必含中文触发词、长度上限；名单取自 `installed-skills.md` 的「本机通用 / 接入」 |
| `.gitattributes` | 本目录不做 EOL 转换。有一个被改文件的原文用 CRLF，归一化成 LF 后快照就不再逐字节相等了 |
| `../scripts/check_index.py` | 一条命令查完词表里的每一个：description 偏离 approved 没、词齐没、正文变没变；**个数从词表读**，不写死 |
| `../scripts/test_check_index.py` | 上面那个脚本自己的反例用例：每一类红都真的造一次，还原后必须真的变绿 |

## 一条命令

```bash
python .agents/skills/skill-routing/scripts/check_index.py          # 全查，全对 EXIT=0
python .agents/skills/skill-routing/scripts/check_index.py --json   # 给别的脚本吃
python .agents/skills/skill-routing/scripts/check_index.py --only desc   # 只查 description 是否偏离 approved
python .agents/skills/skill-routing/scripts/check_index.py --only body   # 只查正文未变
python .agents/skills/skill-routing/scripts/test_check_index.py     # 脚本自己的反例
```

`--only` 的参数改过名：旧文档里的 **`--only changed` 现在是 `--only desc`**，旧名不再接受（argparse 直接报 `invalid choice: 'changed'`，退出码 2）。改的不只是名字——它现在核对的是 **live 的 `description` 与词表里的 `approved` 是否一致**，不是「这次有没有改过 description」：上游把它覆盖掉、手动改一个字、写成另一个措辞，只要与 `approved` 不同都报 `description-drifted`；与 `approved` 一字不差就通过，不管历史上改过几次。修它的方式只有一种：把 live 那一行改回 `approved` 原文（要改定稿就两边一起改）。

输出长这样，`skill` 在前，缺哪个词、差在第几行都写名字：

```
[missing-term] systematic-debugging: 缺期望词 「找不到原因」
[body-changed] writing-plans: 第 118 行 内容与快照不同
```

## 三类红，三种补法

| 报的什么 | 意思 | 补法 |
| --- | --- | --- |
| `description-drifted` | live 的 `description:` 与词表里的 `approved` 不一致（上游覆盖或手改） | 把 live 那一行改回 `approved` 原文；确实要改定稿，就两边一起改，别只改一边 |
| `missing-term` / `too-long` | description 缺触发词，或超过 `max_chars` | 同上：以 `approved` 为准修 live |
| `body-changed` | description 之外的行与快照不同 | 确认改动是有意的，再把 live 覆盖到快照；不确定就按快照还原 |
| `scope-mismatch` | 词表与 `installed-skills.md` 的「本机通用 / 接入」名单对不上 | 补登记或删登记。名单只有那一份，不手抄 |
| `approved-missing` | 词表条目少了 `approved`，无从判断 description 是否偏离 | 补上定稿原文 |

`--only desc` 与 `--only body` 是分开的两把尺子：description 归 `approved` 管，正文归快照管，
改一行正文不会伪装成 description 漂移，反之亦然——`body` 这一侧比对时会把 `description:` 那一行
剔掉，所以只改 description 时 `--only body` 仍然通过。

## 退回去

- **只还原 description**：把 `approved` 贴回 live frontmatter 的那一行，行尾（LF / CRLF）照旧。
- **整份还原成基线**：`cp .agents/skills/skill-routing/index/snapshot/<skill>/SKILL.md ~/.agents/skills/<skill>/SKILL.md`。
  快照就是 live 的逐字节副本，所以这是「回到上一次登记时的样子」，不是「回到加索引词之前」。

## 加一个新技能

1. live 文件（或仓库 `skills/` 下那份）就位；
2. `expected-terms.json` 加一条：`name` / `node` / `path` / `required_terms` / `approved`（就是 live 那句 description 原文）；
3. `snapshot/<name>/SKILL.md` 照抄 live，不虚构任何「改前描述」；
4. `installed-skills.md` 加一行，处置表写「本机通用 / 接入」；
5. 跑 `check_index.py`，0 处问题才算登记完。

## 边界

本目录只证明「每个 skill 的索引里有了我们卡住时真会说的话」，证明不了会话因此更容易加载它。
同节点多 skill 的「该读哪个」仍由 `../nodes.md` 与 `../roles.md` 兜底。
