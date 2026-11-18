#!/usr/bin/env python3
"""去 AI 味体检脚本：定位难读的位置，不打分。

用法：
    python lint_readability.py 稿件.txt
    python lint_readability.py 甲.txt 乙.txt
    python lint_readability.py --text "直接贴一段文字"

这个脚本只做一件事：把可能有问题的位置指出来，让人自己去看。
它没有分词，没有句法分析，所有指标都是近似值。

所以它不能当验收标准。上一个 skill 的教训就是拿规则命中率当目标，
数字降下去了，文章反而更像 AI。判断难读只有一个人能做：读它的人。

指标与阈值都是起步值，不是测量结果。
依据见同级目录 RESEARCH.md。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 词表


def alt(*parts: str) -> str:
    """把若干交替项拼成非捕获组。去掉每项尾部多余的竖线，否则会匹配空串。"""
    joined = "".join(parts).strip()
    joined = joined.rstrip("|")
    return "(?:" + joined + ")"


# 有生动作：只有活人才会做的动作。死物配上它们就是错配候选。
ANIMATE_VERBS = (
    "期待|希望|渴望|盼望|害怕|恐惧|担心|记得|记住|忘记|相信|怀疑|决定|选择|拒绝|"
    "学会|教会|教给|想要|愿意|打算|试图|努力|思考|认为|察觉|感受|承受|忍受|"
    "拥抱|亲吻|吞噬|吞掉|吃掉|呼吸|睡觉|醒来|流泪|微笑|颤抖|低语|诉说|等待|"
    "原谅|承诺|欺骗|背叛|爱上|厌倦|嫉妒|后悔|犹豫|坚持|追逐|逃跑|唱歌|跳舞|"
    "回答|发问|听见|看到|走到|拿走|笑起来|哭出来|笑|讲述|喊|叫|盯|凝视|"
    "哭泣|做梦|叹息|抱怨|撒谎|偷|生长|腐烂|追逐|追赶|跟得|爬|奔跑|逃走"
)
ANIMATE = re.compile(alt(ANIMATE_VERBS))

# 强错配动作：几乎只可能是有生主语在用。用来做宽口径检测。
STRONG_ANIMATE = re.compile(alt(
    "期待|希望|害怕|恐惧|担心|记得|记住|忘记|相信|怀疑|决定|拒绝|学会|教会|教给|"
    "想要|愿意|打算|拥抱|亲吻|吞噬|吞掉|吃掉|呼吸|睡觉|醒来|流泪|微笑|颤抖|低语|"
    "诉说|等待|原谅|承诺|欺骗|背叛|爱上|厌倦|嫉妒|后悔|犹豫|追逐|逃跑|跳舞|保管|"
    "讲|说|唱|哭|笑|喊|盯|叹息|抱怨|撒谎|生长|腐烂|追赶"
))

# 抽象 / 无生命主语的后缀
ABSTRACT_SUFFIX = (
    "性|感|度|力|化|论|观|主义|现象|过程|系统|机制|逻辑|趋势|结构|状态|关系|"
    "模式|方法|能力|价值|意义|问题|情绪|焦虑|孤独|效率|成本|时间|数据|算法|"
    "模型|方案|话语|叙事|文本|节奏|氛围|时代|行业|市场|技术|内容|平台思维"
)
ABSTRACT_NOUN = re.compile(r"([\u4e00-\u9fa5]{1,6}(?:%s))" % ABSTRACT_SUFFIX)

# 具体的无生命物
INANIMATE_NOUN = (
    "对话|聊天|会议|文档|代码|产品|项目|流程|函数|接口|版本|页面|按钮|表格|"
    "邮件|群聊|标题|句子|段落|文章|视频|音乐|电影|城市|街道|风|雨|光|影子|"
    "房间|房子|机器|工具|软件|图表|报告|数字|成绩|分数|模板|框架|规则|条款|"
    "预算|排期|进度|清单|目录|剧本|台词|账号|菜单|界面|礼物|皮肤|活动|关卡|"
    "混乱|混沌|焦虑|孤独|情绪|欲望|惯性|噪音|信息|流量|热度|竞争|节奏|记忆|注意力|"
    "云|风|雨|阳光|月亮|星星|路灯|灯光|窗|门|时间|市场|舆论|季节|小镇|游戏|奖励|进度|故事|"
    "数据|表格|报告|图表|模型|代码|接口|赛季|文案|版面"
)
INANIMATE = re.compile(alt(INANIMATE_NOUN))

# 有生命主语标记，命中就不报
ALIVE = re.compile(
    r"我|你|他|她|人们|用户|读者|作者|团队|公司|客户|玩家|同学|学生|老师|"
    r"孩子|员工|老板|朋友|编辑|记者|人|者|员|家|师|生|友|客|方"
)

# 谓语动词表，只用来判断"这段文字走了多远还没遇到动作"
PREDICATES = (
    "是|有|在|说|写|读|看|听|想|记|忘|学|教|做|拿|放|打|买|卖|查|改|拆|建|算|比|"
    "涨|降|增|减|省|换|选|定|猜|懂|觉得|认为|发现|出现|发生|变成|开始|结束|来自|"
    "属于|意味|代表|构成|导致|包括|需要|要求|允许|禁止|满足|使用|提供|支持|完成|"
    "提升|降低|解决|处理|包含|覆盖|依赖|影响|决定|生成|训练|学习|阅读|理解|记住|"
    "翻译|统计|对比|验证|测量|记录|上传|下载|上线|发布|调整|优化|改善|检查|分析|"
    "改造|升级|同意|反对|提出|解释|描述|判断|区分|举例|假设|证明|反驳|"
    "加|提|减少|增加|变得|显得|持续|保留|采用|围绕|针对|对应|形成|带来|"
    "造成|起到|做成|写成|读成|翻成|翻回|算成|当成"
)
PREDICATE = re.compile(alt(PREDICATES))

# 判断"句首要走多远才遇到动作"时，必须排掉"是/有/在"这类轻动词，
# 否则任何句子里都有一个"在"，指标永远为 0。
FAR_PREDICATES = (
    "觉得|认为|发现|出现|发生|变成|开始|结束|来自|属于|意味|代表|构成|导致|"
    "包括|需要|要求|允许|禁止|满足|使用|提供|支持|完成|提升|降低|解决|处理|"
    "包含|覆盖|依赖|影响|决定|生成|训练|学习|阅读|理解|记住|翻译|统计|对比|"
    "验证|测量|记录|上传|下载|上线|发布|调整|优化|改善|检查|分析|改造|升级|"
    "同意|反对|提出|解释|描述|判断|区分|举例|假设|证明|反驳|减少|增加|变得|"
    "显得|持续|保留|采用|围绕|针对|对应|形成|带来|造成|起到|做成|写成|读成|"
    "教|吃|吞|拆|建|查|改|算|猜|懂|换|选|定|搬|推|拉|写|造"
)
FAR_PREDICATE = re.compile(alt(FAR_PREDICATES))

# 承接词（人写作的冗余，压缩后会省）
CONNECTIVES = (
    "而且|并且|但是|可是|不过|然而|因为|所以|因此|于是|然后|接着|后来|比如|例如|"
    "其实|只是|当然|反正|毕竟|倒是|偏偏|难怪|原来|至于|另外|再说|至少|反过来|"
    "也就是说|换句话说|总之|结果|最后"
)
CONN = re.compile(alt(CONNECTIVES))

NOMINALIZATION = re.compile(
    r"(?:完成了对|实现了|进行了|做出了|达成了)[^，。；！？\n]{0,20}"
    r"(?:的(?:提升|优化|改善|调整|检查|分析|处理|改造|升级))"
)

ENUM3 = re.compile(
    r"[^，。！？；：、\n]{1,14}、[^，。！？；：、\n]{1,14}、[^，。！？；：、\n]{1,14}"
)

SENT_SPLIT = re.compile(r"[^。！？；!?;\n]+[。！？；!?;]?")
CLAUSE_SPLIT = re.compile(r"[，,；;]")

LONG_SENTENCE = 60      # 字
LONG_ATTRIBUTE = 15     # 中心词前的修饰字数
LONG_VERBLESS = 18      # 连续多少字没有谓语
FAR_LIMIT = 20          # 句首到第一个动作允许多远
DE_CHAIN_GAP = 12       # 同一分句内两个"的"的间距


def chinese_len(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def split_sentences(text: str) -> list[str]:
    out = []
    for raw in SENT_SPLIT.finditer(text):
        s = raw.group(0).strip()
        if s.startswith("#") or s.startswith("|"):
            continue          # 小标题和表格不当句子算
        if chinese_len(s) >= 4:
            out.append(s)
    return out


def verbless_runs(sentence: str) -> list[str]:
    """把句子切成不含谓语的最长片段。片段越长，读者要暂存的东西越多。

    不跨逗号：一个逗号本来就是一次换气，跨过去算长度会虚高。
    """
    found = []
    for clause in CLAUSE_SPLIT.split(sentence):
        runs, cur, i = [], "", 0
        while i < len(clause):
            m = PREDICATE.match(clause, i)
            if m:
                runs.append(cur)
                cur = ""
                i = m.end()
            else:
                cur += clause[i]
                i += 1
        runs.append(cur)
        found += [r for r in runs if chinese_len(r) >= LONG_VERBLESS]
    return found


def check(sents: list[str]) -> dict:
    hits = {
        "long": [], "verbless": [], "far_pred": [], "attribute": [], "de_chain": [],
        "animate": [], "nominal": [], "enum3": [],
    }
    for i, s in enumerate(sents, 1):
        n = chinese_len(s)
        if n > LONG_SENTENCE:
            hits["long"].append((i, n, s))

        for run in verbless_runs(s):
            hits["verbless"].append((i, chinese_len(run), run))

        first = FAR_PREDICATE.search(s)
        if first and first.start() > FAR_LIMIT:
            hits["far_pred"].append((i, first.start(), s))

        idx = [k for k, ch in enumerate(s) if ch == "的"]
        for a, b in zip(idx, idx[1:]):
            same_clause = not re.search(r"[，,；;。！？]", s[a:b])
            if same_clause and b - a <= DE_CHAIN_GAP:
                hits["de_chain"].append((i, b - a, s))
                break

        head = CLAUSE_SPLIT.split(s)[0]
        d = head.find("的")
        if d > LONG_ATTRIBUTE:
            hits["attribute"].append((i, d, head))

        # 死物配活物：按分句找主语，主语是无生命物、谓语是有生动作，就是候选。
        # 只看谓语前 4 个字会漏掉长主语（"云在天上冲你笑"），所以要回到分句开头。
        for clause in CLAUSE_SPLIT.split(s):
            head = clause[:4]
            if ALIVE.search(clause[:3]):
                continue
            nm = ABSTRACT_NOUN.search(clause[:6])
            inan = INANIMATE.search(head)
            if not (nm or inan):
                continue
            subject = nm.group(1) if nm else inan.group(0)
            for vm in list(STRONG_ANIMATE.finditer(clause)) + list(ANIMATE.finditer(clause)):
                if vm.start() < 1 or subject in vm.group(0) or vm.group(0) in subject:
                    continue
                # 三种误报要先排掉，否则报告会被噪声淹掉：
                # 1) 情态/评价框架：值得期待、可以追、很难爱上
                # 2) 工具做它本来的事：系统提醒你、程序奖励高分
                # 3) 从 A 走到 B 这类方位隐喻
                before = clause[:vm.start()]
                if re.search(r"值得|可以|能够|容易|很难|会|想|要|像|被|从", before):
                    continue
                if re.match(r"工具|系统|程序|机制|模型|表格|算法|平台|机器", subject) and                         re.match(r"提醒|显示|提示|计算|记录|推荐|追问|告诉|返回|保存|处理|奖励|拦截", vm.group(0)):
                    continue
                hits["animate"].append((i, f"{subject}…{vm.group(0)}", s))
                break

        if NOMINALIZATION.search(s):
            hits["nominal"].append((i, 0, s))
        if ENUM3.search(s):
            hits["enum3"].append((i, 0, s))
    # 去重（同一句同一位置只留一条）
    for k, rows in hits.items():
        seen, uniq = set(), []
        for row in rows:
            if (row[0], row[1]) in seen:
                continue
            seen.add((row[0], row[1]))
            uniq.append(row)
        if k == "animate":
            per_sentence: dict[int, int] = {}
            kept = []
            for row in uniq:
                n = per_sentence.get(row[0], 0)
                if n >= 2:
                    continue
                per_sentence[row[0]] = n + 1
                kept.append(row)
            uniq = kept
        hits[k] = uniq
    return hits


def density(text: str, pattern: re.Pattern) -> float:
    n = chinese_len(text) or 1
    return len(pattern.findall(text)) * 1000 / n


def report(name: str, text: str) -> None:
    sents = split_sentences(text)
    hits = check(sents)
    total = chinese_len(text)
    lens = [chinese_len(s) for s in sents] or [0]
    avg = sum(lens) / len(lens)
    std = (sum((x - avg) ** 2 for x in lens) / len(lens)) ** 0.5

    print(f"\n## {name}")
    print()
    print(f"字数 {total}，句子 {len(sents)}，平均句长 {avg:.0f} 字，标准差 {std:.1f}")
    conn = density(text, CONN)
    flag = ""
    if conn > 18:
        flag = "  ← 超过 18，可能在贴标签凑承接，看规则 12"
    elif conn < 3:
        flag = "  ← 接近 0，句子之间可能没接上"
    print(f"承接词 {conn:.1f}/千字{flag}")
    print()

    def dump(key: str, title: str, note: str, show_value: bool = True) -> None:
        rows = hits[key]
        print(f"### {title}：{len(rows)} 处")
        if not rows:
            print("无。")
            print()
            return
        print(f"_{note}_")
        print()
        for i, val, s in rows[:8]:
            extra = f"（{val}）" if show_value else ""
            tail = "…" if chinese_len(s) > 70 else ""
            print(f"- 第 {i} 句{extra}：{s[:70]}{tail}")
        if len(rows) > 8:
            print(f"- 另有 {len(rows) - 8} 处未列出")
        print()

    dump("animate", "死物配活物（候选）",
         "无生命的东西做出只有活人才做的动作。最该改的一类。")
    dump("nominal", "名词化", "“完成了对……的……”。还原成动词。", show_value=False)
    dump("enum3", "顿号串三项以上", "能概括就概括；必须全列时，改掉其中一项的句法。", show_value=False)
    dump("long", f"长句（>{LONG_SENTENCE} 字）", "念不完就拆。")

    print("### 参考项（误报多，别按数字改）")
    print()
    print("_下面四项靠词表和位置估算，没有句法分析。转折句、带冒号的句子、口语短句都会触发。")
    print("只看你自己读起来卡的地方，不要为了清零去改本来就对的句子。_")
    print()
    ref = [
        ("verbless", f"连续 {LONG_VERBLESS} 字没有谓语"),
        ("far_pred", f"句首到第一个动作超过 {FAR_LIMIT} 字"),
        ("attribute", f"前置定语超过 {LONG_ATTRIBUTE} 字"),
        ("de_chain", "同句“的”链"),
    ]
    for key, title in ref:
        rows = hits[key]
        if not rows:
            print(f"- {title}：无")
            continue
        print(f"- {title}：{len(rows)} 处")
        for i, val, s in rows[:3]:
            print(f"  - 第 {i} 句：{s[:56]}{'…' if chinese_len(s) > 56 else ''}")
    print()

    print("---")
    print()
    print("以上都是位置提示，不是错误清单。")
    print("验收不能靠这个脚本：读一遍，数「需要回头重看」的次数。")


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("files", nargs="*", help="要检查的文件")
    ap.add_argument("--text", help="直接传入文本")
    args = ap.parse_args()

    if not args.files and not args.text:
        ap.print_help()
        return 2

    if args.text:
        report("（命令行输入）", args.text)

    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"找不到文件：{f}")
            continue
        report(p.name, p.read_text(encoding="utf-8", errors="ignore"))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
