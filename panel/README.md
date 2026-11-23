# panel/ · Windows 常驻浮窗（420×700）

一个浮在桌面上的进度小窗：无边框、始终置顶、不进任务栏、可以收成一行。它读的是工作区里那份
`progress-export.json`，**只读、不发消息、不改工单状态、没有任何写操作**。

这份说明是给「装完套件想把它跑起来」的人看的。判据、版式与八条硬指标不在这里重写，在
`../kit/PRD/PRD-016-Windows常驻浮窗.md`（对应 `kit/PRD/PRD-010-项目进度面板.md` 的数据口径）。

## 装在哪

`install.py` 会把整个 `panel/` 装到 `<工作区>/panel`，并把文件里的工作区占位符换成那台机器上的
真实路径。安装脚本为此把 `.mjs` / `.cjs` / `.html` 也加进了模板替换后缀——否则装完的
`main.mjs` 与 `sample.html` 里会留着没被替换的占位符，浮窗读不到导出物。

在本仓库（套件源码树）里直接跑时占位符不会被替换：那时浮窗拿到的默认导出物路径是一段占位符，
所以要么先装一遍，要么用环境变量把导出物指过去（见下面「路径」）。

## 怎么跑浮窗

需要 **Node 18+**，并且 `panel/app` 下要先装 Electron（它不在版本库里）：

```bash
cd panel/app
npm install            # 只装 electron 一个开发依赖
npm start              # 启动浮窗，读的是真导出物
```

跑不起来时的两条兜底（都只跟本机 PATH 与安装脚本有关）：

- `npm` 换成 Node.js 自带的那个：`"C:\Program Files\nodejs\npm.cmd" install`、`..." start`；
- 装完 `panel/app/node_modules/electron/dist/electron.exe` 不存在（安装脚本被跳过或下载中断）：
  补一次 `node panel/app/node_modules/electron/install.js`。

`node_modules/` 与 Electron 的二进制缓存都不入库。

## 怎么跑用例

两种跑法等价，都是 77 项、七个测试文件：

```bash
node --test panel/app/test        # 在仓库根：给目录，落到 app/run-tests.mjs
cd panel/app && node --test       # 在 app/ 里：默认发现 test/*.test.mjs
```

**为什么要多一个 `app/run-tests.mjs` 与 `test/package.json`。** node v24 把 `--test` 的目录形参
当模块解析（实测 `node --test app/test` 直接 `Cannot find module ...\app\test`、退出 1）。目录解析只认
`package.json` 的 `main` 与 `index.js`，所以 `test/package.json` 的 `main` 指向 `../run-tests.mjs`，
目录才有一个落点。入口文件放在 `app/` 而不是 `app/test/`：默认发现规则会把 `test/` 下每个文件都当
测试文件，入口若住在 `test/` 里，`cd app && node --test` 会先跑七份、再被入口导入跑第二遍（77 项变
154 项）。这一层只改「怎么找到测试」，一个断言都没动。

其中一条是**黄金对照**：同一份夹具 `tests/fixtures/export-golden.json` 喂给 `tools/exportfeed.py`
与 `app/feed.mjs`，两边输出必须逐字段相等。改了夹具或任一边的映射之后重跑：

```bash
python tools/exportfeed.py tests/fixtures/export-golden.json --now 1795176000 \
  --out tests/fixtures/export-golden.expected.json
```

`panel/tests/fixtures/` 只放了这两个夹具文件——那套 Python 用例属于面板自己的交付线，没有搬进套件。

## 结构

