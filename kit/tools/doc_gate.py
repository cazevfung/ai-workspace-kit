"""计划文件闸门：把「规划时该做的事」变成可失败的检查。

为什么要有这个脚本：skill 只有名字和描述会进入提示，模型可能该用而没用；
但脚本退出码是非零，是确定性的。所以约束放在产物上，不放在提醒上。

用法：
    python tools/doc_gate.py tasks/plan-002-xxx.md
    python tools/doc_gate.py tasks/round.md
    python tools/doc_gate.py --all          # 扫 tasks/plan-*.md 与 tasks/round*.md
    python tools/doc_gate.py --all --root <项目根>   # 扫别的项目的 tasks/（服务对象项目）

两种文件、两套判据：`plan-*.md` 查六节与依据，`round*.md` 查本轮目标 /
结束条件 / 不做什么。分流的理由是这两件事的错法不同——计划写虚了是一回事，
「这一轮怎么算完」写成一句永远达不到的话是另一回事。

退出码：通过 0，有问题 1，用法错误 2。
"""
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = ("依据", "所用 skill", "交付物", "任务", "不做什么", "未决")
ROUND_SECTIONS = ("本轮目标", "结束条件", "不做什么")
# 结束条件必须是能真的跑一遍的东西；「文档精确即可」这种话在这里过不去。
ROUND_ACTIONS = ("python ", "git ", "grep ", "select ")
# `rounds.md` 是历史账本（一张表），不是「本轮目标」文件：按名字它会撞上 `round*.md`，
# 不排除掉的话 `--all` 会拿三节判据去查一张表，每次收尾都多三条假红。
ROUND_NUMBER = re.compile(r"^轮次[：:]\s*R-\d{3}\s*$", re.M)
ROUND_STATUS = re.compile(r"^状态[：:]\s*(\S+)\s*$", re.M)
# 四档与 docs/closing.md 的三档结论一一对应（需重开一轮 ↔ 需重开）。
ROUND_STATUSES = ("进行中", "等一句答复", "已收尾", "需重开")
ROUND_NAME = re.compile(r"^round(?!s\.md$)[^/\\]*\.md$")
SKILL_REF = re.compile(r"`([a-z0-9][a-z0-9-]{0,63})`")
PRD_REF = re.compile(r"PRD-\d{3}")
VERSION = re.compile(r"(?:版本|version)[:：]?\s*v?\d+\.\d+|v\d+\.\d+", re.I)
PLACEHOLDER = re.compile(r"\bTODO\b|\bTBD\b|\[待定\]|\{\{[^}]*\}\}")
CLARIFY = re.compile(r"\[待澄清[:：][^\]]+\]")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
# 判据 A 里可当命令的词；写成整词匹配，`python3` 不能只靠 `python` 前缀蹭过。
EVIDENCE_COMMANDS = (
    "python3",
    "python",
    "git",
    "grep",
    "find",
    "wc",
    "ls",
    "select",
    "curl",
    "node",
    "npm",
    "pytest",
    "bash",
)
COMMAND_WORD = re.compile(r"^\s*(?:sudo\s+)?(" + "|".join(EVIDENCE_COMMANDS) + r")(?=\s|$)")
# 跑完看到什么才算过；没有其中任何一个，命令可跑但结果不可判。
EVIDENCE_MARKERS = ("期望", "输出", "退出码", "→")
# 判据 B：判断类的验收条件必须写出反例，写不出说明这条本身不可判定。
JUDGEMENT_CLASS = "判断类"
COUNTEREXAMPLES = ("反例", "会红", "什么情况")
BASELINE_NAME = "evidence_baseline.txt"
# 已知存在但不是本仓库自建、也不在用户技能目录里的名字，登记在这里而不是放过未知。
EXTERNAL_SKILLS = frozenset()


def skill_dirs(anchor=None):
    """收集可接受的 skill 目录。

    默认看工具所在仓库与用户目录；传入 anchor 时，额外沿它的祖先逐层找
    `.agents/skills`。不这么做的话，另一项目的计划文件引用自己项目的 skill
    会被误判为「不存在」。
    """
    here = Path(__file__).resolve().parent.parent
    bases = [here / ".agents" / "skills", Path.home() / ".agents" / "skills"]
    if anchor is not None:
        for parent in [anchor if anchor.is_dir() else anchor.parent, *anchor.parents]:
            candidate = parent / ".agents" / "skills"
            if candidate not in bases:
                bases.append(candidate)
    return bases


