# 坏的引用样本（行号）

文件不存在：`tests/fixtures/ref-gate/missing.md:3` 这个文件根本没有。

行数越界：`tests/fixtures/ref-gate/target.md:999` 超出目标文件的实际行数。

空行：`tests/fixtures/ref-gate/target.md:8` 那一行没有任何内容。
