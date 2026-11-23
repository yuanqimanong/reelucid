"""Offline build check using the same executable and protocol as a real job."""

import json
import subprocess

url = "https://www.xiaohongshu.com/explore/probe"
note = {
    "noteId": "probe",
    "title": "Fixture",
    "type": "video",
    "imageList": [{}],
    "video": {"consumer": {"originVideoKey": "v/test.mp4"}},
}
state = {"note": {"noteDetailMap": {"probe": {"note": note}}}}
html = f"<html><script>window.__INITIAL_STATE__={json.dumps(state)}</script></html>"
process = subprocess.run(
    ["/opt/reelucid-xhs/resolve"],
    input=json.dumps({"url": url, "html": html}).encode(),
    capture_output=True,
    timeout=60,
    check=True,
)
assert not process.stderr
assert json.loads(process.stdout) == {
    "data": {
        "作品ID": "probe",
        "作品标题": "Fixture",
        "作品类型": "视频",
        "下载地址": ["https://sns-video-bd.xhscdn.com/v/test.mp4"],
    }
}
