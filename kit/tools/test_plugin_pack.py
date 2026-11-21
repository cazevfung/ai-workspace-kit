"""`dist/*.cindy` 的包内自检：三件套齐全、图标是真图、清单与源码一致。

为什么需要它：Cindy 的 `.cindy` 是一个 zip，打包成功只说明校验过了；**包里到底有没有
`main.js`、图标是不是真图**，只有拆开看才知道。手工 `unzip -l` 一次能看见，改一版就
得再看一次，所以把它做成退出码。

四条判据（每条都有反例，缺一条都能被一个坏包推翻）：

1. `dist/` 下恰好一个 `.cindy`，且它是能完整读出来的 zip（`testzip()` 为 None）；
2. 包根有 `ghost.json`，清单里 `entry` 指向的 `main.js` 也在包里；
3. 清单声明的 `icon` 在包里，字节数大于 1024，且是真正的 PNG 图片而不是一段文本；
4. 包里的 `id` / `version` / `entry` 与源码目录的 `ghost.json` 一致——手改过源码却忘了重打包会红。

用法（在仓库根跑）：

    python tools/test_plugin_pack.py

退出码：全部通过 0，否则 1。
"""
import json
import struct
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
DIST = REPO / "dist"
SOURCE = REPO / "plugin" / "draft"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# 小于这个字节数的「图标」几乎一定是一块纯色或被压扁的占位图。
MIN_ICON_BYTES = 1024


def packages():
    return sorted(DIST.glob("*.cindy")) if DIST.is_dir() else []


def png_size(data):
    """PNG 的 IHDR 紧跟在 8 字节签名之后：宽高各 4 字节大端。"""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE:
        return None
    return struct.unpack(">II", data[16:24])


@unittest.skipUnless(
    packages() and (SOURCE / "ghost.json").is_file(),
    "`.cindy` 只在套件仓库里打；安装出来的工作区不带 dist/ 与 plugin/，这组用例在那里跳过",
)
class PluginPackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = packages()[0]
        cls.archive = zipfile.ZipFile(cls.path)
        cls.names = set(cls.archive.namelist())
        cls.manifest = json.loads(cls.archive.read("ghost.json").decode("utf-8"))
        cls.source = json.loads((SOURCE / "ghost.json").read_text(encoding="utf-8"))

    def test_exactly_one_package(self):
        self.assertEqual(1, len(packages()), f"dist/ 下应当只有一个包，实际：{[p.name for p in packages()]}")

    def test_package_is_a_readable_zip(self):
        self.assertIsNone(self.archive.testzip(), "包内有个文件读不出来")
        self.assertIn("ghost.json", self.names, "清单必须在包根，不能套一层目录")

    def test_entry_script_is_inside_the_package(self):
        entry = self.manifest.get("entry")
        self.assertTrue(entry, "清单没有声明 entry")
        self.assertIn(entry, self.names, f"清单声明的入口 {entry} 不在包里")
        self.assertGreater(self.archive.getinfo(entry).file_size, 0, "入口脚本是空文件")

    def test_icon_is_a_real_png_bigger_than_the_placeholder_threshold(self):
        icon = self.manifest.get("icon")
        self.assertTrue(icon, "清单没有声明 icon")
        self.assertIn(icon, self.names, f"清单声明的图标 {icon} 不在包里")
        data = self.archive.read(icon)
        self.assertGreater(len(data), MIN_ICON_BYTES,
                           f"图标只有 {len(data)} 字节，像是占位图而不是真图")
        size = png_size(data)
        self.assertIsNotNone(size, "图标不是 PNG（签名不对）")
        width, height = size
        self.assertEqual(width, height, f"插件图标应当是方图，实际 {width}x{height}")
        self.assertGreaterEqual(width, 256, f"图标长边只有 {width}px，缩到 32px 会糊")

    def test_manifest_matches_the_source_tree(self):
        for key in ("id", "version", "entry", "icon"):
            self.assertEqual(self.source.get(key), self.manifest.get(key),
                             f"包里的 {key} 与源码 ghost.json 不一致，多半是改了源码没重打包")

    def test_both_tools_are_declared(self):
        self.assertEqual({"bootstrap", "dispatch_ticket"},
                         {tool["name"] for tool in self.manifest["tools"]})

    @classmethod
    def tearDownClass(cls):
        """读完显式关掉：Windows 上留一个打开句柄会让下一次重打包覆盖失败。"""
        cls.archive.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
