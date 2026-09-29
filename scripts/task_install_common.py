r"""Windows 计划任务安装的公共部分 (2026-09-29)。

抽出来的原因: 现在有两个任务要装 (SJBot_Console_Guard / SJBot_Daily_Prep),
而"把任务装对"这件事有三个必须踩准的坑, 不该在两个安装器里各抄一遍:

1. **中文路径 ⇒ 必须走 XML**
   schtasks 的 -tr/-sd 参数按控制台代码页(cp936)解释, 项目路径含中文会被按 ANSI
   误解。把路径写进 XML、以 UTF-16 落盘, 再交给 schtasks /xml 读, 中文无损。

2. **输出解码必须显式 mbcs**
   本环境设了 PYTHONUTF8=1, 于是 locale.getpreferredencoding(False) 返回 'utf-8',
   而 schtasks 的输出是按控制台 ANSI 代码页(cp936)输出的 —— 用 utf-8 解会把
   "成功: 成功创建计划任务" 显示成乱码, **等于把错误信息吃掉** (实测过)。

3. **两条注册通道, 互为兜底**
   首选 schtasks.exe; 若它不可用 (例如被安全策略黑名单拦截 ⇒ `WinError 5 拒绝访问`),
   退回 PowerShell 的 `Register-ScheduledTask -Xml`。两者吃的是**同一份 XML**,
   只是入口不同, 所以任务定义完全一致, 不会出现"两条路装出两种行为"。
"""
from __future__ import annotations

import locale
import subprocess
import tempfile
from pathlib import Path


def run(*args: str) -> subprocess.CompletedProcess:
    """调用外部命令, 输出按 mbcs 优先解码 (见模块 docstring 第 2 条)。

    必须捕获 OSError: 当可执行文件**根本没被启动**时 (安全策略黑名单 / 程序不存在),
    subprocess 抛的是 PermissionError/FileNotFoundError **异常**而不是返回非零码。
    不接住它, "schtasks 失败 -> 改走 PowerShell" 这条兜底就永远够不到 —— 实测踩过。
    """
    try:
        p = subprocess.run(list(args), capture_output=True)
    except OSError as e:
        return subprocess.CompletedProcess(list(args), 127, "",
                                           f"{type(e).__name__}: {e}")

    def dec(b: bytes) -> str:
        for enc in ("mbcs", locale.getpreferredencoding(False)):
            try:
                return b.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return b.decode("utf-8", errors="replace")

    return subprocess.CompletedProcess(p.args, p.returncode, dec(p.stdout), dec(p.stderr))


def write_xml(task_name: str, xml_text: str) -> Path:
    """XML 落到临时目录 (UTF-16 + BOM, schtasks 认这个)。"""
    p = Path(tempfile.gettempdir()) / f"{task_name}.xml"
    p.write_text(xml_text, encoding="utf-16")
    return p


def _register_via_powershell(task_name: str, xml_path: Path) -> tuple[bool, str]:
    """兜底通道: Register-ScheduledTask -Xml。走 CIM, 不落 schtasks.exe。"""
    ps = (
        "$ErrorActionPreference='Stop';"
        f"$x = Get-Content -LiteralPath '{xml_path}' -Raw -Encoding Unicode;"
        f"Register-ScheduledTask -TaskName '{task_name}' -Xml $x -Force | Out-Null;"
        "Write-Output 'REGISTERED_OK'"
    )
    r = run("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
    out = (r.stdout + r.stderr).strip()
    return ("REGISTERED_OK" in r.stdout), out


def register_task(task_name: str, xml_text: str) -> tuple[bool, str]:
    """装(或覆盖)任务。返回 (成功?, 面向人的说明)。"""
    xml_path = write_xml(task_name, xml_text)
    r = run("schtasks", "/create", "/tn", task_name, "/xml", str(xml_path), "/f")
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        return True, f"schtasks 注册成功: {out}"

    low = out.lower()
    blocked = ("拒绝访问" in out or "winerror 5" in low or "permission" in low
               or "access is denied" in low)
    why = "schtasks 被拦截" if blocked else f"schtasks 失败(rc={r.returncode})"
    ok, ps_out = _register_via_powershell(task_name, xml_path)
    if ok:
        return True, (f"{why} -> 已改走 PowerShell Register-ScheduledTask 注册成功\n"
                      f"  (原始输出: {out[:200]})")
    return False, f"{why}, PowerShell 兜底亦失败:\n  schtasks: {out[:300]}\n  powershell: {ps_out[:300]}"


def query_task(task_name: str) -> tuple[bool, str]:
    r = run("schtasks", "/query", "/tn", task_name, "/xml")
    out = (r.stdout or r.stderr).strip()
    if r.returncode == 0:
        return True, out
    # schtasks 不可用时退回 PowerShell 读任务定义
    ps = (f"$t = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop;"
          "$t | Select-Object -ExpandProperty Xml | Write-Output")
    r2 = run("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
    if r2.returncode == 0 and r2.stdout.strip():
        return True, r2.stdout.strip()
    return False, out


def remove_task(task_name: str) -> tuple[bool, str]:
    r = run("schtasks", "/delete", "/tn", task_name, "/f")
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        return True, out
    ps = (f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false;"
          "Write-Output 'REMOVED_OK'")
    r2 = run("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
    if "REMOVED_OK" in r2.stdout:
        return True, "已通过 PowerShell 卸载"
    return False, out


def verify_next_run(task_name: str) -> str:
    """给一句"装好了没"的结论 —— 历史事故就是 NextRun 为空才暴露的, 装完必看。"""
    ps = (
        f"$i = Get-ScheduledTaskInfo -TaskName '{task_name}' -ErrorAction Stop;"
        "$t = Get-ScheduledTask -TaskName '" + task_name + "' -ErrorAction Stop;"
        '"State=" + $t.State + " NextRun=" + $i.NextRunTime + " EndBoundary=" + $t.Triggers[0].EndBoundary'
    )
    r = run("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
    return (r.stdout + r.stderr).strip()
