import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_SANDBOX = Path("/usr/bin/sandbox-exec")
_PROFILE = """(version 1)
(deny default)
(allow process-exec (literal (param "PYTHON")) (literal (param "APP_PYTHON")))
(allow file-read-metadata)
(allow file-read* file-map-executable (literal "/") (subpath (param "PYHOME"))
    (subpath "/System/Library") (subpath "/usr/lib") (literal "/dev/null") (literal "/dev/urandom"))
(allow sysctl-read)
"""


@dataclass(frozen=True)
class Execution:
    status: str
    output: dict | None = None
    feedback: str = ""


class PythonSandbox:
    @property
    def available(self) -> bool:
        return sys.platform == "darwin" and _SANDBOX.is_file()

    def run(self, code: str, rows: list[dict], plan: dict) -> Execution:
        if not self.available:
            return Execution("unavailable", feedback="当前机器没有可用的 macOS 沙箱运行器。")
        if not code.strip() or len(code) > 6000:
            return Execution("execution_error", feedback="Return one non-empty analysis function within 6000 characters.")
        program = Path(__file__).with_name("ds_sandbox_worker.py").read_text(encoding="utf-8")
        python = Path(sys.executable).resolve()
        base = Path(sys.base_prefix).resolve()
        python_app = base / "Resources/Python.app/Contents/MacOS/Python"
        args = [str(_SANDBOX), "-D", f"PYTHON={python}", "-D", f"APP_PYTHON={python_app}",
                "-D", f"PYHOME={base}", "-p", _PROFILE, str(python), "-I", "-B", "-S", "-c", program]
        request = json.dumps({"code": code, "rows": rows, "plan": plan}, ensure_ascii=True, allow_nan=False)
        if len(rows) > 256 or len(request) >= 256 * 1024:
            return Execution("unavailable", feedback="本次数据窗口超出沙箱输入上限。")
        try:
            reply = subprocess.run(args, input=request, text=True, capture_output=True, timeout=5,
                                   env={"LANG": "en_US.UTF-8"}, cwd="/")
        except subprocess.TimeoutExpired:
            return Execution("execution_error", feedback="Analysis exceeded the wall-clock time limit.")
        except OSError:
            return Execution("unavailable", feedback="无法启动 macOS 沙箱运行器。")
        if reply.returncode != 0:
            return Execution("unavailable", feedback=f"沙箱进程退出，状态 {reply.returncode}。")
        try:
            result = json.loads(reply.stdout)
        except ValueError:
            return Execution("unavailable", feedback="沙箱未返回可解析的响应。")
        if "error" in result:
            return Execution("execution_error", feedback=result["error"])
        if not isinstance(result.get("result"), dict):
            return Execution("result_error", feedback="analysis() must return a result dictionary.")
        return Execution("ok", result["result"])
