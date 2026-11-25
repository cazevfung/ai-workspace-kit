# 本轮：把最新工作区与面板同步进来，发 0.6.0

轮次：R-025
状态：已收尾

## 本轮目标

1. **同步**：把 `D:/AI工作区` 当前 master 的框架（`task_center` / `tools` / `tests` / `examples` / `docs` / `.githooks`）、22 个通用技能、6 个工作区技能与 16 份 PRD 同步进 `kit/` 与 `skills/`；把 `panel/` 与 `D:/cindy-dashboard` 逐文件核一遍，结论写成证据。
2. **装完能核验**：装出来的工作区跑 `python tools/run_tests.py` 必须全绿——这一步的前提是 AWK-120 先把 `.gitattributes` 装进工作区（已合入 `f54da61`）。
3. **发版**：把 0.6.0 打进 `dist/`、打 tag、推 master、建 GitHub Release 附 `.cindy` 与源码包，并在 `CHANGELOG.md` 写清这版同步了什么。
4. **欠账入池**：本轮发现的缺口（面板 PRD 落后、`kit/tasks/` 整棵树随包装走）落成 `planned` 单，不写散文。

## 结束条件

- `python maintainer/sync_from_origin.py --origin D:/AI工作区` 退出码 0，九项文件数都打印出来。
- `python maintainer/redact_for_public.py --check` 退出码 0。
- `python tools/run_tests.py` 末行 `结果：全部通过`，合计不少于 1133 项。
- `python verify/verify_install.py` 每一项都通过，没有非零退出。
- `python tools/pack_plugin.py` 退出码 0，`ls dist` 只有 0.6.0 的两个资产。
- `gh release view v0.6.0 --json assets --jq '.assets[].name'` 输出两行，与 `ls dist` 逐字一致。

## 不做什么

- 不改 `D:/AI工作区` 与 `D:/cindy-dashboard` 任何一个字节。
- 不改 `task_center` 的 schema，不改闸门的判定逻辑。
- 不重写套件里的面板 PRD，不重构 `kit/tasks/` 的打包方式——这两件各自开单。
- 不动 `v0.5.0` / `v0.5.1` / `v0.5.2` 三个已发的 tag 与 Release。
- 不做陌生人真机演练与插件导入（要人点一下）。
