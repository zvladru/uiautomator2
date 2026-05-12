"""Shizuku/rish backend for local same-device UI automation.

This module intentionally implements a small uiautomator2-compatible subset on
top of Android shell tools available through Shizuku/rish:

- ``uiautomator dump`` for hierarchy reads
- ``input`` for key, tap, long tap, and swipe actions
- ``screencap`` for screenshots
- ``am``/``dumpsys``/``wm`` for app and display helpers

It does not start the upstream uiautomator2 JSON-RPC server and therefore does
not provide the complete atx-agent-backed API.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from PIL import Image

from uiautomator2.abstract import ShellResponse
from uiautomator2.exceptions import UiObjectNotFoundError


DEFAULT_RISH = "/data/data/com.termux/files/home/bin/rish"
DEFAULT_WINDOW_DUMP = "/sdcard/window_dump.xml"


def _quote_cmd(cmdargs: Union[List[str], Tuple[str, ...], str]) -> str:
    if isinstance(cmdargs, str):
        return cmdargs
    return " ".join(shlex.quote(str(part)) for part in cmdargs)


def _bool_text(value: Any) -> str:
    return "true" if bool(value) else "false"


def _parse_bounds(bounds: str) -> Dict[str, int]:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        raise ValueError(f"invalid Android bounds: {bounds!r}")
    left, top, right, bottom = (int(part) for part in match.groups())
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _bounds_tuple(bounds: str) -> Tuple[int, int, int, int]:
    parsed = _parse_bounds(bounds)
    return parsed["left"], parsed["top"], parsed["right"], parsed["bottom"]


@dataclass
class RishShellRunner:
    """Small wrapper around a Shizuku ``rish`` executable."""

    executable: str = DEFAULT_RISH
    timeout: float = 30.0

    def run(self, cmdargs: Union[List[str], Tuple[str, ...], str], check: bool = False) -> ShellResponse:
        command = _quote_cmd(cmdargs)
        proc = subprocess.run(
            [self.executable, "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
        )
        output = proc.stdout.decode("utf-8", errors="replace")
        if proc.stderr:
            output += proc.stderr.decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise RuntimeError(f"rish command failed ({proc.returncode}): {command}\n{output}")
        return ShellResponse(output=output, exit_code=proc.returncode)


class RishUiObject:
    def __init__(self, device: "RishDevice", selector: Dict[str, Any]):
        self.session = device
        self.selector = selector
        self.scroll = RishScroll(device)

    @property
    def exists(self) -> bool:
        return self._find_node() is not None

    @property
    def info(self) -> Dict[str, Any]:
        node = self._find_node()
        if node is None:
            raise UiObjectNotFoundError(0, "object not found", self.selector)
        bounds = _parse_bounds(node.attrib.get("bounds", ""))
        return {
            "text": node.attrib.get("text", ""),
            "className": node.attrib.get("class", ""),
            "packageName": node.attrib.get("package", ""),
            "resourceName": node.attrib.get("resource-id", ""),
            "contentDescription": node.attrib.get("content-desc", ""),
            "checkable": node.attrib.get("checkable") == "true",
            "checked": node.attrib.get("checked") == "true",
            "clickable": node.attrib.get("clickable") == "true",
            "enabled": node.attrib.get("enabled") != "false",
            "focusable": node.attrib.get("focusable") == "true",
            "focused": node.attrib.get("focused") == "true",
            "scrollable": node.attrib.get("scrollable") == "true",
            "selected": node.attrib.get("selected") == "true",
            "bounds": bounds,
            "visibleBounds": bounds,
        }

    def info_list(self) -> List[Dict[str, Any]]:
        return [self._node_info(node) for node in self._matching_nodes()]

    def wait(self, timeout: Optional[float] = None) -> bool:
        deadline = time.time() + (timeout if timeout is not None else self.session.wait_timeout)
        while time.time() < deadline:
            if self.exists:
                return True
            time.sleep(0.15)
        return self.exists

    def must_wait(self, timeout: Optional[float] = None) -> bool:
        if self.wait(timeout):
            return True
        raise UiObjectNotFoundError(0, "object not found", self.selector)

    def bounds(self) -> Tuple[int, int, int, int]:
        node = self._find_node()
        if node is None:
            raise UiObjectNotFoundError(0, "object not found", self.selector)
        return _bounds_tuple(node.attrib.get("bounds", ""))

    def center(self, offset: Optional[Tuple[float, float]] = (0.5, 0.5)) -> Tuple[int, int]:
        left, top, right, bottom = self.bounds()
        xoff, yoff = offset or (0.5, 0.5)
        return int(left + (right - left) * xoff), int(top + (bottom - top) * yoff)

    def click(self, timeout: Optional[float] = None, offset: Optional[Tuple[float, float]] = None) -> None:
        self.must_wait(timeout=timeout)
        x, y = self.center(offset=offset)
        self.session.click(x, y)

    def click_exists(self, timeout: float = 0) -> bool:
        if not self.wait(timeout):
            return False
        self.click()
        return True

    def long_click(self, duration: float = 0.5, timeout: Optional[float] = None) -> None:
        self.must_wait(timeout=timeout)
        x, y = self.center()
        self.session.long_click(x, y, duration=duration)

    def get_text(self, timeout: Optional[float] = None) -> str:
        self.must_wait(timeout=timeout)
        return self.info.get("text", "")

    def _matching_nodes(self) -> Iterable[ET.Element]:
        root = self.session.xml_root()
        if root is None:
            return []
        return [node for node in root.iter("node") if self._matches(node)]

    def _find_node(self) -> Optional[ET.Element]:
        for node in self._matching_nodes():
            return node
        return None

    def _node_info(self, node: ET.Element) -> Dict[str, Any]:
        bounds = _parse_bounds(node.attrib.get("bounds", ""))
        return {
            "text": node.attrib.get("text", ""),
            "className": node.attrib.get("class", ""),
            "packageName": node.attrib.get("package", ""),
            "resourceName": node.attrib.get("resource-id", ""),
            "contentDescription": node.attrib.get("content-desc", ""),
            "bounds": bounds,
            "visibleBounds": bounds,
        }

    def _matches(self, node: ET.Element) -> bool:
        for key, expected in self.selector.items():
            value = self._node_value(node, key)
            if key.endswith("Contains"):
                if str(expected) not in str(value):
                    return False
            elif key.endswith("StartsWith"):
                if not str(value).startswith(str(expected)):
                    return False
            elif key.endswith("Matches"):
                if not re.search(str(expected), str(value)):
                    return False
            elif isinstance(expected, bool):
                if value != _bool_text(expected):
                    return False
            elif key == "instance":
                continue
            elif value != str(expected):
                return False
        return True

    @staticmethod
    def _node_value(node: ET.Element, key: str) -> str:
        mapping = {
            "text": "text",
            "textContains": "text",
            "textStartsWith": "text",
            "textMatches": "text",
            "className": "class",
            "classNameMatches": "class",
            "description": "content-desc",
            "descriptionContains": "content-desc",
            "descriptionStartsWith": "content-desc",
            "descriptionMatches": "content-desc",
            "resourceId": "resource-id",
            "resourceIdMatches": "resource-id",
            "packageName": "package",
            "packageNameMatches": "package",
            "checkable": "checkable",
            "checked": "checked",
            "clickable": "clickable",
            "enabled": "enabled",
            "focusable": "focusable",
            "focused": "focused",
            "scrollable": "scrollable",
            "selected": "selected",
        }
        return node.attrib.get(mapping.get(key, key), "")


class RishScroll:
    def __init__(self, device: "RishDevice"):
        self._device = device

    def to(self, **selector: Any) -> bool:
        target = self._device(**selector)
        if target.exists:
            return True
        for _ in range(self._device.scroll_attempts):
            self.forward()
            if target.exists:
                return True
        return False

    def forward(self, steps: Optional[int] = None) -> None:
        del steps
        width, height = self._device.window_size()
        x = width // 2
        self._device.swipe(x, int(height * 0.78), x, int(height * 0.54), duration=0.12)

    def backward(self, steps: Optional[int] = None) -> None:
        del steps
        width, height = self._device.window_size()
        x = width // 2
        self._device.swipe(x, int(height * 0.54), x, int(height * 0.78), duration=0.12)


class RishDevice:
    """uiautomator2-compatible local device backed by Shizuku/rish."""

    KEYCODES = {
        "home": 3,
        "back": 4,
        "menu": 82,
        "left": 21,
        "right": 22,
        "up": 19,
        "down": 20,
        "center": 23,
        "enter": 66,
        "delete": 67,
        "del": 67,
        "tab": 61,
        "recent": 187,
        "volume_up": 24,
        "volume_down": 25,
        "power": 26,
    }

    def __init__(
        self,
        rish: str = DEFAULT_RISH,
        window_dump: str = DEFAULT_WINDOW_DUMP,
        runner: Optional[RishShellRunner] = None,
    ):
        self.rish = rish
        self.window_dump = window_dump
        self.wait_timeout = 10.0
        self.scroll_attempts = 8
        self._runner = runner or RishShellRunner(rish)
        self._check_backend()

    @property
    def serial(self) -> str:
        output = self.shell("getprop ro.serialno").output.strip()
        return output or "rish-local"

    def shell(self, cmdargs: Union[List[str], Tuple[str, ...], str]) -> ShellResponse:
        return self._runner.run(cmdargs)

    def dump_hierarchy(self, compressed: bool = False, pretty: bool = False, max_depth: Optional[int] = None) -> str:
        del compressed, max_depth
        response = self.shell(f"uiautomator dump >/dev/null 2>&1; cat {shlex.quote(self.window_dump)}")
        content = response.output
        if pretty:
            root = ET.fromstring(content)
            content = ET.tostring(root, encoding="unicode")
        return content

    def xml_root(self) -> Optional[ET.Element]:
        xml = self.dump_hierarchy()
        try:
            return ET.fromstring(xml)
        except ET.ParseError:
            try:
                from lxml import etree
                parser = etree.XMLParser(recover=True, encoding="utf-8")
                return etree.fromstring(xml.encode("utf-8", errors="replace"), parser=parser)
            except Exception:
                return None

    def window_size(self) -> Tuple[int, int]:
        output = self.shell("wm size").output
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
        if match:
            return int(match.group(1)), int(match.group(2))
        root = self.xml_root()
        if root is not None:
            max_right = max_bottom = 0
            for node in root.iter("node"):
                bounds = node.attrib.get("bounds")
                if not bounds:
                    continue
                parsed = _parse_bounds(bounds)
                max_right = max(max_right, parsed["right"])
                max_bottom = max(max_bottom, parsed["bottom"])
            if max_right and max_bottom:
                return max_right, max_bottom
        return 1080, 2340

    def click(self, x: Union[int, float], y: Union[int, float]) -> None:
        x, y = self._rel2abs(x, y)
        self.shell(f"input tap {int(x)} {int(y)}")

    def double_click(self, x: Union[int, float], y: Union[int, float], duration: float = 0.1) -> None:
        self.click(x, y)
        time.sleep(duration)
        self.click(x, y)

    def long_click(self, x: Union[int, float], y: Union[int, float], duration: float = 0.5) -> None:
        x, y = self._rel2abs(x, y)
        self.shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration * 1000)}")

    def swipe(
        self,
        fx: Union[int, float],
        fy: Union[int, float],
        tx: Union[int, float],
        ty: Union[int, float],
        duration: Optional[float] = None,
        steps: Optional[int] = None,
    ) -> None:
        del steps
        fx, fy = self._rel2abs(fx, fy)
        tx, ty = self._rel2abs(tx, ty)
        duration_ms = int((duration if duration is not None else 0.2) * 1000)
        self.shell(f"input swipe {int(fx)} {int(fy)} {int(tx)} {int(ty)} {duration_ms}")

    def press(self, key: Union[int, str], meta: Any = None) -> None:
        del meta
        if isinstance(key, int):
            code = key
        else:
            code = self.KEYCODES.get(key.lower(), key.upper())
        self.shell(f"input keyevent {code}")

    def keyevent(self, key: Union[int, str]) -> None:
        self.press(key)

    def screenshot(self, filename: Optional[str] = None, format: str = "pillow", display_id: Optional[int] = None):
        del display_id
        local_path = filename or "/data/data/com.termux/files/usr/tmp/rish-screenshot.png"
        self.shell(f"screencap -p {shlex.quote(local_path)}")
        image = Image.open(local_path)
        if filename:
            return None
        if format == "pillow":
            return image
        if format == "opencv":
            try:
                import cv2
                import numpy as np
            except Exception as exc:  # pragma: no cover
                raise RuntimeError("opencv screenshot format requires cv2 and numpy") from exc
            return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        return image

    def app_current(self) -> Dict[str, str]:
        output = self.shell(
            "dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity' | head -n 1"
        ).output
        match = re.search(r" ([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", output)
        if not match:
            return {"package": "", "activity": ""}
        return {"package": match.group(1), "activity": match.group(2)}

    def app_start(
        self,
        package_name: str,
        activity: Optional[str] = None,
        wait: bool = False,
        stop: bool = False,
        use_monkey: bool = False,
    ) -> None:
        if stop:
            self.app_stop(package_name)
        if use_monkey or not activity:
            self.shell(["monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"])
        else:
            self.shell(["am", "start", "-n", f"{package_name}/{activity}"])
        if wait:
            self.app_wait(package_name)

    def app_stop(self, package_name: str) -> None:
        self.shell(["am", "force-stop", package_name])

    def app_wait(self, package_name: str, timeout: float = 20.0, front: bool = False) -> int:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if front and self.app_current().get("package") == package_name:
                return 1
            if not front and package_name in self.app_list_running():
                return 1
            time.sleep(0.5)
        return 0

    def app_list(self, filter: Optional[str] = None) -> List[str]:
        args = ["pm", "list", "packages"]
        if filter:
            args.append(filter)
        output = self.shell(args).output
        return re.findall(r"package:([^\s]+)", output)

    def app_list_running(self) -> List[str]:
        output = self.shell("ps -A").output
        return [line.split()[-1] for line in output.splitlines()[1:] if line.split()]

    def exists(self, **kwargs: Any) -> bool:
        return self(**kwargs).exists

    def implicitly_wait(self, seconds: Optional[float] = None) -> float:
        if seconds is not None:
            self.wait_timeout = float(seconds)
        return self.wait_timeout

    def __call__(self, **kwargs: Any) -> RishUiObject:
        return RishUiObject(self, kwargs)

    def _rel2abs(self, x: Union[int, float], y: Union[int, float]) -> Tuple[int, int]:
        width, height = self.window_size()
        if isinstance(x, float) and 0 <= x <= 1:
            x = width * x
        if isinstance(y, float) and 0 <= y <= 1:
            y = height * y
        return int(x), int(y)

    def _check_backend(self) -> None:
        if not Path(self.rish).exists():
            raise FileNotFoundError(f"rish executable not found: {self.rish}")
        response = self.shell("echo rish-ok")
        if response.exit_code != 0 or "rish-ok" not in response.output:
            raise RuntimeError(f"rish backend check failed: {response.output}")


def connect_rish(rish: Optional[str] = None, **kwargs: Any) -> RishDevice:
    """Connect to the current Android device through Shizuku/rish.

    Args:
        rish: Path to the rish executable. Defaults to ``$RISH`` or the common
            Termux path ``/data/data/com.termux/files/home/bin/rish``.

    Returns:
        RishDevice: uiautomator2-compatible same-device backend.
    """

    return RishDevice(rish=rish or os.getenv("RISH", DEFAULT_RISH), **kwargs)
