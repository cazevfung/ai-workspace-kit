#!/usr/bin/env python3
"""CD-016 真机探针：卡片外那圈 12px 透明留白到底吃不吃点击。

为什么要有这个脚本：C1/C2/C3 都不是「看代码对不对」，是「鼠标点下去落到哪个窗口」。
所以这里问的是系统本身：

  · `WindowFromPoint`  —— Windows 自己用来决定「这一下点归谁」的那条路。留白里的点应该落到
    浮窗下面那个窗口（这里是本脚本自己开的一块垫子），卡片本体里的点应该仍然回到浮窗。
  · 真实的鼠标移动与点击（`SendInput`）—— 只落在**本脚本自己的窗口**上：垫子或浮窗。
    指针位置开跑前记下来，跑完放回去；开跑前先等系统安静几秒（`GetLastInputInfo`），
    免得跟正在用这台机器的人抢鼠标。
  · 浮窗自己报出来的状态回读 —— `setIgnoreMouseEvents` 到底是穿透还是拦截、卡片当前在哪。
    这条只在设了 `CINDY_WIDGET_PROBE` 时才有（见 app/probe.mjs）；老板本没有，所以
    `--expect-bug` 那条基线复现只做 WindowFromPoint。

跑法（仓库根）：

    # 修好之后：A 展开 / B 折叠成一行 / C 藏到托盘再打开 + 展开
    python tools/probe-hit-through.py

    # 基线：直接跑主检出里没修的那一版，证明「留白确实吃点击」——也就是这个探针本身是敏感的
    python tools/probe-hit-through.py --expect-bug --app {{WORKSPACE}}/panel/app

    # 完全不动真鼠标（只问 WindowFromPoint）
    python tools/probe-hit-through.py --no-real-input

退出码：0 = 全部符合预期；1 = 有不符；2 = 环境不满足（跑不起来）。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes

# 先声明 DPI 感知，再读任何坐标：这样 GetWindowRect / GetCursorPos / WindowFromPoint /
# SendInput 全在同一套物理像素里，卡片矩形那个「12px 留白」也能按真实比例换算。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ---------------------------------------------------------------- Win32

user32 = ctypes.WinDLL('user32', use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
GA_ROOT = 2


class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('mi', MOUSEINPUT)]


INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         ctypes.POINTER(RECT), wintypes.UINT]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_rect(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def client_rect(hwnd):
    r = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def client_origin(hwnd):
    p = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return (p.x, p.y)


def ex_style(hwnd):
    return int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))


def root_window(hwnd):
    """最外层那个窗口。WindowFromPoint 拿到的往往是浮窗里的子窗口
    （Chrome_RenderWidgetHostHWND），要比就得比到根上。"""
    return user32.GetAncestor(hwnd, GA_ROOT) or hwnd


def work_area():
    """主屏工作区（物理像素）"""
    r = RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
    return (r.left, r.top, r.right, r.bottom)


def cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def idle_seconds():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return max(0.0, (tick - info.dwTime) / 1000.0)


def find_window_by_pid(pid):
    """这个进程自己的顶层可见窗口（拿不到探针时用）"""
    found = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if pid_of(hwnd) != pid:
            return True
        if class_name(hwnd).startswith('Chrome_WidgetWin'):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found[0] if found else None


def send_mouse(flags, x=None, y=None):
    """发一个真实的鼠标事件。带坐标时按虚拟屏幕的 0–65535 归一化。"""
    item = (INPUT * 1)()
    item[0].type = INPUT_MOUSE
    item[0].mi.dwFlags = flags
    if x is not None and y is not None:
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        item[0].mi.dx = int(round((x - vx) * 65535 / max(1, vw - 1)))
        item[0].mi.dy = int(round((y - vy) * 65535 / max(1, vh - 1)))
    user32.SendInput(1, item, ctypes.sizeof(INPUT))


# ---------------------------------------------------------------- 报告

class Report:
    def __init__(self, path):
        self.lines = []
        self.failures = 0
        self.checks = 0
        self.path = path

    def line(self, text=''):
        self.lines.append(text)
        print(text, flush=True)

    def check(self, ok, message):
        self.checks += 1
        if not ok:
            self.failures += 1
        self.line(('  [PASS] ' if ok else '  [FAIL] ') + message)
        return ok

    def note(self, message):
        self.line('  · ' + message)

    def flush(self):
        if not self.path:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(self.lines) + '\n')


# ---------------------------------------------------------------- 垫子窗口

class Pad:
    """浮窗下面那块「谁都没点到的桌面」：用来接住留白里点下去的那一下。

    它必须是**普通窗口**（不能置顶）：浮窗是 alwaysOnTop，垫子置顶就会把卡片盖住。
    """

    def __init__(self, rect):
        import tkinter as tk

        self.clicks = []
        self.lock = threading.Lock()
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry('{w}x{h}+{x}+{y}'.format(
            w=rect[2] - rect[0], h=rect[3] - rect[1], x=rect[0], y=rect[1]))
        self.root.configure(bg='#2060a0')
        tk.Label(self.root, text='CD-016 垫子窗口（这一段代表浮窗下面那个窗口）',
                 bg='#2060a0', fg='white').pack(expand=True)
        self.root.bind('<Button-1>', self._on_click)
        self.root.update()
        handle = self.root.winfo_id()
        self.hwnd = user32.GetAncestor(handle, GA_ROOT) or handle

    def _on_click(self, event):
        with self.lock:
            self.clicks.append((event.x_root, event.y_root, time.time()))

    def click_count(self):
        with self.lock:
            return len(self.clicks)

    def pump(self, seconds=0.2):
        """没有 mainloop 也要把队列里的鼠标事件收掉，否则点到的计数一直是 0。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                self.root.update()
            except Exception:
                return
            time.sleep(0.02)

    def destroy(self):
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- 跟浮窗里那个探针说话

