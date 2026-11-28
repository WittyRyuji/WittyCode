# desktop.sandbox.posix — POSIX 后端（Linux bubblewrap / macOS Seatbelt）

`core` 的固定后端契约 `probe()` / `execute()` 的 POSIX 实现。**没有任何普通子进程回退**：平台不支持、可信工具缺失、内核能力自检失败或启动失败都显式抛 `BackendUnavailableError` / `LaunchError` / `PolicyError`。

状态：独立模块，纯标准库；编译器（`_linux_args` / `_seatbelt_profile`）是纯字符串构建，Windows 上即可单测；真实隔离需在原生 Linux/macOS 主机上跑 `NativeIntegrationTests`。

## 可信工具

| 平台 | 工具 | 唯一认可位置 |
| --- | --- | --- |
| Linux | bubblewrap | `/usr/bin/bwrap` |
| macOS | sandbox-exec | `/usr/bin/sandbox-exec` |
| 自检用 | true(1) | `/usr/bin/true` |

不查 `PATH`、不读环境变量找后端。候选必须 realpath 解析后是 **root 拥有的常规可执行文件**，且**每个祖先目录 root 拥有、组/其他不可写**（`st_uid == 0` 且 `st_mode & 022 == 0`）——普通用户能在任一级换掉二进制即判缺失。`/bin` 在 usr-merge 系统上只是符号链接、`/usr/local/bin` 管理员可写，都不入列。

## probe()

不只检查文件存在，而是**用与 execute 同一套命名空间、同一套只读系统运行时和同样的 cwd 策略真跑一次**沙箱命令：

- Linux：`_linux_base()`（`--unshare-user/pid/net/ipc/uts` + `--uid/--gid` + 运行时 binds）+ `--chdir / -- /usr/bin/true`。动态链接的 `true` 需要 `/lib*` 里的 loader——probe 挂的正是 execute 的 `/usr /bin /sbin /lib /lib64` 与 `/etc` 文件白名单，所以自检失败就是 execute 必败。内核不允许 userns/net/pidns 时报 `(False, 具体 stderr)`，**不会先报可用再启动时降级**。
- macOS：`sandbox-exec -p '(version 1)(deny default)(allow …)' /usr/bin/true`。

## Linux（bubblewrap）策略

参数由 `_linux_args` 确定性编译（同输入同输出）：

- `--unshare-user/pid/net/ipc/uts` + `--uid/--gid`（inside uid=真实 uid，文件归属不变，setuid 只得到命名空间内能力）+ `--die-with-parent` + `--new-session`。flag 集合只用 bwrap 0.4.x（Ubuntu 20.04）已有的选项。
- **没有 `--clearenv`**（需要 bwrap ≥0.5）：改为以**全空环境**启动 bwrap 进程本身，子进程 env 恰好只有逐条 `--setenv`（core 的最小白名单：固定 `PATH=/usr/bin:/bin`、`TMPDIR=scratch`）。
- cwd：`--chdir workspace`，子进程在工作区内启动。
- 文件系统视图：**不挂整机、不挂家目录**。`--dev /dev`（最小 dev）、`--proc /proc`、`--tmpfs /tmp`；运行时目录 `/usr /bin /sbin /lib /lib64` 一律 `--ro-bind-try` 只读；**不挂整棵 `/etc`**——只 `--ro-bind-try` 白名单文件（ld.so.cache/conf/conf.d、nsswitch、passwd、group、hosts、localtime）；workspace 默认 `--ro-bind`（`writable` 才 `--bind`）；`readable` 授权 `--ro-bind`；`scratch` `--bind` 可写。父级缺失路径由 bwrap 在新根里创建空目录，宿主 `/home`、`/Users` 等整棵树不会进沙箱。
- **protected（deny > 写，也不可读）**：掩蔽物在宿主侧、scratch 的**兄弟目录** `<scratch>-posix-mask/`（目录掩蔽用该空目录本身；文件掩蔽用其中的空常规文件 `empty`），以 `--ro-bind` 遮住 protected——读得空/EOF、写得 EROFS。掩蔽物**不放在 scratch 里**：scratch 可写，子进程能往其中放文件、让被遮目录重新"有内容"。兄弟位置只经只读掩蔽 bind 暴露，guest 写不到；launch 后宿主侧删除——只删本次自己创建的目录（已存在的同名路径让 `mkdir` 直接失败，绝不清理既有目录；`FileNotFoundError` 之外的删除错误会显式传播，不吞掉）。**不用 tmpfs**（可写）。被已遮罩父目录覆盖的子项自动跳过。
- 掩蔽是**路径级**的：workspace 内指向被保护路径的符号链接会随目标一起被遮；但同一文件的**另一个硬链接路径**看不到遮罩——本模块不承诺 inode 级保护。
- 工具 `argv[0]` 不在任何已挂接树下时补 `--ro-bind tool tool`（只能保证文件本身可见；需要同目录资源的工具要用 `readable` 授权其安装目录）。
- 网络：新建空 netns，连 loopback 都没有 → 默认且不可关闭地断网，宿主 127.0.0.1 服务不可达。
- 父进程 FD：`subprocess.run` 默认 `close_fds=True`，stdin 固定 `DEVNULL`，只有捕获管道；bwrap 再 unshare IPC。
- 进程回收：pid 命名空间 init 退出即全树 SIGKILL + `--die-with-parent`，Linux 上是真回收保障。
- 超时：`subprocess.run(timeout)` 到点杀 bwrap → 整树随 pidns 消亡，`TimeoutExpired` 透传给 core（CLI 退出码 124）。

