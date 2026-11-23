# 历史合并记录豁免清单

生成时间：2026-11-23T07:47:22+00:00　生成命令：`python tools/backfill_merges.py --apply`

这份清单回答的是「`done` 但没有 `merges` 行」里哪些**不该**补记录。判断依据不是
猜测，而是两个现场事实：最新提交有没有 `head_sha`，以及它在不在 `master` 里。

- **A 档（可验证）**：有 `head_sha` 且是 `master` 的祖先 → 由本工具补一条
  `source=backfill` 的 `merges` 行，不进这份清单。
- **B 档（本清单）**：最新提交没有 `head_sha`（schema 9 以前的提交）→ 没有可核对的树，
  一行不写，在这儿写清理由。它们不是等待合并的单。
- **C 档（不豁免）**：有 `head_sha` 但不在 `master` 里 → 说明它没落地，工具拒绝并点名；
  这类单留在 `auto_merge.py --list` 的「真待合并」栏里等人判断。

本次豁免 0 张。核对用：`python tools/backfill_merges.py --dry-run`。

**清单为空是什么意思：**

此刻没有「state=done 但没有 merges 行、又没有 head_sha」的单。以前列在这里的历史单只有两种去向：证据补齐了（A 档补了 merges），或者已被 archived 终态覆盖 —— archived 不算 done，既不需要也不允许补合并记录。所以读到空清单就是答案，不必再去翻旧版本找它们。