class Probe:
    def __init__(self, state_path):
        self.state_path = state_path
        self.cmd_path = state_path + '.cmd.json' if state_path else ''
        self.available = bool(state_path)
        self._cmd_id = 0

    def state(self, timeout=0.0):
        if not self.available:
            return None
        deadline = time.time() + timeout
        while True:
            try:
                with open(self.state_path, encoding='utf-8') as handle:
                    return json.load(handle)
            except Exception:
                if time.time() >= deadline:
                    return None
                time.sleep(0.1)

    def latest(self):
        """浮窗最近一次记下的那份现状（hitSnapshot 的内容都在 last 里）"""
        return (self.state() or {}).get('last') or {}

    def command(self, name, *args, timeout=8.0):
        if not self.available:
            return None
        self._cmd_id += 1
        cmd_id = self._cmd_id
        with open(self.cmd_path, 'w', encoding='utf-8') as handle:
            json.dump({'id': cmd_id, 'name': name, 'args': list(args)}, handle)
        deadline = time.time() + timeout
        while time.time() < deadline:
            done = (self.state() or {}).get('lastCommand') or {}
            if done.get('id') == cmd_id:
                return done
            time.sleep(0.1)
        return None


def wait_for(predicate, timeout, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# ---------------------------------------------------------------- 一次验证的上下文

class Session:
    def __init__(self, report, probe, widget_hwnd, pad, args):
        self.report = report
        self.probe = probe
        self.widget = widget_hwnd
        self.pad = pad
        self.args = args

    # -- 几何：全部换算到 WindowFromPoint / SendInput 用的那一套像素 ---

    def _stable_latest(self):
        """最近一份带卡片矩形的快照，等它稳下来再返回。

        启动后那几帧里内容区会被校正一次（fitShell），#win 的核中位置跟着变几个像素；
        拿一份刚刚好就快照到旧位置的矩形，会让「卡片外 5px」量成「卡片外 1px」——
        滞回刚好把它吃掉，看起来就成了「留白不穿透」。
        """
        latest = self.probe.latest()
        if not self.probe.available:
            return latest
        for _ in range(4):
            if not latest.get('cardRectLocal'):
                self.probe.command('snapshot')
                latest = self.probe.latest()
                continue
            before = latest['cardRectLocal']
            self.probe.command('snapshot')
            time.sleep(0.2)
            latest = self.probe.latest()
            if latest.get('cardRectLocal') == before:
                return latest
        return latest

    def card_rect(self, state=None):
        """卡片在屏幕上的矩形（物理像素）。

        卡片在窗口内容区里的矩形由页面报（CSS 像素 = DIP），内容区原点和缩放比从这里量：
        ClientToScreen + GetClientRect / 内容区尺寸。这样不管进程 DPI 感知与否，
        算出来的矩形跟 WindowFromPoint 用的是同一套坐标。
        """
        latest = ((state or {}).get('last') or {}) if state else self._stable_latest()
        local = latest.get('cardRectLocal')
        content = latest.get('contentBounds')
        cx, cy = client_origin(self.widget)
        left, top, right, bottom = client_rect(self.widget)
        client_w, client_h = right - left, bottom - top
        if local and content and content.get('width'):
            scale = client_w / float(content['width'])
        else:
            # 拿不到探针：按「卡片 420 + 四边各 12 留白」的已知几何反推
            # （内容区请求值 444×724，实际可能多 1–2px，所以卡内矩形由宽度比例算）
            scale = client_w / 444.0
            local = {'left': 12, 'top': 12, 'right': 12 + 420,
                     'bottom': 12 + max(0, client_h / scale - 24)}
        return {
            'left': cx + local['left'] * scale,
            'top': cy + local['top'] * scale,
            'right': cx + local['right'] * scale,
            'bottom': cy + local['bottom'] * scale,
            'scale': scale,
            'client': (cx, cy, cx + client_w, cy + client_h),
        }

    # -- 动作 ---

    def far_point(self, card):
        """一个离卡片足够远的点（停在那里让浮窗回到穿透底态）。

        不写死坐标：卡片可能被挤在屏幕某个角上，写死的点可能已经在卡片里或者出了屏。
        """
        area = work_area()
        candidates = [
            (area[0] + 30, area[1] + 30),
            (area[2] - 30, area[1] + 30),
            (area[0] + 30, area[3] - 30),
            (area[2] - 30, area[3] - 30),
        ]
        for x, y in candidates:
            outside = (x < card['left'] - 30 or x > card['right'] + 30
                       or y < card['top'] - 30 or y > card['bottom'] + 30)
            if outside:
                return x, y
        return candidates[0]

    def who(self, x, y):
        hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
        if not hwnd:
            return ('none', None)
        top = root_window(hwnd)
        if top == self.widget:
            return ('widget', top)
        if self.pad and top == self.pad.hwnd:
            return ('pad', top)
        return ('%s(pid %s)' % (class_name(top), pid_of(top)), top)

    def move_to(self, x, y, settle=0.35):
        if not self.args.real_input:
            return
        x, y = int(round(x)), int(round(y))
        for _ in range(3):
            send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, x, y)
            time.sleep(0.03)
            if cursor_pos() == (x, y):
                break
            user32.SetCursorPos(x, y)     # 归一化坐标没落准：退回直接设位置
            time.sleep(0.03)
            if cursor_pos() == (x, y):
                break
        time.sleep(settle)

    def park(self, x, y, settle=0.35):
        """把指针挪过去，并确认它真的停在那里。返回 False = 没停位。

        不假设一次就能到位：这台机器上可能有别的东西在动鼠标（远控/辅助工具）。
        没停位就如实记一笔然后跳过这一点，不把「别人把鼠标抢走了」算成浮窗的错。
        """
        x, y = int(round(x)), int(round(y))
        for _ in range(3):
            self.move_to(x, y, settle=0.15)
            now = cursor_pos()
            if abs(now[0] - x) <= 1 and abs(now[1] - y) <= 1:
                time.sleep(settle)
                again = cursor_pos()
                if abs(again[0] - x) <= 1 and abs(again[1] - y) <= 1:
                    return True
        return False

    def click_at(self, x, y):
        """真点一下。只在 WindowFromPoint 确认是「我们自己的窗口」时才按下去。"""
        if not self.args.real_input:
            return
        if not self.park(x, y, settle=0.2):
            self.report.note('跳过这次点击：(%d,%d) 指针没停到位' % (x, y))
            return
        target = self.who(x, y)[0]
        if target not in ('widget', 'pad'):
            self.report.note('跳过这次点击：(%d,%d) 上是别人的窗口（%s）' % (x, y, target))
            return
        send_mouse(MOUSEEVENTF_LEFTDOWN)
        send_mouse(MOUSEEVENTF_LEFTUP)
        time.sleep(0.3)
        if self.pad:
            self.pad.pump()

    def ignore_state(self):
        # 探针里 applyIgnore / snapshot 记的键叫 ignore（hitSnapshot 只管几何，不管状态）
        return self.probe.latest().get('ignore')

    def transparent_bit(self):
        return bool(ex_style(self.widget) & WS_EX_TRANSPARENT)