def known_skills(anchor=None):
    names = set(EXTERNAL_SKILLS)
    for base in skill_dirs(anchor):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if (entry / "SKILL.md").is_file():
                names.add(entry.name)
    return names


def prd_dirs(anchor):
    """沿计划文件的祖先找 PRD 目录，用来核对依据里的 PRD 编号不是编的。

    此外始终带上工具所在仓库的 `PRD/`：计划搬进服务对象项目之后，它照样会引用
    工作区的 `PRD-001` / `PRD-003` 这类过程依据，那些文件在项目自己的 `PRD/` 下
    并不存在。这与 skill_dirs 的做法一致——两处都在核对「这个名字不是编的」，
    而名字的合法出处不止本仓库一处。
    """
    found = []
    here = Path(__file__).resolve().parent.parent / "PRD"
    if here.is_dir():
        found.append(here)
    if anchor is None:
        return found
    start = anchor if anchor.is_dir() else anchor.parent
    for parent in [start, *start.parents]:
        candidate = parent / "PRD"
        if candidate.is_dir() and candidate not in found:
            found.append(candidate)
    return found


def known_prds(anchor=None):
    """返回 {PRD 编号: 文件名}，只认 `PRD-00N-*.md` 这种命名。"""
    names = {}
    for base in prd_dirs(anchor):
        for entry in base.iterdir():
            match = re.match(r"(?i)prd-(\d{3})-.*\.md$", entry.name)
            if match and entry.is_file():
                names.setdefault(match.group(1), entry.name)
    return names


