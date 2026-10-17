"""Capture only a selected portion of the visible MapleStory game client."""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes as wt

import mss
import numpy as np

from game_capture import CaptureFailure, USER, game_geometry, physical_dpi


@dataclass(frozen=True)
class QuizRegion:
    hwnd: int
    client_size: tuple[int, int]
    dpi: int
    left: int
    top: int
    width: int
    height: int


def validate_region(region, geometry):
    client = geometry['client']
    if (region.hwnd != geometry['hwnd'] or region.client_size != (client['width'], client['height'])
            or region.dpi != geometry['dpi']):
        raise ValueError('游戏尺寸或缩放已变化，请重新框选题目')
    if (region.left < 0 or region.top < 0 or region.width < 40 or region.height < 20
            or region.left+region.width > client['width']
            or region.top+region.height > client['height']):
        raise ValueError('题目区域必须位于游戏窗口内')
    return dict(left=client['left']+region.left, top=client['top']+region.top,
                width=region.width, height=region.height)


def region_visible(hwnd, roi):
    # Reject every visible top-level window above the game intersecting the
    # selection. Unlike three sample points, this catches small overlay windows.
    from game_capture import overlap
    clear = True
    seen_game = False
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    USER.IsWindowVisible.argtypes = [wt.HWND]
    USER.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    USER.EnumWindows.argtypes = [callback_type, wt.LPARAM]

    @callback_type
    def visit(window, _):
        nonlocal clear, seen_game
        if window == hwnd:
            seen_game = True
            return False
        if USER.IsWindowVisible(window) and not USER.IsIconic(window):
            rect = wt.RECT()
            if USER.GetWindowRect(window, ctypes.byref(rect)):
                bounds = dict(left=rect.left, top=rect.top,
                              width=rect.right-rect.left, height=rect.bottom-rect.top)
                if overlap(bounds, roi) > 0:
                    clear = False
                    return False
        return True

    USER.EnumWindows(visit, 0)
    return clear and seen_game


def capture_region(region):
    physical_dpi()
    before = game_geometry(include_monitor=False)
    roi = validate_region(region, before)
    if not region_visible(before['hwnd'], roi):
        raise CaptureFailure('题目被遮挡，请移开辅助窗或其他窗口', 'occluded')
    with mss.mss() as capture:
        pixels = np.asarray(capture.grab(roi))[:, :, :3].copy()
    after = game_geometry(include_monitor=False)
    if before != after or not region_visible(after['hwnd'], roi):
        raise CaptureFailure('游戏窗口正在移动或被遮挡，等待恢复', 'window_moved')
    return pixels