# ---------------------------------------------------------------- 断言段

def settle_ignore(session, expected, timeout=3.0):
    if not session.probe.available:
        return None
    return wait_for(lambda: session.ignore_state() is expected, timeout)


def sweep_margin(session, report, points):
    """把几个留白点走一遍：先把指针停到位，再同时看「系统把点派给谁」与「浮窗自报状态」。"""
    for label, (x, y) in points.items():
        if session.args.real_input and not session.park(x, y):
            report.note('%s：指针没停到位（有东西在抢鼠标），这一点没测' % label)
            continue
        settle_ignore(session, True)
        if session.probe.available:
            actual = session.ignore_state()
            report.check(actual is True, '%s：浮窗自报 ignoring=%s（期望 True）' % (label, actual))
        who = session.who(x, y)[0]
        report.check(who == 'pad', '%s：点落到下面那个窗口（WindowFromPoint → %s）' % (label, who))


def step_state(session, expected, label, report):
    """回读浮窗自己报的穿透状态 + 窗口上的 WS_EX_TRANSPARENT 位。"""
    if not session.probe.available:
        report.note('%s：没有探针，跳过状态回读' % label)
        return
    settle_ignore(session, expected)
    actual = session.ignore_state()
    report.check(actual is expected, '%s：浮窗自报 ignoring=%s（期望 %s）' % (label, actual, expected))
    report.check(session.transparent_bit() is bool(expected),
                 '%s：窗口 WS_EX_TRANSPARENT 位 = %s' % (label, session.transparent_bit()))


