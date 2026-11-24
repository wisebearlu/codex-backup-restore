# Codex Backup & Restore

[中文](#中文) · [English](#english)

## 中文

### Codex 备份与恢复

在两台 Windows 电脑之间离线迁移 Codex 本地项目和侧栏 Section 中的聊天，也可以在本机修复被清空的侧栏登记。脚本将分页历史、归档记录、项目归属、Section 名称和样式，以及聊天的组内顺序一起写入 ZIP。项目代码可以继续通过 Git 同步。

迁移包包含会话正文、工具输出和原始路径等敏感数据。请私下传输和保存，并只导入可信的 ZIP。包里不包含项目文件、登录凭据、浏览器数据、应用配置或插件。

### 运行条件

- 两台电脑均使用 Windows 和 Python 3.14 或更新版本。`compression.zstd` 随 Python 提供。
- 两台电脑的 Codex 数据库结构需要兼容。尽量使用相同版本的 Codex。
- 正式迁移前，完全退出 ChatGPT/Codex 桌面端、CLI 和 IDE 中的 Codex 会话。准备足够空间存放 ZIP、临时文件和导入备份。

以下命令在 PowerShell 和 Git Bash 中都可用。若 `python` 指向旧版本，可以换成 `py -3.14`。

### 桌面窗口

在资源管理器双击 `Start-Codex-Backup.cmd`。启动文件和 `codex-backup-restore.py` 放在同一个目录。也可以从终端启动：

```text
python codex-backup-restore.py
```

窗口提供导出、导入、清理、修复四个入口，可选择 Codex 数据目录和 ZIP，并查看运行记录。Skills 默认勾选。修复和清理不使用 ZIP。耗时操作在后台运行，任务结束后可继续操作。

清理和修复都要先点「预览」，核对列表后再执行并确认。如果操作需要退出 Codex，窗口会列出正在运行的相关进程并询问是否结束它们。选「是」会强制结束这些进程并继续，进行中的任务可能中断。选「否」会取消本次操作。窗口使用 Python 自带的 Tkinter。

### 导出

在旧电脑上进入脚本所在目录，完全退出 Codex，再运行：

```text
python codex-backup-restore.py export
```

默认生成脚本旁的 `backups/codex-chats.zip`。可显式指定其他 ZIP 路径。脚本会打印识别到的项目、项目根目录、会话及历史段数量。如果发现需要的历史段缺失，导出会停止。默认 ZIP 已存在时，请改用新的文件名。

```text
python codex-backup-restore.py export codex-chats-new.zip
```

### 导入

1. 把脚本和 ZIP 复制到新电脑。ZIP 可以直接读取。
2. 用新电脑自己的账号登录 Codex，初始化本地数据后完全退出。
3. 在脚本所在目录运行以下命令。使用自定义文件名时，在 `import` 后加上 ZIP 路径。

```text
python codex-backup-restore.py import
```

脚本会在脚本旁的 `backups/conversation-transfer-backup-时间/` 中保存目标数据库、会话索引和侧栏登记，然后合并会话及项目归属。省略 ZIP 路径时，优先读取 `backups/codex-chats.zip`，也兼容旧版直接放在脚本旁的 ZIP。缺失的项目目录会按旧机的绝对路径创建为空目录。如果路径所在的盘符不可用或没有写权限，导入会停止。迁移保留原工作路径，建议把 Git 仓库放在两台机器相同的绝对路径上。

完成后打开 Codex，检查侧栏项目，打开一条旧会话，确认能翻到前文并继续发送消息。

### 重复导入

可以用新版脚本和同一个 ZIP 重跑，例如补齐旧版本遗漏的侧栏登记。重复导入会复用一致的会话文件和已有分页索引。输出 `0 new indexed chats` 表示这一轮没有新增聊天。另看 `project registrations` 和 `chat memberships` 的计数。

同一 Section ID 会复用目标分组。重跑会保留新机已经调整的名称、归属和顺序。旧版生成的 ZIP 仍可导入。要迁移 Section，需要用新版脚本重新导出。

重复导入会比较解压后的历史。如果一方的历史是另一方的完整字节前缀，就保留较长的一方。如果两边历史分叉，默认用 ZIP 覆盖目标版本，并同步对应的分页历史和索引。被替换的会话文件保存在本次导入备份的 `rollouts` 子目录。如果目标机独有的会话或分页依赖待覆盖的历史，导入会保留这组历史，并继续导入其余内容。

可先用 `import --preview` 查看待更新和覆盖的文件。分叉时要保留本机版本，在窗口勾选「历史分叉时保留本机版本」，或使用以下命令。普通增量更新仍会执行。

```text
python codex-backup-restore.py import --keep-existing
```

### 全局 AGENTS.md 备份与恢复

导出默认包含 Codex 数据目录下的 `AGENTS.md` 和 `AGENTS.override.md`（存在时）。导入会将文件恢复到目标机的 Codex 数据目录；使用 `--codex-home` 时以指定目录为准。`--exclude-skills` 只跳过 Skills，全局规则仍会导出和导入。

导出会跟随全局规则中的 Markdown 文件引用，包括被引用文档里的后续引用。只收集 Codex 数据目录内的实际文件，并保留目录结构。支持相对路径和绝对路径。含空格的路径用反引号、引号或 Markdown 链接的尖括号括起来。导入时，随包恢复的文档中指向包内文档的绝对路径会自动改成目标机路径。显式引用的 Markdown 文件属于全局规则备份，即使用了 `--exclude-skills` 也会迁移。

目标机缺少文件时自动恢复，内容相同时跳过，内容不同时提示 `Global instruction conflict, kept destination` 并保留目标机版本。预览会显示待恢复数量及冲突。旧 ZIP 仍可导入。要带上引用文件，请重新导出。项目内的 `AGENTS.md` 随 Git 同步。

发布名称为 **Codex Backup & Restore**，主脚本已改名为 `codex-backup-restore.py`，命令参数沿用原用法。全局规则、会话和 Skills 可能包含私人信息，请私下保管备份包。

### Skills 迁移

导出和导入默认都包含用户 Skills。`--include-skills` 仍可显式指定。只想迁移聊天和分组时，在对应命令加 `--exclude-skills`：

```text
python codex-backup-restore.py export codex-chats-only.zip --exclude-skills
python codex-backup-restore.py import codex-chats-only.zip --exclude-skills
```

脚本会处理 Codex 数据目录下的 `skills` 和用户目录下的 `.agents/skills`，只打包包含 `SKILL.md` 的用户技能目录及其支持文件。仓库内 Skills 继续随 Git 同步，内置 `.system` 目录会跳过。导入发现同名内容不同时会保留新机版本并提示冲突。Skills 可以包含可执行脚本或私人配置，请先检查来源和内容。

### 清理旧备份和临时文件

先预览，再完全退出 Codex 并执行删除：

```text
python codex-backup-restore.py cleanup
python codex-backup-restore.py cleanup --apply
```

清理范围包括脚本旁 `backups` 和 Codex 数据目录中超过 7 天的导入备份，以及过期的 Codex 全局状态写入临时文件。每个位置最新一份导入备份会保留。含被覆盖会话文件的备份也会保留。目录或其内部文件最近 7 天有变动时会跳过，含未知文件或链接的备份目录也会跳过。删除的旧备份会失去对应的回退能力。

会话、正式数据库、Skills、配置、浏览器数据、导出的 ZIP 和其他工具的备份会保留。`cleanup` 默认只打印路径和可释放大小，`--apply` 才会永久删除列出的项。

「清理旧文件」默认勾选 Codex 应用和插件缓存。预览和结果先显示总量，点「查看明细」可展开文件清单和缓存小计。脚本只匹配超过 7 天的应用目录、连接器工具与服务信息、远程插件目录 JSON 缓存。Codex 下次使用这些功能时可能重新下载目录数据。命令行通过 `--include-cache` 纳入缓存：

```text
python codex-backup-restore.py cleanup --include-cache
python codex-backup-restore.py cleanup --include-cache --apply
```

### 修复被清空的侧栏

Codex 异常退出后，侧栏里的项目、置顶和 Section 成员可能变成空列表，而 `state_5.sqlite` 里的项目和会话归属还在。`repair` 根据数据库和本机旧的全局状态文件，把这些侧栏登记填回去。

```text
python codex-backup-restore.py repair
python codex-backup-restore.py repair --apply
```

先预览。核对输出后，完全退出 Codex，再加 `--apply`。脚本会把当前 `.codex-global-state.json` 备份到脚本旁的 `backups/sidebar-repair-backup-时间.json`，然后写回侧栏。窗口里的「修复侧栏」也要先预览，再确认后写入。

项目登记使用数据库里的当前项目 id。侧栏如果还保存着迁移前的旧 id，脚本保持这些旧 id，不把它们改成当前 id。当前 id 和旧 id 映射同时存在时，脚本会去掉这份旧映射，避免 Codex 下次启动时把同一项目再导入一次。

Section 成员只从本机已有的全局状态备份中填回当前分组，分组本身的 id、名称和顺序保持不变。备份里已经不存在的项目会跳过。备份当时没有分组记录的项目留在 Projects。已经有成员的分组、已经登记的项目和已有的会话归属保持原样。

`repair` 不读取 ZIP，也不改会话正文、数据库、Skills、远程主机或项目目录。

### 帮助和预览

```text
python codex-backup-restore.py --help
python codex-backup-restore.py export --preview
python codex-backup-restore.py import --preview
```

导入预览会读取 ZIP 并检查目标状态，列出将创建的目录。正式导入时才写入项目目录和 Codex 数据。使用自定义 Codex 数据目录时，可以传入 `--codex-home`。

## English

Transfer local Codex project and sidebar Section conversations between Windows computers, and repair a wiped sidebar on the same computer. The ZIP includes archived sessions, paginated history, project assignments, and Section names, styles, chat memberships, and chat order within each Section. Keep syncing your source code through Git.

### Requirements

- Windows and Python 3.14 or newer on both computers. The `compression.zstd` module is part of Python.
- Compatible Codex database schemas. Using the same Codex version on both computers is a good starting point.
- Quit Codex Desktop, the CLI, and any Codex IDE sessions before transferring. Allow space for the archive, temporary files, and destination backups.

The commands below work in PowerShell and Git Bash. Use `py -3.14` if `python` points to an older version.

### Desktop window

Double-click `Start-Codex-Backup.cmd` in File Explorer. Keep it beside `codex-backup-restore.py`. You can also start it from a terminal:

```text
python codex-backup-restore.py
```

The Chinese-language desktop window provides export, import, cleanup, and repair controls, directory and ZIP pickers, a Skills checkbox enabled by default, and a live activity log. Repair and cleanup do not use a ZIP. Operations run in the background while the controls are locked.

For cleanup or repair, preview first, then run the action and confirm. If the operation requires Codex to exit, the window lists the running processes and asks whether to close them. Choose Yes to force them to exit and continue, which may interrupt ongoing tasks. Choose No to cancel. The window uses Python's bundled Tkinter.

### Export

On the source computer, open a terminal in the script directory and run:

```text
python codex-backup-restore.py export
```

The default archive is `backups/codex-chats.zip` under the script directory. Pass a different ZIP filename if the default already exists. Export stops if it detects missing history required by a selected conversation.

### Import

Copy the script and ZIP to the destination computer. Sign in to Codex with the destination account once, then quit Codex completely. From the script directory, run:

```text
python codex-backup-restore.py import
```

The script reads the ZIP directly, saves destination databases, indexes, and sidebar state under `backups/conversation-transfer-backup-<timestamp>/` beside the script, and merges conversations and project assignments. With no ZIP argument, import looks under `backups` first, then beside the script for an older archive. It creates empty directories for missing project roots at their original absolute paths. Put your Git repositories at the same paths to keep the restored chats' working directories usable. Open Codex afterward and verify that an old conversation shows its earlier messages and accepts a new one.

### Repeat an import

On repeat import, the script compares decompressed history. If one history is an exact byte prefix of the other, it keeps the longer version. Divergent histories default to the ZIP version. The importer updates the associated paginated history and index and saves replaced conversation files under `rollouts` in the import backup. If destination-only history depends on a file that would be overwritten, the importer keeps that group and continues with the remaining data.

Use `import --preview` to see proposed updates and replacements. To keep the destination version when history diverges, select the corresponding checkbox in the window or run the command below. Ordinary incremental updates still apply.

```text
python codex-backup-restore.py import --keep-existing
```

Repeat imports reuse Sections by ID and preserve destination names, assignments, and chat order. Older ZIPs still import. To transfer Sections, export a new ZIP with the updated script.

### Back up and restore global instructions

Export includes `AGENTS.md` and `AGENTS.override.md` from the Codex data directory when present. Import restores them to the destination Codex data directory, including a custom `--codex-home`. The `--exclude-skills` option only skips Skills; global instructions still transfer.

Export follows Markdown file references in the global instructions and in the referenced documents. It collects regular files within the Codex data directory and preserves their folder structure. Relative and absolute paths are supported. Enclose paths containing spaces in backticks, quotes, or angle brackets within Markdown links. On import, absolute paths between restored documents are updated for the destination computer. Explicitly referenced Markdown files are part of the global instructions backup even with `--exclude-skills`.

Missing destination files are restored. Identical files are skipped. When contents differ, the importer reports `Global instruction conflict, kept destination` and keeps the destination version. Preview reports additions and conflicts. Older ZIPs remain supported. Export a fresh ZIP to include referenced files. Repository-level `AGENTS.md` files travel through Git.

The project is named **Codex Backup & Restore** and the main script is now `codex-backup-restore.py`. Existing command arguments still work. Global instructions, conversations, and Skills may contain private information; keep backup packages private.

### Skill transfer

Export and import include user skills by default. You can still pass `--include-skills` explicitly. To skip skills, add `--exclude-skills` to the relevant command:

```text
python codex-backup-restore.py export codex-chats-only.zip --exclude-skills
python codex-backup-restore.py import codex-chats-only.zip --exclude-skills
```

The script looks for user skill folders containing `SKILL.md` in the Codex data directory's `skills` folder and the user's `.agents/skills`. Each selected folder's supporting files travel with it. Repository skills stay with Git, and built-in `.system` skills are skipped. If a same-name destination skill differs, the importer reports a conflict and keeps the destination copy. Review skill contents and provenance before transferring executable scripts or private configuration.

### Clean up old backups and temporary files

Preview first, then fully quit Codex before applying the cleanup:

```text
python codex-backup-restore.py cleanup
python codex-backup-restore.py cleanup --apply
```

Cleanup checks the script's `backups` directory and the Codex data directory for import backups older than seven days. It also checks for old Codex global-state temporary files. It keeps the newest import backup in each location and backups of replaced conversation files. Recently modified directories or files, backups with unexpected contents, and links are skipped. Deleting an old backup removes its corresponding rollback option.

Conversations, active databases, Skills, configuration, browser data, exported ZIPs, and other tools' backups are retained. The default command lists candidates and their size. `--apply` permanently deletes the listed candidates.

The cleanup tab includes Codex app and plugin caches by default. Preview and results show totals first. Select 查看明细 (Show details) for the file list and cache subtotal. The script matches app directory, connector tool and server metadata, and remote plugin catalog JSON cache files older than seven days. Codex may download this data again the next time it uses those features. Use `--include-cache` to include caches from the command line:

```text
python codex-backup-restore.py cleanup --include-cache
python codex-backup-restore.py cleanup --include-cache --apply
```

### Repair a wiped sidebar

After Codex exits unexpectedly, the sidebar can save empty project, pin, and Section membership lists while `state_5.sqlite` still has the projects and chat assignments. `repair` fills those sidebar records back in from the database and older global-state files on the same computer.

```text
python codex-backup-restore.py repair
python codex-backup-restore.py repair --apply
```

Preview first. After checking the output, quit Codex completely and run the command with `--apply`. The script copies `.codex-global-state.json` to `backups/sidebar-repair-backup-<timestamp>.json` beside the script, then writes the sidebar. In the desktop window, choose 修复侧栏, preview, and confirm before it writes.

Project records use the ids currently stored in the database. When the sidebar still stores pre-migration ids, the script keeps those ids. When current ids and the legacy id map are both present, the script removes that map so Codex does not import the same projects again on the next launch.

Section membership is copied only from a global-state backup already on this computer, into the current Sections with the same identity. Section ids, names, and order stay as they are. Projects that no longer exist are skipped. Projects that had no Section membership in the backup stay in Projects. Sections that already have items, projects already registered, and existing chat assignments are left unchanged.

`repair` does not read a ZIP. It does not change conversation text, the databases, Skills, the selected remote host, or project directories.

### Help and preview

```text
python codex-backup-restore.py --help
python codex-backup-restore.py export --preview
python codex-backup-restore.py import --preview
```

Use `--codex-home` to select a custom Codex data directory. Import preview checks the package and destination and lists missing project directories before applying changes.

The ZIP contains private conversation content, tool output, and paths. Keep it private and import only trusted ZIPs. Project files, sign-in credentials, browser data, app configuration, and plugins are excluded.

## 许可证 / License

本项目采用 MIT 许可证。 This project is licensed under the MIT License.

Copyright (c) 2026 Codex Backup & Restore contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