def sections(text):
    """按二级标题切段，返回 {标题: 正文}。"""
    found, current, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                found[current] = "\n".join(buf)
            current, buf = line[3:].strip(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        found[current] = "\n".join(buf)
    return found


def task_blocks(body):
    """取 `## 任务` 下的每个 `###` 块正文。"""
    blocks, current = [], None
    for line in body.splitlines():
        if line.startswith("### "):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return ["\n".join(b) for b in blocks]


def is_round_file(path):
    """`round*.md` 走本轮目标的检查，其余（含 `plan-*.md`）走计划检查。"""
    return bool(ROUND_NAME.match(Path(path).name))


def check_round(path):
    """检查「这一轮做到什么算完」的文件。

    六条判据：三节齐全、结束条件里有可跑的动作、不做什么非空、文件不空、顶部有
    `轮次：R-00N`、`状态：` 是四档之一。
    第一条是结构，第二、三条才是这份文件存在的理由——写不出可判定动作的
    结束条件，等于没有结束条件。
    """
    problems = []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return [f"{path}: 文件为空"]

    found = sections(text)
    for name in ROUND_SECTIONS:
        if name not in found:
            problems.append(f"{path}: 缺少必需的二级标题 `## {name}`")

    if not ROUND_NUMBER.search(text):
        problems.append(
            f"{path}: 顶部没有 `轮次：R-00N` 这一行；没有编号的一轮在 tasks/rounds.md 里对不上账"
        )

    status_match = ROUND_STATUS.search(text)
    if status_match is None:
        problems.append(
            f"{path}: 顶部没有 `状态：…` 这一行；只收四档之一：{'、'.join(ROUND_STATUSES)}"
        )
    elif status_match.group(1) not in ROUND_STATUSES:
        problems.append(
            f"{path}: 状态写成 `{status_match.group(1)}`，只收四档之一：{'、'.join(ROUND_STATUSES)}"
            "（收尾的判定与落点见 docs/closing.md）"
        )

    end = found.get("结束条件")
    if end is not None and not any(action in end for action in ROUND_ACTIONS):
        problems.append(
            f"{path}: `## 结束条件` 里没有一处可判定动作（{'、'.join(a.strip() for a in ROUND_ACTIONS)} 之一）；"
            "写成「文档精确即可」这种话，永远达不到"
        )

    excluded = found.get("不做什么")
    if excluded is not None and not excluded.strip():
        problems.append(f"{path}: `## 不做什么` 不能为空；没写排除项通常意味着边界没想过")

    return problems


def scan(root):
    """`--all` 的两组目标。两个都缺时分别报，不互相遮住。

    `root` 可以是另一个项目的根（`--root`）：文档搬进服务对象项目之后，`--all`
    也得能对那边跑。指向的目录连 `tasks/` 都没有时，点名那个路径——不然会出现
    「没有找到 tasks/plan-*.md」这种看不出找错地方的说法。
    """
    tasks = Path(root) / "tasks"
    problems = []
    if not tasks.is_dir():
        problems.append(f"{tasks}: 这个目录不存在；`--root` 要指向一个带 tasks/ 的项目根")
        return [], problems
    plans = sorted(tasks.glob("plan-*.md"))
    rounds = [p for p in sorted(tasks.glob("round*.md")) if is_round_file(p)]
    if not plans:
        problems.append(f"{tasks}: 没有找到 tasks/plan-*.md")
    if not rounds:
        problems.append(
            f"{tasks}: 没有找到 tasks/round*.md；先写 tasks/round.md（本轮目标 / 结束条件 / 不做什么）"
        )
    return plans + rounds, problems


def load_baseline(path=None):
    """读历史计划豁免清单，返回 {文件名: 该行原文}。

    格式：每行 `tasks/<计划文件名>`，`#` 之后是理由；`#` 开头的整行是注释。
    基线只豁免新增的「证据可判定」判据：六节与 skill 的检查照跑。
    这是「不回头改写历史」的出口——判据生效前的计划是当时规矩的产物。
    """
    target = Path(path) if path is not None else Path(__file__).resolve().parent / BASELINE_NAME
    names = {}
    if not target.is_file():
        return names
    for line in target.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        names[Path(entry).name] = entry
    return names


def evidence_verdict(block):
    """任务块的完成证据能不能被机器判一次。

    A：有一处反引号命令，命令词在名单内，且块内写了期望输出（含退出码）。
    B：写了 `判断类`，并且写出了反例。
    两者都不满足就是「写了一句散文」，不算证据。
    """
    for code in INLINE_CODE.findall(block):
        if not COMMAND_WORD.match(code):
            continue
        if any(marker in block for marker in EVIDENCE_MARKERS) or "exit" in block.lower():
            return True
    if JUDGEMENT_CLASS in block and any(word in block for word in COUNTEREXAMPLES):
        return True
    return False


def check(path, baseline=None):
    problems = []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return [f"{path}: 文件为空"]

    found = sections(text)
    for name in REQUIRED_SECTIONS:
        if name not in found:
            problems.append(f"{path}: 缺少必需的二级标题 `## {name}`")

    basis = found.get("依据", "")
    if basis:
        if not PRD_REF.search(basis):
            problems.append(f"{path}: `## 依据` 里没有 PRD 编号（形如 PRD-001）")
        if not VERSION.search(basis):
            problems.append(f"{path}: `## 依据` 里没有版本号；PRD 改一次就会全部失效")
        # 编号 + 版本号只能证明格式对；编一个不存在的编号和编一个 skill 名是同一种虚报。
        available_prds = known_prds(path)
        if available_prds:
            for number in sorted({m.group(1) for m in re.finditer(r"PRD-(\d{3})", basis)}):
                if number not in available_prds:
                    problems.append(
                        f"{path}: `## 依据` 引用了 PRD-{number}，但 PRD/ 下没有对应的 `PRD-{number}-*.md`"
                    )

    used = found.get("所用 skill", "")
    if used:
        refs = {m.group(1) for m in SKILL_REF.finditer(used)}
        if not refs:
            problems.append(f"{path}: `## 所用 skill` 里没有用反引号标出的 skill 名")
        available = known_skills(path)
        for ref in sorted(refs):
            if ref in ("done", "todo", "python", "bash", "true"):
                continue
            if ref not in available:
                problems.append(f"{path}: 引用了不存在的 skill `{ref}`（既不在项目 .agents/skills 也不在用户技能目录）")
        # 点名不等于真读了：每个 skill 所在行除了名字，必须还有一句它改变了什么。
        for line in used.splitlines():
            named = [m.group(1) for m in SKILL_REF.finditer(line) if m.group(1) in available]
            if not named:
                continue
            rest = SKILL_REF.sub("", line).strip(" -:\t\u3000")
            rest = re.sub(r"^|\b", "", rest)
            if len(re.sub(r"[\s\-—:：、，,。]", "", rest)) < 8:
                problems.append(
                    f"{path}: skill `{named[0]}` 后面没有写它改变了什么；只点名不算调用"
                )

    deliverable = found.get("交付物", "")
    if deliverable:
        for word in ("主题", "不覆盖"):
            if word not in deliverable:
                problems.append(f"{path}: `## 交付物` 必须写清「{word}」——只写验收条件会让成果合规地跑偏")

    body_tasks = found.get("任务", "")
    if body_tasks:
        name = Path(path).name
        if baseline is None:
            baseline = load_baseline()
        skipped = name in baseline
        if skipped:
            print(f"基线跳过 {name}")
        blocks = task_blocks(body_tasks)
        if not blocks:
            problems.append(f"{path}: `## 任务` 下没有任何 `###` 任务块")
        for block in blocks:
            title = block.splitlines()[0][4:].strip()
            judgeable = evidence_verdict(block)
            has_evidence_word = "验证" in block or "证据" in block
            if judgeable:
                continue
            if JUDGEMENT_CLASS in block or has_evidence_word:
                if not skipped:
                    problems.append(
                        f"{path}: 任务「{title}」的完成证据不可判定："
                        "要么写可跑命令 + 期望输出，要么标 判断类 并写出反例"
                    )
            else:
                problems.append(f"{path}: 任务「{title}」没有写完成证据（缺「验证」或「证据」）")

    for match in PLACEHOLDER.finditer(text):
        line = text[: match.start()].count("\n") + 1
        problems.append(f"{path}: 第 {line} 行有裸占位符 `{match.group(0)}`，改成带上下文的 [待澄清：…] 并登记到 `## 未决`")

    pending = CLARIFY.findall(text)
    if pending and "未决" not in found:
        problems.append(f"{path}: 有 {len(pending)} 处 [待澄清]，但缺少 `## 未决` 节登记它们")

    return problems


def all_root(argv):
    """从 `--all` 之后的参数里取 `--root <目录>`；缺省是工具所在仓库根。

    只认 `--all` 与 `--root <目录>` 两种参数，多写一个不认识的就当用法错误——
    与单文件模式一样退出码 2，不静默忽略，否则「我以为它在查 B 项目、其实又查了
    一遍自己」这种事查不出来。
    """
    root, index = Path(__file__).resolve().parent.parent, 0
    rest = [a for a in argv if a != "--all"]
    while index < len(rest):
        if rest[index] == "--root":
            if index + 1 >= len(rest):
                raise ValueError("--root 后面要跟一个目录")
            root = Path(rest[index + 1])
            index += 2
            continue
        raise ValueError(f"--all 只接受 --root <目录>，不认 {rest[index]}")
    return root


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        print(__doc__.strip())
        return 2
    baseline = load_baseline()
    if "--all" in argv:
        try:
            root = all_root(argv)
        except ValueError as exc:
            print(f"用法错误：{exc}", file=sys.stderr)
            return 2
        targets, all_problems = scan(root)
        if all_problems:
            print(f"闸门未通过，{len(all_problems)} 个问题：")
            for problem in all_problems:
                print(" - " + problem)
            return 1
    else:
        targets, all_problems = [Path(a) for a in argv], []

    for target in targets:
        if not target.is_file():
            all_problems.append(f"{target}: 文件不存在")
            continue
        if is_round_file(target):
            all_problems.extend(check_round(target))
        else:
            all_problems.extend(check(target, baseline))

    if all_problems:
        print(f"闸门未通过，{len(all_problems)} 个问题：")
        for problem in all_problems:
            print(" - " + problem)
        return 1
    print(f"闸门通过：{len(targets)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