def phase_expanded(session, report):
    card = session.card_rect()
    report.line('')
    report.line('A. 展开态（卡片 %.0f×%.0f，客户端矩形 %s，缩放比 %.4f）' % (
        card['right'] - card['left'], card['bottom'] - card['top'], card['client'], card['scale']))

    mid_x = (card['left'] + card['right']) / 2
    mid_y = (card['top'] + card['bottom']) / 2
    # 「卡片外 5px」里的 5px 按页面里的 CSS 像素算（那圈留白本来就是 CSS 的 12px），
    # 换算成物理像素后离卡片还有 4 个 DIP，不会被 2px 的边缘滞回吃掉
    m5 = 5 * card['scale']
    points = {
        '左外 5px': (card['left'] - m5, mid_y),
        '右外 5px': (card['right'] + m5, mid_y),
        '上外 5px': (mid_x, card['top'] - m5),
        '下外 5px': (mid_x, card['bottom'] + m5),
        '留白最外侧': (card['client'][0] + card['scale'], card['client'][1] + card['scale']),
    }

    report.line('')
    report.line('A0. 指针不在浮窗上（底态）')
    far = session.far_point(card)
    session.move_to(*far, settle=0.3)
    if session.probe.available:
        session.probe.command('rearm')
        time.sleep(0.3)
    step_state(session, True, '底态', report)
    for label, (x, y) in points.items():
        who = session.who(x, y)[0]
        report.check(who == 'pad', '底态（指针在远处没动）下%s的点落到下面那个窗口（WindowFromPoint → %s）'
                     % (label, who))

    report.line('')
    report.line('A1. 指针进到卡片本体')
    if session.park(mid_x, mid_y):
        step_state(session, False, '指针在卡片里', report)
        who = session.who(mid_x, mid_y)[0]
        report.check(who == 'widget', '卡片正中仍归浮窗（WindowFromPoint → %s）' % who)
    else:
        report.note('指针没停到卡片正中，这一点没测')

    report.line('')
    report.line('A2. 指针回到卡片外 5px（C1）')
    sweep_margin(session, report, points)
    step_state(session, True, '指针在留白里', report)

    if session.args.real_input and session.pad:
        report.line('')
        report.line('A3. 真点一下留白 / 真点一下卡片')
        x, y = points['左外 5px']
        before = session.pad.click_count()
        session.click_at(x, y)
        report.check(session.pad.click_count() == before + 1,
                     '左外 5px 真点一下：垫子收到了（%d → %d）'
                     % (before, session.pad.click_count()))

        inside = (card['left'] + 4, card['top'] + 4)
        if session.park(*inside):      # 真实的手也是先动再点
            settle_ignore(session, False)
            who = session.who(*inside)[0]
            report.check(who == 'widget', '卡片内 4px 处归浮窗（WindowFromPoint → %s）' % who)
        before = session.pad.click_count()
        session.click_at(*inside)
        report.check(session.pad.click_count() == before,
                     '卡片本体真点一下：垫子一个都没收到（%d → %d）'
                     % (before, session.pad.click_count()))