## macOS（Seatbelt）策略

参数由 `_seatbelt_profile` 确定性编译：

- `(deny default)` + `(deny network*)`：所有未允许操作默认拒绝，**一切网络操作（含 loopback）恒拒绝**。
- 允许集刻意收窄：`process-fork`（**子进程自动继承同一 profile**）、`process-exec`、`signal (target self)`；`file-read* file-map-executable` 只给 `/System /usr /bin /sbin` + workspace + scratch + `readable` 授权 + 工具文件 literal；`/dev` 不整树开放，只 literal `/dev/null|zero|full|random|urandom` 读 + `null|zero|full` 写；`file-write*` 只给 scratch（`writable` 时加 workspace）。不放开 mach-lookup / sysctl / 任意目标 signal；不整树开放 `/Library`、`/private/etc`。
- **protected**：在全部 allow 之后追加 `(deny file-read* file-map-executable file-write* (subpath …))`——Seatbelt 是 last-match-wins，靠顺序保证 deny 压过 workspace 的读写允许。此语义依赖由原生集成测试实证。
- cwd：`Popen(cwd=workspace)`，与 Linux 行为一致。
- 超时与回收：子进程 `start_new_session` 自成进程组，超时 `killpg(SIGKILL)`，随后给管道最多 `_KILL_DRAIN` 秒排干，拿不到输出也带上已捕获部分抛 `TimeoutExpired`。**已知边界**：后代若自己 `setsid`/起新进程组可逃出 killpg，**macOS 无法保证完整回收**；这些逃逸后代仍被 Seatbelt profile 约束，只可能留下资源残留。本模块的测试不创建这种逃逸进程，也不声称该保障已验证。
- `sandbox-exec` 是 Apple 已弃用接口但仍为系统自带工具；未来移除时 `probe` 会显式报不可用。

## 明确拒绝而非降级

- 非 linux/darwin 的 POSIX 平台（FreeBSD 等）：`probe` 报不支持，`execute` 抛 `BackendUnavailableError`。
- 工具位于 protected 遮罩之下：`execute` 抛 `PolicyError`（启动必然失败，提前说清）。
- 内核缺 user/net 命名空间能力：`probe` 自检失败，报具体 stderr，不静默退化为不隔离。
- 掩蔽目录无法创建（scratch 兄弟位置不可写）：`OSError` → core 包成 `LaunchError`。

## 验证

托管 runner（仓库根）：

```powershell
python desktop/core/test_runs.py -- python -m desktop.sandbox.test_posix -v
```

- `LinuxCompileTests` / `SeatbeltCompileTests`：确定性编译、cwd、deny>写、无整机/家目录/宽系统树挂接、无网络 allow、无 `--clearenv`、掩蔽物宿主侧不可写、路径转义等——**任何主机可跑（含 Windows）**。
- `ProbeTests` / `ExecutePlumbingTests`：probe 分派与失败语义、probe 与 execute 的运行时一致性、可信工具祖先可写性检查、启动 plumbing（mock 子进程）、掩蔽目录生命周期——任何主机可跑。
- `NativeIntegrationTests`：同一套用例实测 Linux bwrap 与 macOS Seatbelt——cwd 为 workspace、可写/只读 workspace、protected 文件/目录不可读写且不泄漏、readable 只读、scratch 经 `TMPDIR` 可写（`PATH` 固定 `/usr/bin:/bin`）、env 隔离、**子进程继承限制**、**loopback 断网**（测试自己在宿主起监听、同一解释器先在宿主确认可达，再断言沙箱内连接被拒且客户端确实启动过）、超时回收。仅当 `sys.platform` 为 linux/darwin 且 `probe()` 通过才运行，否则整类 SKIPPED（不算已验证）；设置 `WITTYCODE_SANDBOX_NATIVE=1` 做显式原生验收时，probe 失败或平台不符会硬失败而不是跳过。

本机（Windows）实测：单测 41 通过、原生用例整类 SKIPPED；`WITTYCODE_SANDBOX_NATIVE=1` 下验收模式如期硬失败。Rocky9/Ubuntu20+/macOS 上的原生结果须在对应主机跑出后再记录为已验证。
