# desktop.sandbox — 独立 OS 沙箱公共层

在操作系统沙箱里跑**一条**命令：工作区默认只读、环境最小白名单、网络固定关闭。

**状态**：独立模块，只用 Python 3.11 标准库，不依赖 Qt、Harness 或任何模型；尚未接入主程序或代理流程，现有发布 copytree 即使带上这些文件也不会运行它们。可在仓库根目录 `import desktop.sandbox`，或 `python -m desktop.sandbox` 调用。

## 组成

| 文件 | 职责 |
| --- | --- |
| `core.py` | 平台后端选择、统一策略校验、最小环境构建、单次 scratch 的创建与清理 |
| `__main__.py` | CLI：解析参数 → `core.run` → 透传子进程输出和退出码 |
| `__init__.py` | 公共 API 导出（不触发后端导入） |
| `windows.py` | Windows 后端（见下） |
| `posix.py` + `posix.md` | POSIX 后端与安全模型 |
| `mxc.mts` | Windows 后端的 Node 助手（直连官方 SDK） |
| `test_core.py` / `test_posix.py` / `test_windows.py` | 各层单测 |

后端固定契约（`core` 懒加载，绝不回退为普通进程）：

```python
def probe() -> tuple[bool, str]: ...
def execute(
    workspace: Path, argv: tuple[str, ...], *,
    writable: bool, protected: tuple[Path, ...], readable: tuple[Path, ...],
    scratch: Path, env: dict[str, str], timeout: float,
) -> subprocess.CompletedProcess[bytes]: ...
```

后端缺失、probe 失败或启动失败都显式报错（`BackendUnavailableError` / `LaunchError` / `PolicyError`）。

## 三个后端与可信宿主定义

**可信宿主**：后端只从固定系统路径或显式环境变量指向的位置加载执行器——不查 `PATH`、不读工作区、不继承调用方的工具链。POSIX 还要求候选是 **root 拥有的常规可执行文件**（`st_uid == 0`）。

| 平台 | 机制 | 可信依赖 |
| --- | --- | --- |
| Windows 11 | Microsoft MXC `processcontainer`（`windows.py`） | 官方 `@microsoft/mxc-sdk`（校验完整性的解压包，不跑安装脚本）+ `mxc.mts` 助手跑在 **Node ≥ 24** 上。解析顺序：`WITTYCODE_MXC_NODE`/`WITTYCODE_MXC_SDK` 显式指定 → `%ProgramData%\WittyCode\mxc\nodejs` + `sdk\` → `%LOCALAPPDATA%\WittyCode\mxc\...` |
| Linux | bubblewrap 命名空间（`posix.py`） | 固定路径 `/usr/bin/bwrap`、`/bin/bwrap`、`/usr/local/bin/bwrap` |
| macOS | Seatbelt `sandbox-exec`（`posix.py`） | 固定路径 `/usr/bin/sandbox-exec` |

细节见各后端模块 docstring 与 `posix.md`。这些 native/SDK 依赖由后端 `probe()` 真发自检（不是只查文件存在），不可用时明确报告；公共层不安装、不假定组件。**本机（Windows）上 POSIX 原生用例一律 SKIPPED，Windows MXC 与 macOS/Linux 的端到端结果只有在对应主机跑出后才算已验证。**

## 用法

```powershell
# 探测本平台后端是否可用
python -m desktop.sandbox --probe

# 工作区只读（默认），-- 之后是命令
python -m desktop.sandbox --workspace C:\Users\me\project --tool C:\tools\run.exe -- arg1

# 允许写工作区，但 secrets.txt 拒读拒写；额外授予运行时目录只读
python -m desktop.sandbox --workspace C:\Users\me\project --write `
    --protect C:\Users\me\project\secrets.txt `
    --read D:\tools\runtime -- C:\tools\run.exe arg
```

API 等价形式：

```python
from pathlib import Path
from desktop.sandbox import run