def phase_collapsed(session, report):
    report.line('')
    report.line('B. 折叠成一行（C2）')
    done = session.probe.command('collapse')
    report.check(bool(done and done.get('ok')), '折叠命令被执行了')
    time.sleep(1.3)
    session.probe.command('snapshot')
    latest = session.probe.latest()
    report.check(latest.get('collapsed') is True,
                 '窗口已折叠（自报 collapsed=%s）' % latest.get('collapsed'))
    card = session.card_rect()
    height = card['bottom'] - card['top']
    report.check(0 < height < 80, '折叠后卡片只有一行高（实际 %.1fpx）' % height)

    row_x = (card['left'] + card['right']) / 2
    row_y = (card['top'] + card['bottom']) / 2

    report.line('')
    report.line('B1. 行本体可点、行外留白穿透')
    if session.park(row_x, row_y):
        step_state(session, False, '指针在这一行上', report)
        who = session.who(row_x, row_y)[0]
        report.check(who == 'widget', '这一行仍归浮窗（WindowFromPoint → %s）' % who)
    else:
        report.note('指针没停到这一行上，这一点没测')

    m5 = 5 * card['scale']
    sweep_margin(session, report, {
        '行上方 5px': (row_x, card['top'] - m5),
        '行下方 5px': (row_x, card['bottom'] + m5),
        '行左外 5px': (card['left'] - m5, row_y),
    })

    if session.args.real_input and session.pad:
        report.line('')
        report.line('B2. 折叠态真点一下')
        x, y = (row_x, card['bottom'] + m5)
        before = session.pad.click_count()
        session.click_at(x, y)
        report.check(session.pad.click_count() == before + 1,
                     '行下方 5px 真点一下：垫子收到了（%d → %d）'
                     % (before, session.pad.click_count()))
        # 行本体左侧 40px：压在「单号 · 名称」那段字上，那里没有按钮，点了什么也不发生
        inside = (card['left'] + 40, row_y)
        if session.park(*inside):
            settle_ignore(session, False)
            before = session.pad.click_count()
            session.click_at(*inside)
            report.check(session.pad.click_count() == before,
                         '这一行真点一下：垫子没收到（%d → %d）' % (before, session.pad.click_count()))


def phase_hidden_reopened(session, report):
    report.line('')
    report.line('C. 藏到托盘再打开、折叠与展开切换（C3）')
    session.probe.command('hide')
    time.sleep(0.7)
    session.probe.command('snapshot')
    report.check(session.probe.latest().get('visible') is False,
                 '窗口已藏起来（自报 visible=%s）' % session.probe.latest().get('visible'))

    session.probe.command('show')
    time.sleep(1.1)
    session.probe.command('snapshot')
    report.check(session.probe.latest().get('visible') is True,
                 '窗口已打开（自报 visible=%s）' % session.probe.latest().get('visible'))

    card = session.card_rect()
    row_x = (card['left'] + card['right']) / 2
    row_y = (card['top'] + card['bottom']) / 2
    below = card['bottom'] + 5 * card['scale']

    report.line('')
    report.line('C1. 打开之后：留白仍然穿透、卡片仍然可点')
    if session.park(card['left'] + 4, below):
        step_state(session, True, '打开后指针在留白里', report)
    else:
        report.note('指针没停到位：留白那一点没测')
    if session.park(row_x, row_y):
        step_state(session, False, '打开后指针在卡片上', report)
        who = session.who(row_x, row_y)[0]
        report.check(who == 'widget', '打开后卡片仍归浮窗（WindowFromPoint → %s）' % who)
    else:
        report.note('指针没停到卡片上：这一点没测')

    report.line('')
    report.line('C2. 再展开回 420×700')
    done = session.probe.command('expand')
    report.check(bool(done and done.get('ok')), '展开命令被执行了')
    time.sleep(1.4)
    session.probe.command('snapshot')
    latest = session.probe.latest()
    report.check(latest.get('collapsed') is False,
                 '窗口已展开（自报 collapsed=%s）' % latest.get('collapsed'))
    card = session.card_rect()
    height = card['bottom'] - card['top']
    report.check(height > 600, '展开后卡片回到整卡高度（实际 %.0fpx）' % height)

    # 展开是底边不动、往上长：折叠时那一行现在落在整卡的底部附近，仍然在卡片里
    if session.park(row_x, row_y):
        step_state(session, False, '展开后指针在卡片上', report)
        who = session.who(row_x, row_y)[0]
        report.check(who == 'widget', '展开后卡片仍归浮窗（WindowFromPoint → %s）' % who)
    else:
        report.note('指针没停到卡片上：这一点没测')

    x, y = (row_x, card['top'] - 5 * card['scale'])
    sweep_margin(session, report, {'展开后上边外 5px': (x, y)})
    step_state(session, True, '展开后指针在留白里', report)


