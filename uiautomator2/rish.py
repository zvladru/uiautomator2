"""Shizuku/rish backends for local same-device UI automation.

The default backend starts the real uiautomator2 ``u2.jar`` JSON-RPC server
through Shizuku/rish and talks to it over ``127.0.0.1:9008``. It is intended for
Termux-on-device setups where ADB is unavailable but Shizuku is authorized.

The module also keeps a lightweight shell/XML fallback backend on top of Android
shell tools:

- ``uiautomator dump`` for hierarchy reads
- ``input`` for key, tap, long tap, and swipe actions
- ``screencap`` for screenshots
- ``am``/``dumpsys``/``wm`` for app and display helpers
"""

from __future__ import annotations

import atexit
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from PIL import Image

from uiautomator2._proto import HTTP_TIMEOUT
from uiautomator2.core import HTTPResponse
from uiautomator2.abstract import ShellResponse
from uiautomator2.exceptions import (
    AccessibilityServiceAlreadyRegisteredError,
    HTTPError,
    HTTPTimeoutError,
    LaunchUiAutomationError,
    RPCInvalidError,
    RPCStackOverflowError,
    RPCUnknownError,
    UiAutomationNotConnectedError,
    UiObjectNotFoundError,
)
from uiautomator2.utils import with_package_resource


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
        return self.run_command(command, check=check)

    def run_command(
        self,
        command: str,
        input_bytes: Optional[bytes] = None,
        check: bool = False,
        timeout: Optional[float] = None,
    ) -> ShellResponse:
        proc = subprocess.run(
            [self.executable, "-c", command],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout or self.timeout,
        )
        output = proc.stdout.decode("utf-8", errors="replace")
        if proc.stderr:
            output += proc.stderr.decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise RuntimeError(f"rish command failed ({proc.returncode}): {command}\n{output}")
        return ShellResponse(output=output, exit_code=proc.returncode)

    def popen(self, command: str) -> subprocess.Popen:
        return subprocess.Popen(
            [self.executable, "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


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

    def app_clear(self, package_name: str) -> None:
        self.shell(["pm", "clear", package_name])

    def app_info(self, package_name: str):
        output = self.shell(["dumpsys", "package", package_name]).output
        if package_name not in output:
            return None
        version_name = ""
        version_code = 0
        version_name_match = re.search(r"versionName=([^\s]+)", output)
        version_code_match = re.search(r"versionCode=(\d+)", output)
        if version_name_match:
            version_name = version_name_match.group(1)
        if version_code_match:
            version_code = int(version_code_match.group(1))

        class AppInfo:
            pass

        info = AppInfo()
        info.version_name = version_name
        info.version_code = version_code
        return info

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

    def getprop(self, key: str) -> str:
        return self.shell(["getprop", key]).output.strip()

    def wlan_ip(self) -> Optional[str]:
        output = self.shell("ip -f inet addr show wlan0 2>/dev/null").output
        match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", output)
        return match.group(1) if match else None

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


class RishProcess:
    def __init__(self, runner: RishShellRunner, command: str):
        self._proc = runner.popen(command)
        self._event = threading.Event()
        self._output = bytearray()
        thread = threading.Thread(target=self._read_output, name="rish-uiautomator-output", daemon=True)
        thread.start()

    @property
    def output(self) -> bytes:
        return bytes(self._output)

    def wait(self, timeout: float = 3.0) -> bool:
        return self._event.wait(timeout=timeout)

    def pool(self) -> Optional[int]:
        return self._proc.poll()

    def kill(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
        self._event.set()

    def _read_output(self) -> None:
        try:
            assert self._proc.stdout is not None
            while True:
                chunk = self._proc.stdout.read(1024)
                if not chunk:
                    break
                self._output.extend(chunk)
        finally:
            self._event.set()


def _direct_http_request(
    device_port: int,
    method: str,
    path: str,
    data: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
    print_request: bool = False,
) -> HTTPResponse:
    try:
        if print_request:
            start_time = datetime.datetime.now()
            current_time = start_time.strftime("%H:%M:%S.%f")[:-3]
            url = f"http://127.0.0.1:{device_port}{path}"
            fields = [current_time, f"$ curl -X {method}", url]
            if data:
                fields.append(f"-d '{json.dumps(data)}'")
            print(f"# http timeout={timeout}")
            print(" ".join(fields))

        headers = {
            "User-Agent": "uiautomator2-rish",
            "Accept-Encoding": "",
            "Content-Type": "application/json",
        }
        conn = HTTPConnection("127.0.0.1", device_port, timeout=timeout)
        try:
            if data is None:
                conn.request(method, path, headers=headers)
            else:
                conn.request(method, path, json.dumps(data), headers=headers)
            response = conn.getresponse()
            content = response.read()
            if response.status != 200:
                raise HTTPError(f"HTTP request failed: {response.status} {response.reason}")
            result = HTTPResponse(content)
        finally:
            conn.close()

        if print_request:
            end_time = datetime.datetime.now()
            current_time = end_time.strftime("%H:%M:%S.%f")[:-3]
            print(f"{current_time} Response >>>")
            print(result.text.rstrip())
            print("<<< END timed_used = %.3f\n" % (end_time - start_time).total_seconds())
        return result
    except TimeoutError as exc:
        raise HTTPTimeoutError(f"HTTP request timeout: {exc}") from exc
    except OSError as exc:
        raise HTTPError(f"HTTP request failed: {exc}") from exc


def _direct_jsonrpc_call(
    device_port: int,
    method: str,
    params: Any,
    timeout: float,
    print_request: bool,
) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    response = _direct_http_request(
        device_port,
        "POST",
        "/jsonrpc/0",
        payload,
        timeout=timeout,
        print_request=print_request,
    )
    data = response.json()
    if not isinstance(data, dict):
        raise RPCInvalidError("Unknown RPC error: not a dict")

    if "error" in data:
        code = data["error"].get("code")
        message = data["error"].get("message", "")
        stacktrace = data["error"].get("data")
        if "UiAutomation not connected" in response.text:
            raise UiAutomationNotConnectedError("UiAutomation not connected")
        if "android.os.DeadObjectException" in message:
            raise UiAutomationNotConnectedError("android.os.DeadObjectException")
        if "android.os.DeadSystemRuntimeException" in message:
            raise UiAutomationNotConnectedError("android.os.DeadSystemRuntimeException")
        if "uiautomator.UiObjectNotFoundException" in message:
            raise UiObjectNotFoundError(code, message, params)
        if "java.lang.StackOverflowError" in message:
            raise RPCStackOverflowError(
                f"StackOverflowError: {message}",
                params,
                stacktrace[:1000] + "..." + stacktrace[-1000:],
            )
        raise RPCUnknownError(f"Unknown RPC error: {code} {message}", params, stacktrace)

    if "result" not in data:
        raise RPCInvalidError("Unknown RPC error: no result field")
    return data["result"]


class RishUiautomatorServer:
    _lock = threading.Lock()

    def __init__(self, shell_device: RishDevice, device_server_port: int = 9008):
        self._dev = shell_device
        self._runner = shell_device._runner
        self._process: Optional[RishProcess] = None
        self._debug = False
        self._device_server_port = device_server_port
        self.start_uiautomator()
        atexit.register(self.stop_uiautomator, wait=False)

    @property
    def debug(self) -> bool:
        return self._debug

    @debug.setter
    def debug(self, value: bool) -> None:
        self._debug = bool(value)

    def start_uiautomator(self) -> None:
        with self._lock:
            self._setup_jar()
            if self._process and self._process.pool() is not None:
                self._process = None
            if not self._check_alive():
                self._launch_and_wait(kill_first=False)

    def stop_uiautomator(self, wait: bool = True) -> None:
        with self._lock:
            if self._process:
                self._process.kill()
                self._process = None
            self._kill_orphaned_server()
        if wait:
            deadline = time.time() + 10
            while time.time() < deadline:
                if not self._check_alive():
                    return
                time.sleep(0.5)

    def jsonrpc_call(self, method: str, params: Any = None, timeout: float = HTTP_TIMEOUT) -> Any:
        try:
            return _direct_jsonrpc_call(self._device_server_port, method, params, timeout, self._debug)
        except (HTTPError, UiAutomationNotConnectedError):
            self.stop_uiautomator()
            self.start_uiautomator()
            return _direct_jsonrpc_call(self._device_server_port, method, params, timeout, self._debug)

    def _setup_jar(self) -> None:
        jar_path = self._local_u2_jar()
        target_path = "/data/local/tmp/u2.jar"
        if self._check_device_file_hash(jar_path, target_path):
            return
        data = Path(jar_path).read_bytes()
        self._runner.run_command(
            f"cat > {target_path}.tmp && chmod 644 {target_path}.tmp && mv {target_path}.tmp {target_path}",
            input_bytes=data,
            check=True,
            timeout=60,
        )

    def _local_u2_jar(self) -> Path:
        try:
            with with_package_resource("assets/u2.jar") as jar_path:
                path = Path(jar_path)
                if path.is_file() and path.stat().st_size > 0:
                    return path
        except FileNotFoundError:
            pass

        assets_dir = Path(__file__).resolve().parent / "assets"
        sync_script = assets_dir / "sync.sh"
        if sync_script.is_file():
            subprocess.run(["bash", str(sync_script)], cwd=str(assets_dir), check=True, timeout=120)
            jar_path = assets_dir / "u2.jar"
            if jar_path.is_file() and jar_path.stat().st_size > 0:
                return jar_path
        raise FileNotFoundError("assets/u2.jar not found; run uiautomator2/assets/sync.sh")

    def _check_device_file_hash(self, local_file: Union[str, Path], remote_file: str) -> bool:
        local_md5 = hashlib.md5(Path(local_file).read_bytes()).hexdigest()
        output = self._dev.shell(["toybox", "md5sum", remote_file]).output
        if "toybox" in output and "not found" in output:
            output = self._dev.shell(["md5", remote_file]).output
        return local_md5 in output

    def _check_alive(self) -> bool:
        try:
            response = _direct_http_request(self._device_server_port, "GET", "/ping", timeout=1.0)
            return response.content == b"pong"
        except (HTTPError, ConnectionError, HTTPTimeoutError):
            return False

    def _wait_ready(self, launch_timeout: float = 30.0) -> None:
        deadline = time.time() + launch_timeout
        output_buffer = ""
        while time.time() < deadline:
            output = self._process.output.decode("utf-8", errors="ignore") if self._process else ""
            output_buffer += output
            if "already registered" in output:
                raise AccessibilityServiceAlreadyRegisteredError(output)
            if self._process and self._process.pool() is not None:
                raise LaunchUiAutomationError("server quit unexpectedly", output_buffer)
            if self._check_alive():
                return
            time.sleep(0.5)
        raise LaunchUiAutomationError("server not ready", output_buffer)

    def _launch_and_wait(self, kill_first: bool) -> None:
        if kill_first:
            self._kill_orphaned_server()
        command = "CLASSPATH=/data/local/tmp/u2.jar app_process / com.wetest.uia2.Main"
        self._process = RishProcess(self._runner, command)
        try:
            self._wait_ready()
        except LaunchUiAutomationError:
            if kill_first:
                raise
            self._kill_orphaned_server()
            self._launch_and_wait(kill_first=True)

    def _kill_orphaned_server(self) -> None:
        self._dev.shell("pkill -f 'com.wetest.uia2.Main' >/dev/null 2>&1 || true")


def connect_rish_shell(rish: Optional[str] = None, **kwargs: Any) -> RishDevice:
    return RishDevice(rish=rish or os.getenv("RISH", DEFAULT_RISH), **kwargs)


def connect_rish(
    rish: Optional[str] = None,
    jsonrpc: bool = True,
    device_server_port: int = 9008,
    **kwargs: Any,
):
    """Connect to the current Android device through Shizuku/rish.

    Args:
        rish: Path to the rish executable. Defaults to ``$RISH`` or the common
            Termux path ``/data/data/com.termux/files/home/bin/rish``.
        jsonrpc: When true, start the real uiautomator2 JSON-RPC server through
            Shizuku/rish. When false, return the lightweight shell/XML backend.

    Returns:
        Device-compatible object backed by Shizuku/rish.
    """

    rish_path = rish or os.getenv("RISH", DEFAULT_RISH)
    if not jsonrpc:
        return connect_rish_shell(rish_path, **kwargs)

    from uiautomator2 import Device

    class RishJsonRpcDevice(Device):
        def __init__(self):
            self._BaseClient__serial = "rish://"
            self._dev = RishDevice(rish=rish_path, **kwargs)
            self._debug = False
            self._rish_server = RishUiautomatorServer(self._dev, device_server_port=device_server_port)

        @property
        def adb_device(self):
            return self._dev

        @property
        def debug(self) -> bool:
            return self._debug

        @debug.setter
        def debug(self, value: bool) -> None:
            self._debug = bool(value)
            self._rish_server.debug = bool(value)

        def shell(self, cmdargs: Union[str, List[str]], timeout: int = 60) -> ShellResponse:
            del timeout
            return self._dev.shell(cmdargs)

        def start_uiautomator(self) -> None:
            self._rish_server.start_uiautomator()

        def stop_uiautomator(self, wait: bool = True) -> None:
            self._rish_server.stop_uiautomator(wait=wait)

        def jsonrpc_call(self, method: str, params: Any = None, timeout: float = HTTP_TIMEOUT) -> Any:
            return self._rish_server.jsonrpc_call(method, params=params, timeout=timeout)

        def app_current(self) -> Dict[str, str]:
            return self._dev.app_current()

        @property
        def device_info(self) -> Dict[str, Any]:
            sdk = self._dev.getprop("ro.build.version.sdk")
            version = self._dev.getprop("ro.build.version.release")
            return {
                "serial": self._dev.getprop("ro.serialno"),
                "sdk": int(sdk) if sdk.isdigit() else None,
                "brand": self._dev.getprop("ro.product.brand"),
                "model": self._dev.getprop("ro.product.model"),
                "arch": self._dev.getprop("ro.product.cpu.abi"),
                "version": int(version) if version.isdigit() else None,
            }

        @property
        def wlan_ip(self) -> Optional[str]:
            return self._dev.wlan_ip()

    return RishJsonRpcDevice()
