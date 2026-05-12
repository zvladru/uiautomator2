from uiautomator2.abstract import ShellResponse
from uiautomator2.rish import RishDevice


SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="OK" resource-id="android:id/button1" class="android.widget.Button"
        package="android" content-desc="" checkable="false" checked="false"
        clickable="true" enabled="true" focusable="true" focused="false"
        scrollable="false" selected="false" bounds="[100,200][300,400]" />
  <node index="1" text="Throughput test (128 bytes)"
        resource-id="android:id/text1" class="android.widget.CheckedTextView"
        package="android" content-desc="" checkable="true" checked="false"
        clickable="true" enabled="true" focusable="true" focused="false"
        scrollable="false" selected="false" bounds="[0,400][500,500]" />
</hierarchy>
"""


class FakeRunner:
    def __init__(self):
        self.commands = []

    def run(self, cmdargs, check=False):
        del check
        command = cmdargs if isinstance(cmdargs, str) else " ".join(cmdargs)
        self.commands.append(command)
        if command == "echo rish-ok":
            return ShellResponse("rish-ok\n", 0)
        if "uiautomator dump" in command:
            return ShellResponse(SAMPLE_XML, 0)
        if command == "wm size":
            return ShellResponse("Physical size: 1080x2340\n", 0)
        return ShellResponse("", 0)


def test_rish_selector_exists_info_and_click():
    runner = FakeRunner()
    device = RishDevice(rish="/data/data/com.termux/files/usr/bin/sh", runner=runner)

    ok = device(text="OK")
    assert ok.exists
    assert ok.info["resourceName"] == "android:id/button1"
    assert ok.center() == (200, 300)

    ok.click()
    assert runner.commands[-1] == "input tap 200 300"


def test_rish_selector_contains_and_resource_id():
    runner = FakeRunner()
    device = RishDevice(rish="/data/data/com.termux/files/usr/bin/sh", runner=runner)

    assert device(textContains="Throughput").exists
    assert device(resourceId="android:id/text1").info["text"] == "Throughput test (128 bytes)"