def phase_static(session, report):
    """不动真鼠标的版本：只看「现在这个状态」下系统把点派给谁。

    适合这台机器正有人在用、不方便动鼠标的时候。它能回答「留白现在穿不穿」与
    「折叠/藏起来再打开之后还穿不穿」，但回答不了「指针进到卡片里会不会切回可交互」——
    那一条必须有真指针参与（带 --no-real-input 跑的日志里要写清这一点）。
    """
    report.note('没有真鼠标参与：下面只验证「当前状态下的系统路由」，不验证指针进出卡片的切换')

    def margin_points(card):
        mid_x = (card['left'] + card['right']) / 2
        mid_y = (card['top'] + card['bottom']) / 2
        m5 = 5 * card['scale']
        return {
            '左外 5px': (card['left'] - m5, mid_y),
            '右外 5px': (card['right'] + m5, mid_y),
            '上外 5px': (mid_x, card['top'] - m5),
            '下外 5px': (mid_x, card['bottom'] + m5),
            '留白最外侧': (card['client'][0] + card['scale'], card['client'][1] + card['scale']),
        }

    def sweep(label):
        card = session.card_rect()
        if session.probe.available and session.ignore_state() is not True:
            # 指针正好停在卡片上：这会儿留白当然是拦着的（本来就该拦）。不动鼠标就测不了这一组，
            # 如实记一笔，不把「指针恰好在卡片上」算成留白吃点击。
            report.note('%s：指针现在停在卡片上（可交互态），这一组留白没测；'
                        '要把指针移开才能测，那就得用真机鼠标模式' % label)
            return
        for name, (x, y) in margin_points(card).items():
            who = session.who(x, y)[0]
            report.check(who == 'pad', '%s · %s：点落到下面那个窗口（WindowFromPoint → %s）'
                         % (label, name, who))

    sweep('展开态')
    session.probe.command('collapse')
    time.sleep(1.3)
    session.probe.command('snapshot')
    report.check(session.probe.latest().get('collapsed') is True, '窗口已折叠')
    sweep('折叠态')
    session.probe.command('hide')
    time.sleep(0.7)
    session.probe.command('show')
    time.sleep(1.1)
    session.probe.command('snapshot')
    report.check(session.probe.latest().get('visible') is True, '窗口已重新打开')
    sweep('藏起来再打开之后（折叠态）')
    session.probe.command('expand')
    time.sleep(1.4)
    session.probe.command('snapshot')
    report.check(session.probe.latest().get('collapsed') is False, '窗口已展开')
    sweep('展开回来之后')