result = run(
    (r"C:\tools\run.exe", "arg1"),
    workspace=Path(r"C:\Users\me\project"),
    writable=False,
    protected=(),
    readable=(Path(r"D:\tools\runtime"),),
    env={"APP_MODE": "batch"},
    timeout=60.0,
)
```

## 策略规则（`validate_policy` 强制，fail closed）

- **workspace**：现存的私有目录，resolve 后参与一切判断。禁止：文件系统根、主目录本身、系统/安装目录（`C:\Windows`、`Program Files`、`/usr`、`/etc` 等，含其内部）、凭据目录（`~/.ssh`、`~/.gnupg`、`~/.aws` 等，含其内部）、以及任何会覆盖上述位置的目录。POSIX 的 `/private` 不整体拒绝——macOS 的每用户临时目录就在其下，受保护的是解析后的真实系统根（`/etc→/private/etc` 等）。默认只读，`--write` / `writable=True` 才放行写；`writable` 必须是 `bool`；含本沙箱模块自身的工作区不可授予写。
- **protected**：必须现存且严格位于工作区内部，且必须是普通文件或目录（拒绝设备、FIFO 等特殊对象）。**语义是拒读也拒写**（掩蔽/deny），不只是只读；不得覆盖 `argv[0]`（工具被遮住必然启动失败）。
- **readable**：显式追加的可信只读路径，同样必须是普通文件或目录。不得是文件系统根、主目录、凭据/系统目录（含双向重叠），不得是整个安装根/共享根本身（其具体子目录可以，如 `D:\tools\runtime`），不得与工作区或另一条授权重叠。
- **argv**：非空、每项非空且无 NUL；`argv[0]` 必须是现存普通文件的绝对路径，推荐 `--tool` 显式指定；`--write` 时不得位于工作区内。
- **env**：从空白构建——不继承宿主环境。`PATH` 是固定系统工具目录（Windows `%SystemRoot%\System32`，POSIX `/usr/bin:/bin`），`TEMP`/`TMP`（POSIX 为 `TMPDIR`）指向本次 scratch。调用方可加白名单变量：拒绝保留名（`PATH` 等）、凭据/令牌名、`*_PROXY`、`PYTHON*`、`VIRTUAL_ENV`、`CONDA_*`、`LD_*`、`DYLD_*`、`BASH_ENV`。
- **scratch**：每次运行 `mkdtemp` 新建、结束即删；删除前核对创建时记录的 inode/文件索引，路径被替换则拒绝删除。根目录默认 `WITTYCODE_TEST_WORK`（托管测试运行器），否则系统临时目录；scratch 不得位于工作区或任何只读授权内。
- **timeout**：正的有限秒数；超时以 `subprocess.TimeoutExpired` 抛出（CLI 退出码 124）。
- **网络**：固定关闭，本层不提供任何开关，也不继承代理变量。

## 路径策略的边界（不承诺）

这是**路径**策略：工作区内已有的硬链接（同一 inode 的另一条路径在工作区外）以及运行期间宿主机上其他进程的并发改写，都不在保护范围内；不承诺内容级或 inode 级保密。`protected` 保证的是沙箱内该路径不可读写，不是该文件内容在其他入口下不可见。

## 验证

托管测试运行器（仓库根目录、项目 venv）：

```powershell
python desktop/core/test_runs.py -- python -m desktop.sandbox.test_core -v
python desktop/core/test_runs.py -- python -m desktop.sandbox.test_posix -v
python desktop/core/test_runs.py -- python -m desktop.sandbox.test_windows -v
```

`test_core.py`/`test_posix.py` 的编译与 plumbing 用例任何主机可跑（`test_core` 也可 `python -m desktop.sandbox.test_core` 直跑）；POSIX 原生集成用例只在真实 Linux/macOS 主机且 `probe()` 通过时执行，否则 SKIPPED；`test_windows` 会下载并校验 `@microsoft/mxc-sdk`、定位 Node ≥ 24，全在 `WITTYCODE_TEST_WORK` 内。CLI 冒烟：`python -m desktop.sandbox --probe`。

## 限制

- 不默认开网，也没有开网开关；需要网络的命令不要走本模块。
- macOS Seatbelt 的进程回收有已知限制（`setsid`/新进程组可逃出 killpg，见 `posix.md`）。
- 后端在本机不可用或未经验证时，`--probe` 如实报告不可用——不降级、不声称隔离。