| 文件 | 干什么 |
| --- | --- |
| `app/main.mjs` | 浮窗主进程：单实例、无边框置顶、托盘、位置存档、加载 `docs/样张/sample.html` |
| `app/feed.mjs` | 导出物归一：把 `progress-export.json` 变成页面吃的那份模型（与 Python 侧逐字段对齐） |
| `app/resolve.mjs` | 一次 resolve 的算路：交回它自己的会话，或在项目根开新建会话；第三种只是说清为什么不行 |
| `app/host.mjs` | 宿主端口：导航 + 剪贴板、`--open-folder`，以及一次 resolve 一行 JSONL 的发出记录 |
| `app/sessions.mjs` | 会话存活判据：找会话库、一条只读 `SELECT`、「不知道」与「没了」分开说 |
| `app/hit.mjs` / `app/window-math.mjs` | 留白不吃点击的几何判定；折叠与贴边钳制的窗口数学 |
| `app/probe.mjs` | 只在调试时用的探针通道 |
| `app/preload.cjs` | 沙箱里的桥：只暴露 `window.cindyWidget` 的几个方法，不暴露 `ipcRenderer` |
| `app/test/` | 七个 `node:test` 用例文件（77 项） |
| `tools/datalayer.py` | 只读取数：`mode=ro` + `PRAGMA query_only=ON`、按 mtime 找 Cindy 库、时间换算 |
| `tools/overview.py` | 工单进度视图：阶段判定、五步进度、排序与汇总 |
| `tools/serve.py` + `tools/live.html` | 本地只读服务与实时页面（`/window` 是同一个样张），只绑 `127.0.0.1` |
| `tools/exportfeed.py` | 把中心库导出成 `progress-export.json`（浮窗读的就是它） |
| `tools/snapshot.py` | 生成单个自包含 HTML 的离线快照 |
| `tools/probe-hit-through.py` | 留白穿透的真机探针（要真鼠标，Windows） |
| `tools/verify-real-feed.mjs` / `tools/verify-session-targets.mjs` | 拿真导出物 + 真会话库核「箭头与 resolve 是同一套判定」（只读，退出码就是判据） |
| `docs/样张/` | `sample.html` 本体、`对照.md`、四张 420×700 PNG |
| `tests/fixtures/` | 黄金对照用的两份夹具（脱敏，路径是 `D:/redacted/...`） |

## 路径

- **中心库**：`<工作区>/.data/task-center.sqlite3`（`tools/datalayer.py`）。
- **导出物**：`<工作区>/.data/progress-export.json`，可用环境变量 `CINDY_WIDGET_EXPORT` 指到别处
  （测试、换机、指向另一份快照时用）。主进程按 `mtime+size` 判变，2 秒看一次。
- **会话库**：导出物自报的 `source.sessionsDb`；读不到就退回 `%APPDATA%/Cindy` 下 mtime 最新的
  `cindy-*.db`。只一条 `SELECT`、`mode=ro`、读完就关，不复制库、不做 checkpoint。
- **窗口状态与发出记录**：`%APPDATA%/cindy-dashboard-widget/window-state.json` 与
  `resolve-log.jsonl`（一次 resolve 一行，成功与失败都记）。

## resolve 的两条边界（别把它当成「发送」）

宿主对窗口外程序只公开 `cindy://` 导航与命令行开关，**没有「替我说这段话」的形态，也没有「在某个
目录建一条会话」的接口**。所以 resolve 只做两件真事加一句实话：

| 做 | 不做 |
| --- | --- |
| 会话还在：请求宿主导航到 `cindy://session/<id>`，并把要交回的那段话放进剪贴板 | 不往会话里发消息（没有这个接口） |
| 没有会话 / 会话已被删：请求宿主 `--open-folder <项目根>` 打开新建会话页，那段话进剪贴板 | 不新建会话（`--open-folder` 只把草稿页打开，正文仍得人发） |
| 文案只说可核的事：请求了哪条链接 / 哪个开关、复制了什么 | 不写「已发送」「已交回」「已建好」 |

## 没有搬进来的东西

- `node_modules/`（`npm install` 生成）、Electron 的二进制缓存；
- `dashboard.html`（`tools/snapshot.py` 的生成物，本身不入库）；
- 面板仓库自己的 `tests/`（Python 用例）与 `tests/live_harness.js`；
- `docs/evidence/`（截图证据）与 `docs/CD-*.md`（面板仓库的历史工单记录）。

`tools/run_tests.py` 原样搬进来了，但它跑的是面板仓库自己的 `tests/`——那套没搬，所以在 `panel/`
下跑它会打印「找不到测试目录：tests」并退出 2。这不是坏掉了，是它在这里没有可跑的用例。