def phase_baseline(session, report):
    """老板本：留白应该**吃**点击。用来证明这个探针本身是敏感的，不是怎么跑都绿。"""
    card = session.card_rect()
    report.line('')
    report.line('基线（未修）：卡片 %.0f×%.0f' % (card['right'] - card['left'], card['bottom'] - card['top']))
    report.check(session.transparent_bit() is False,
                 '未修的版本没有设 WS_EX_TRANSPARENT（位=%s）' % session.transparent_bit())
    mid_x = (card['left'] + card['right']) / 2
    mid_y = (card['top'] + card['bottom']) / 2
    m5 = 5 * card['scale']
    for label, (x, y) in {
        '左外 5px': (card['left'] - m5, mid_y),
        '右外 5px': (card['right'] + m5, mid_y),
        '上外 5px': (mid_x, card['top'] - m5),
        '下外 5px': (mid_x, card['bottom'] + m5),
    }.items():
        who = session.who(x, y)[0]
        report.check(who == 'widget',
                     '%s：点被浮窗吃掉（WindowFromPoint → %s）——这就是要修的那个手感' % (label, who))
    who = session.who(mid_x, mid_y)[0]
    report.check(who == 'widget', '卡片正中本来就归浮窗（WindowFromPoint → %s）' % who)


# ---------------------------------------------------------------- 主流程

