# 进度

进度以中心库为准，这份文件只放「怎么用」与「环境事实」，不记录工单状态。

## 环境

- Python：3.10+
- Git：用于工作树与提交闸门（可选，但没有它就没有「没工单不许提交」这道默认路径）
- 中心库：`.data/task-center.sqlite3`（唯一权威，不要另建备用库）

## 怎么读进度

```bash
python -m task_center --db "<工作区>/.data/task-center.sqlite3" list_projects
python -m task_center --db "<工作区>/.data/task-center.sqlite3" list_tickets --input <JSON>
python -m task_center --db "<工作区>/.data/task-center.sqlite3" events --input <JSON>
```

各会话不要各自维护一份 Markdown 进度——那会和中心库分叉，且看起来最详细的那份最先过期。
