// 七个测试文件的固定入口：让 `node --test panel/app/test` 这种「给一个目录」的跑法成立。
//
// 为什么需要它：node v24 把 `--test` 的目录形参当模块去解析（实测 `node --test app/test`
// 直接 `Cannot find module ...\\app\\test`，退出 1）。目录解析只认 package.json 的 main 与
// index.js，所以 `test/package.json` 的 main 指到这里，目录形参才有落点。
//
// 为什么放在 app/ 而不是 app/test/：`node --test` 不带形参时的默认发现规则会把 `test/`
// 目录下的每个文件都当成测试文件。入口若放在 `test/` 里，`cd app && npm test` 会把七个
// 文件先跑一遍、再被入口导入跑第二遍（77 项变 154 项）。放在 `app/` 下不匹配任何默认
// 测试文件名，两种跑法各自只跑一遍。
//
// 顺序与 `node --test` 的字母序一致，便于两边输出对照。

import './test/feed.test.mjs';
import './test/golden.test.mjs';
import './test/hit.test.mjs';
import './test/host.test.mjs';
import './test/resolve.test.mjs';
import './test/sessions.test.mjs';
import './test/window-math.test.mjs';