def find_electron(app_dir):
    candidates = [
        os.path.join(app_dir, 'node_modules', 'electron', 'dist', 'electron.exe'),
        os.path.join(os.path.dirname(app_dir), 'app', 'node_modules', 'electron', 'dist', 'electron.exe'),
        '{{WORKSPACE}}/panel/app/node_modules/electron/dist/electron.exe',
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    parser = argparse.ArgumentParser(description='CD-016 留白穿透探针')
    parser.add_argument('--app', help='要跑的浮窗目录（默认本仓库的 app/）')
    parser.add_argument('--electron', help='electron.exe 路径（默认在 app/node_modules 里找）')
    parser.add_argument('--log', help='把报告同时写进这个文件')
    parser.add_argument('--expect-bug', action='store_true',
                        help='跑到修之前的版本上：断言留白**吃**点击（证明探针敏感）')
    parser.add_argument('--no-real-input', action='store_true',
                        help='不动真鼠标：只做 WindowFromPoint')
    parser.add_argument('--idle-wait', type=float, default=90.0,
                        help='动鼠标之前最多等系统安静多少秒（默认 90）')
    parser.add_argument('--keep', action='store_true', help='保留临时目录，方便事后看')
    args = parser.parse_args()
    args.real_input = not args.no_real_input

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_dir = os.path.abspath(args.app or os.path.join(repo, 'app'))
    electron = args.electron or find_electron(app_dir)
    if not electron:
        print('找不到 electron.exe（可以 --electron 指定）', file=sys.stderr)
        return 2
    if not os.path.exists(os.path.join(app_dir, 'main.mjs')):
        print('%s 不像浮窗目录（没有 main.mjs）' % app_dir, file=sys.stderr)
        return 2
    if args.expect_bug:
        args.real_input = False

    report = Report(args.log)
    report.line('CD-016 留白穿透探针 · ' + time.strftime('%Y-%m-%d %H:%M:%S'))
    report.line('浮窗目录：%s' % app_dir)
    report.line('模式：%s' % ('基线（未修，预期留白吃点击）' if args.expect_bug
                             else ('只问 WindowFromPoint，不动真鼠标' if not args.real_input
                                   else '真机鼠标（只点自己的窗口）')))

    tmp = tempfile.mkdtemp(prefix='cd016-probe-')
    state_path = '' if args.expect_bug else os.path.join(tmp, 'probe-state.json')
    output_path = os.path.join(tmp, 'electron.log')
    env = dict(os.environ)
    if state_path:
        env['CINDY_WIDGET_PROBE'] = state_path
    export = env.get('CINDY_WIDGET_EXPORT') or '{{WORKSPACE}}/.data/progress-export.json'
    if os.path.exists(export):
        env['CINDY_WIDGET_EXPORT'] = export
    env['ELECTRON_DISABLE_SECURITY_WARNINGS'] = '1'

    console = open(output_path, 'wb')
    proc = subprocess.Popen(
        [electron, app_dir, '--user-data-dir=' + os.path.join(tmp, 'ud')],
        env=env, stdout=console, stderr=subprocess.STDOUT)
    pad = None
    original_cursor = cursor_pos()
    try:
        probe = Probe(state_path)
        if state_path:
            state = wait_for(lambda: (probe.state() or {}).get('last', {}).get('hwnd'), 40.0)
            if not state:
                report.line('浮窗没起来（等不到探针状态文件）')
                console.close()
                tail = open(output_path, encoding='utf-8', errors='replace').read()[-2000:]
                report.line(tail)
                return 2
            widget = int(probe.latest()['hwnd'])
        else:
            widget = wait_for(lambda: find_window_by_pid(proc.pid), 40.0)
            if not widget:
                report.line('浮窗窗口没出现（pid %d）' % proc.pid)
                console.close()
                report.line(open(output_path, encoding='utf-8', errors='replace').read()[-2000:])
                return 2

        report.line('浮窗窗口：hwnd=0x%X pid=%d class=%s' % (widget, pid_of(widget), class_name(widget)))
        report.line('窗口外框：%s' % (window_rect(widget),))

        # 挪到一个干净的地方：主检出里那个旧浮窗可能正开着（这一段只动我们自己这一个窗口）。
        # 位置按主屏工作区算，不写死：写死的点在小屏上会把窗口推到任务栏底下。
        if state_path:
            left, top, right, bottom = window_rect(widget)
            frame_w, frame_h = right - left, bottom - top
            area = work_area()
            client_w = client_rect(widget)[2] - client_rect(widget)[0]
            latest = probe.latest()
            content = latest.get('contentBounds')
            scale = (client_w / float(content['width'])) if content and content.get('width') else 1.0
            target = (
                area[0] + max(20, int((area[2] - area[0] - frame_w) * 0.18)),
                area[1] + max(0, (area[3] - area[1] - frame_h) // 2),
            )
            dip = latest.get('bounds') or {}
            if dip:
                probe.command('move',
                              dip['x'] + int(round((target[0] - left) / scale)),
                              dip['y'] + int(round((target[1] - top) / scale)))
                time.sleep(0.9)
        else:
            # 基线跑的是没探针的老板本：直接按窗口句柄挪（只影响这一次验证用的临时实例）
            left, top, right, bottom = window_rect(widget)
            area = work_area()
            target = (
                area[0] + max(20, int((area[2] - area[0] - (right - left)) * 0.18)),
                area[1] + max(0, (area[3] - area[1] - (bottom - top)) // 2),
            )
            user32.SetWindowPos(widget, None, target[0], target[1], 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.6)
        report.line('主屏工作区：%s' % (work_area(),))
        report.line('挪位置之后的外框：%s' % (window_rect(widget),))

        left, top, right, bottom = window_rect(widget)
        pad = Pad((left - 120, top - 120, right + 120, bottom + 120))
        report.line('垫子窗口：hwnd=0x%X rect=%s' % (pad.hwnd, (left - 120, top - 120, right + 120, bottom + 120)))

        session = Session(report, probe, widget, pad, args)
        card = session.card_rect()
        report.line('卡片（屏幕矩形）：left=%.1f top=%.1f right=%.1f bottom=%.1f  缩放比=%.4f'
                    % (card['left'], card['top'], card['right'], card['bottom'], card['scale']))

        if args.real_input:
            idle = idle_seconds()
            if idle < 3.0:
                report.note('系统刚有输入（空闲 %.1fs），先等它安静下来再动鼠标' % idle)
                if wait_for(lambda: idle_seconds() >= 3.0, args.idle_wait, interval=0.5):
                    report.note('等到了（空闲 %.1fs）' % idle_seconds())
                else:
                    report.note('一直有人在用（已等 %.0fs），继续——但只点自己的窗口' % args.idle_wait)

        if args.expect_bug:
            phase_baseline(session, report)
        elif not args.real_input:
            phase_static(session, report)
        else:
            phase_expanded(session, report)
            phase_collapsed(session, report)
            phase_hidden_reopened(session, report)

        if state_path:
            events = (probe.state() or {}).get('events') or []
            report.line('')
            report.line('浮窗自己的状态记录（最后 %d 条）：' % min(len(events), 40))
            for event in events[-40:]:
                report.line('  %s %s' % (event.get('at', ''), json.dumps(
                    {k: event.get(k) for k in ('kind', 'ignore', 'reason', 'collapsed', 'visible')},
                    ensure_ascii=False)))
    finally:
        if args.real_input:
            user32.SetCursorPos(original_cursor[0], original_cursor[1])
        if pad is not None:
            pad.destroy()
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            console.close()
        except Exception:
            pass
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    report.line('')
    report.line('汇总：%d 项，%d 项失败' % (report.checks, report.failures))
    report.flush()
    return 0 if report.failures == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
