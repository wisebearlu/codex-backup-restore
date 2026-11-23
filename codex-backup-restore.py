"""Codex Backup & Restore: conversations, sections, global instructions and user skills (Python 3.14+)."""
import argparse
import copy
import hashlib
import json
import os
import queue
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque
from compression import zstd
from contextlib import closing
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

STATE = "state_5.sqlite"
HISTORY = "thread_history_1.sqlite"
INDEX = "session_index.jsonl"
SIDEBAR = ".codex-global-state.json"
SCRIPT_DIR = Path(__file__).resolve().parent
BACKUP_DIR = SCRIPT_DIR / "backups"
DEFAULT_ARCHIVE = BACKUP_DIR / "codex-chats.zip"
LEGACY_ARCHIVE = SCRIPT_DIR / DEFAULT_ARCHIVE.name
SESSION_DIRS = {"sessions", "archived_sessions"}
SECTIONS = "thread_sections"
SKILL_PREFIX = "user-skills"
INSTRUCTION_PREFIX = "global-instructions"
INSTRUCTION_FILES = ("AGENTS.md", "AGENTS.override.md")
STATE_TABLES = ("projects", "project_roots", "threads", "thread_dynamic_tools",
                "thread_spawn_edges")
HISTORY_TABLES = ("thread_turns", "thread_items", "thread_history_projection_state",
                  "thread_realtime_items")
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ROLLOUT = re.compile(rf"rollout-.*?-({UUID})(?:_({UUID}))?\.jsonl(?:\.zst)?$", re.I)
CLEANUP_DAYS = 7
BACKUP_NAME = re.compile(r"conversation-transfer-backup-\d{8}-\d{6}-\d{6}$")
STATE_TEMP_NAME = re.compile(
    rf"\.\.codex-global-state\.json(?:\.bak)?\.tmp-\d+-{UUID}$", re.I)
BACKUP_FILES = {STATE, HISTORY, INDEX, SIDEBAR, "new-index.tmp", "new-sidebar.tmp"}
CACHE_FILE_PATTERNS = {
    "codex_app_directory": re.compile(r"[0-9a-f]{40}\.json"),
    "codex_apps_server_info": re.compile(r"[0-9a-f]{40}\.json"),
    "codex_apps_tools": re.compile(r"[0-9a-f]{40}\.json"),
    "remote_plugin_catalog": re.compile(r"[0-9a-f]{16}\.json"),
}
SECTION_ATOM = "sidebar-custom-sections-v3"
LEGACY_MAP = "app-server-project-id-by-legacy-project-id-by-host"
MIGRATION_PREFIX = "electron-owner-value:app-server-projects-migration-by-host:"
PROJECT_ITEM = "codex:project:"


def active_codex_processes():
    if os.name != "nt":
        raise ValueError("This release supports Windows on both computers.")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Get-Process | ForEach-Object { '{0}:{1}' -f $_.Id, $_.ProcessName }"],
        check=True, capture_output=True, text=True, errors="replace", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    ignored = {"codex-windows-sandbox-service", "codex-windows-sandbox-setup"}
    active = {}
    for line in result.stdout.splitlines():
        pid, separator, name = line.partition(":")
        if separator and pid.isascii() and pid.isdecimal() and name.casefold() not in ignored \
                and re.fullmatch(r"(?:codex|chatgpt)(?:[-_ ][a-z0-9_ .-]+)?", name, re.I):
            active[int(pid)] = name
    return active


def require_stopped():
    active = active_codex_processes()
    if active:
        raise ValueError("Fully quit Codex / ChatGPT / Codex CLI first: " + ", ".join(sorted(set(active.values()))))


def close_codex_processes(approved):
    for pid, name in approved.items():
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
             f"if ($p -and $p.ProcessName -eq '{name}' -and "
             "$p.SessionId -eq (Get-Process -Id $PID).SessionId) "
             "{ Stop-Process -InputObject $p -Force -ErrorAction Stop }"],
            check=True, capture_output=True, text=True, errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    deadline = time.monotonic() + 6
    while active_codex_processes():
        if time.monotonic() >= deadline:
            raise ValueError("Codex processes are still running. Close the remaining apps and retry.")
        time.sleep(0.2)


def read_db(path):
    if not path.is_file():
        raise ValueError(f"Missing database: {path}")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def snapshot(source, target):
    with closing(read_db(source)) as db, closing(sqlite3.connect(target)) as copy:
        db.backup(copy)


def header(path):
    opener = zstd.open if path.name.endswith(".zst") else open
    with opener(path, "rb") as stream:
        row = json.loads(stream.readline(8 * 1024 * 1024))
    if row.get("type") != "session_meta":
        raise ValueError("Missing session_meta")
    return row["payload"]


def path_key(path):
    normalized = os.path.normcase(os.path.normpath(path))
    if normalized.startswith("\\\\?\\unc\\"):
        return "\\\\" + normalized[8:]
    return normalized.removeprefix("\\\\?\\")


def below(path, root):
    path = path_key(path)
    root = path_key(root)
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def legacy_project_assignments(home, projects, roots):
    state_path = home / ".codex-global-state.json"
    if not state_path.is_file():
        return {}
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    project_roots = defaultdict(set)
    for root in roots:
        project_roots[root["project_id"]].add(path_key(root["path"]))
    mapped = {}
    for key, entry in state.get("local-projects", {}).items():
        paths = {path_key(path) for path in entry.get("rootPaths", [])}
        matches = [project for project in projects if paths and project_roots[project] == paths]
        if len(matches) == 1:
            mapped[key] = matches[0]
    return {thread: mapped[entry["projectId"]]
            for thread, entry in state.get("thread-project-assignments", {}).items()
            if isinstance(entry, dict) and entry.get("projectKind") == "local"
            and entry.get("projectId") in mapped
            and state.get("thread-project-membership-host-ids", {}).get(thread, "local") == "local"}


def discover(db, home):
    projects = {r["id"]: dict(r) for r in db.execute("SELECT * FROM projects")}
    roots = list(db.execute("SELECT * FROM project_roots"))
    roots = sorted((r for r in roots if r["path"]), key=lambda r: -len(r["path"]))
    projects = {key: row for key, row in projects.items()
                if any(r["project_id"] == key for r in roots)}
    legacy_owners = legacy_project_assignments(home, projects, roots)
    threads = {r["id"]: dict(r) for r in db.execute("SELECT * FROM threads")}
    owners = {}
    for key, row in threads.items():
        owner = row["project_id"]
        if owner not in projects:
            owner = legacy_owners.get(key) or next(
                (r["project_id"] for r in roots if below(row["cwd"], r["path"])), None)
        if owner:
            owners[key] = owner
    edges = defaultdict(set)
    for parent, child in db.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
        if parent in threads and child in threads:
            edges[parent].add(child)
            edges[child].add(parent)
    files = defaultdict(set)
    rejected = {}
    history_bases = set()
    for folder in sorted(SESSION_DIRS):
        for path in (home / folder).rglob("rollout-*.jsonl*"):
            match = ROLLOUT.fullmatch(path.name)
            if not match or not path.is_file():
                continue
            ids = set(filter(None, match.groups()))
            try:
                meta = header(path)
            except (ValueError, OSError, EOFError, KeyError, zstd.ZstdError) as exc:
                rejected[path] = (ids, str(exc))
                continue
            ids.add(meta["id"])
            for key in ids:
                files[key].add(path)
                edges[key].update(ids - {key})
            base = (meta.get("history_base") or {}).get("thread_id")
            parent = meta.get("forked_from_id")
            for dependency in filter(None, (base, parent)):
                edges[meta["id"]].add(dependency)
                if dependency == base:
                    history_bases.add((meta["id"], dependency))
            if meta["id"] not in threads:
                owner = legacy_owners.get(meta["id"]) or next(
                    (r["project_id"] for r in roots if below(meta.get("cwd", ""), r["path"])), None)
                if owner:
                    owners.setdefault(meta["id"], owner)
    wanted = set(owners) | {key for key, row in threads.items() if row.get("thread_section_id")}
    queue = deque(wanted)
    while queue:
        for dependency in edges[queue.popleft()] - wanted:
            wanted.add(dependency)
            queue.append(dependency)
    selected = {path for key in wanted for path in files[key]}
    problems = [f"Missing history base {base} referenced by {child}"
                for child, base in sorted(history_bases) if child in wanted and not files[base]]
    problems += [f"Unreadable {p.name}: {error}" for p, (ids, error) in rejected.items()
                 if ids & wanted]
    for key in sorted(wanted & threads.keys()):
        row = threads[key]
        saved = Path(row["rollout_path"])
        candidates = [p for p in files[key] if p.name.removesuffix(".zst") == saved.name.removesuffix(".zst")]
        if not candidates:
            problems.append(f"Missing primary rollout for {key}: {saved.name}")
        elif len(candidates) > 1:
            problems.append(f"Multiple copies of primary rollout for {key}: {saved.name}")
        else:
            row["rollout_path"] = candidates[0].relative_to(home).as_posix()
            row["project_id"] = owners.get(key)
            for field in ("creator_account_id", "creator_user_id"):
                if field in row:
                    row[field] = None
    return projects, roots, threads, wanted, selected, problems


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def export_tables(db, package, tables, filters, thread_rows=None):
    for table in tables:
        rows = db.execute(f"SELECT * FROM {quote(table)} WHERE {filters[table]}")
        names = [column[0] for column in rows.description]
        package.execute(f"CREATE TABLE {quote(table)} ({','.join(map(quote, names))})")
        values = (tuple(thread_rows[row["id"]][name] for name in names) for row in rows) \
            if table == "threads" else rows
        if table == "projects":
            values = (tuple("{}" if name == "metadata" else row[name] for name in names)
                      for row in rows)
        placeholders = ",".join("?" for _ in names)
        package.executemany(f"INSERT INTO {quote(table)} VALUES ({placeholders})", values)


def add_ids(db, name, ids):
    db.execute(f"CREATE TEMP TABLE {name} (id TEXT PRIMARY KEY)")
    db.executemany(f"INSERT INTO {name} VALUES (?)", ((key,) for key in ids))


def index_lines(path):
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                yield json.loads(line)


def skill_roots(home):
    return {"codex": home / "skills", "agents": Path.home() / ".agents" / "skills"}


def is_link(path):
    return path.is_symlink() or path.is_junction()


def ensure_backup_directory():
    if is_link(BACKUP_DIR):
        raise ValueError(f"Backup directory is a link: {BACKUP_DIR}")
    BACKUP_DIR.mkdir(exist_ok=True)


def default_archive(mode):
    if mode == "import" and not DEFAULT_ARCHIVE.exists() and LEGACY_ARCHIVE.is_file():
        return LEGACY_ARCHIVE
    return DEFAULT_ARCHIVE


def raise_walk_error(error):
    raise error


def skill_files(folder):
    files = {}
    for directory, dirs, names in folder.walk(on_error=raise_walk_error):
        for name in dirs:
            if is_link(directory / name):
                raise ValueError(f"Skill contains a link: {directory / name}")
        for name in names:
            entry = directory / name
            if is_link(entry) or not entry.is_file():
                raise ValueError(f"Invalid or linked skill file: {entry}")
            files[entry.relative_to(folder)] = entry
    return files


def collect_skills(home):
    skills = {}
    for scope, root in skill_roots(home).items():
        if is_link(root) or is_link(root.parent):
            raise ValueError(f"Skill root is a link: {root}")
        if not root.is_dir():
            continue
        for folder in root.iterdir():
            if not folder.name.startswith(".") and folder.is_dir() and (folder / "SKILL.md").is_file():
                if is_link(folder):
                    raise ValueError(f"Skill directory is a link: {folder}")
                skills[scope, folder.name] = skill_files(folder)
    return skills


def collect_instructions(home):
    files = {}
    for name in INSTRUCTION_FILES:
        path = home / name
        if is_link(path):
            raise ValueError(f"Global instruction file is a link: {path}")
        if path.exists():
            if not path.is_file():
                raise ValueError(f"Global instruction path is not a file: {path}")
            files[name] = path
    return files


def plan_instructions(home, work):
    additions = []
    for name in INSTRUCTION_FILES:
        source = work / INSTRUCTION_PREFIX / name
        if not source.is_file():
            continue
        target = home / name
        if is_link(target) or (target.exists() and not target.is_file()):
            raise ValueError(f"Unsafe global instruction destination: {target}")
        if target.is_file():
            if digest(source) != digest(target):
                print(f"Global instruction conflict, kept destination: {target}")
        else:
            additions.append((source, target))
    return additions


def export(home, archive, preview, include_skills=True):
    if not preview:
        require_stopped()
    with closing(read_db(home / STATE)) as state:
        projects, roots, threads, wanted, files, problems = discover(state, home)
        print(f"Detected {len(projects)} projects, {len({path_key(r['path']) for r in roots})} distinct roots,"
              f" {len(wanted & threads.keys())} indexed chats,"
              f" {len(files)} rollout files / segments.")
        for problem in problems[:25]:
            print(problem)
        if problems:
            raise ValueError(f"{len(problems)} history problems. No bundle created.")
        skills = collect_skills(home) if include_skills else {}
        instructions = collect_instructions(home)
        if not files and not skills and not instructions:
            raise ValueError("No conversations, user skills or global instruction files found.")
        print(f"{len(instructions)} global instruction files selected.")
        if include_skills:
            print(f"{len(skills)} user skills selected.")
        if preview:
            return
        if archive.exists():
            raise ValueError(f"Output already exists: {archive}")
        if archive.is_relative_to(home):
            raise ValueError("Save the archive outside CODEX_HOME.")
        if archive.parent == BACKUP_DIR:
            ensure_backup_directory()
        with tempfile.TemporaryDirectory(dir=archive.parent) as work:
            work = Path(work)
            state_filter = {"projects": "id IN (SELECT id FROM picked_projects)",
                            "project_roots": "project_id IN (SELECT id FROM picked_projects)",
                            "threads": "id IN (SELECT id FROM picked)",
                            "thread_dynamic_tools": "thread_id IN (SELECT id FROM picked)",
                            "thread_spawn_edges": "parent_thread_id IN (SELECT id FROM picked) "
                                                  "AND child_thread_id IN (SELECT id FROM picked)"}
            add_ids(state, "picked_projects", projects)
            add_ids(state, "picked", wanted)
            with closing(sqlite3.connect(work / STATE)) as subset:
                export_tables(state, subset, STATE_TABLES, state_filter, threads)
                if state.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (SECTIONS,)).fetchone():
                    export_tables(state, subset, (SECTIONS,), {
                        SECTIONS: "1 ORDER BY rowid",
                    })
                subset.commit()
            with closing(read_db(home / HISTORY)) as history, \
                    closing(sqlite3.connect(work / HISTORY)) as subset:
                add_ids(history, "picked", wanted)
                export_tables(history, subset, HISTORY_TABLES,
                              dict.fromkeys(HISTORY_TABLES, "thread_id IN (SELECT id FROM picked)"))
                subset.commit()
            index = [row for row in index_lines(home / INDEX) if row.get("id") in wanted]
            require_stopped()
            package = work / "bundle.zip"
            with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as zip_file:
                zip_file.writestr("manifest.json", json.dumps({"format": 1, "projects": len(projects),
                                                              "chats": len(wanted & threads.keys())}))
                zip_file.writestr(INDEX, "".join(json.dumps(row) + "\n" for row in index))
                for name in (STATE, HISTORY):
                    zip_file.write(work / name, name)
                for path in sorted(files):
                    zip_file.write(path, path.relative_to(home).as_posix())
                for (scope, folder), entries in sorted(skills.items()):
                    for relative, path in sorted(entries.items()):
                        zip_file.write(path, f"{SKILL_PREFIX}/{scope}/{folder}/{relative.as_posix()}")
                for name, path in instructions.items():
                    zip_file.write(path, f"{INSTRUCTION_PREFIX}/{name}")
            require_stopped()
            os.rename(package, archive)
        print(f"Saved {archive}")


def unpack(archive, work, include_skills):
    required = {"manifest.json", STATE, HISTORY, INDEX}
    with zipfile.ZipFile(archive) as bundle:
        names = set()
        for entry in bundle.infolist():
            name = entry.filename
            path = PurePosixPath(name)
            if name.casefold() in names or path.as_posix() != name or path.is_absolute() or \
                    ".." in path.parts or any(os.path.isreserved(part) for part in path.parts) or "\\" in name or \
                    entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f"Invalid archive entry: {name}")
            names.add(name.casefold())
            is_skill = (len(path.parts) >= 4 and path.parts[0] == SKILL_PREFIX
                        and path.parts[1] in {"codex", "agents"} and not path.parts[2].startswith("."))
            is_instruction = (len(path.parts) == 2 and path.parts[0] == INSTRUCTION_PREFIX
                              and path.parts[1] in INSTRUCTION_FILES)
            if name not in required and not is_skill and not is_instruction and not (
                    len(path.parts) >= 2 and path.parts[0] in SESSION_DIRS and ROLLOUT.fullmatch(path.name)):
                raise ValueError(f"Unexpected archive entry: {name}")
            if is_skill and not include_skills:
                continue
            target = work.joinpath(*path.parts)
            if not target.resolve().is_relative_to(work.resolve()):
                raise ValueError(f"Unsafe archive entry: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    if not required <= names or json.loads((work / "manifest.json").read_text())["format"] != 1:
        raise ValueError("Unsupported bundle.")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def compare_rollouts(source, target):
    """Compare decoded bytes so compression settings do not affect the decision."""
    source_open = zstd.open if source.name.endswith(".zst") else open
    target_open = zstd.open if target.name.endswith(".zst") else open
    with source_open(source, "rb") as left, target_open(target, "rb") as right:
        while True:
            incoming = left.read(1024 * 1024)
            current = right.read(1024 * 1024)
            if incoming == current:
                if not incoming:
                    return "same"
                continue
            if len(incoming) > len(current) and incoming.startswith(current):
                return "update"
            if len(current) > len(incoming) and current.startswith(incoming):
                return "ahead"
            return "diverged"


def rollout_ids(path):
    match = ROLLOUT.fullmatch(path.name)
    if match is None:
        raise ValueError(f"Invalid rollout filename: {path.name}")
    meta = header(path)
    ids = set(filter(None, (*match.groups(), meta["id"])))
    related = set(filter(None, (meta.get("forked_from_id"),
                                (meta.get("history_base") or {}).get("thread_id"))))
    return ids, related


def related_ids(seeds, edges):
    found = set(seeds)
    pending = deque(found)
    while pending:
        for key in edges[pending.popleft()] - found:
            found.add(key)
            pending.append(key)
    return found


def plan_rollouts(work, home, target_db, keep_existing):
    primary = defaultdict(set)
    destinations = {}
    edges = defaultdict(set)
    with closing(read_db(work / STATE)) as source:
        for key, relative in source.execute("SELECT id, rollout_path FROM threads"):
            relative = PurePosixPath(relative)
            path = work / relative
            if (len(relative.parts) < 2 or relative.parts[0] not in SESSION_DIRS
                    or ".." in relative.parts or not ROLLOUT.fullmatch(relative.name)
                    or not path.resolve().is_relative_to(work) or not path.is_file()):
                raise ValueError(f"Invalid primary rollout for {key}")
            primary[path].add(key)
            current = target_db.execute("SELECT rollout_path FROM threads WHERE id=?", (key,)).fetchone()
            if current:
                destinations[path] = Path(current[0])
        for parent, child in source.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
            edges[parent].add(child)
            edges[child].add(parent)
    plans = []
    ahead = set()
    diverged = set()
    targets = set()
    for folder in sorted(SESSION_DIRS):
        for path in sorted((work / folder).rglob("*")):
            if not path.is_file():
                continue
            ids, related = rollout_ids(path)
            ids.update(primary[path])
            for key in ids | related:
                edges[key].update((ids | related) - {key})
            bundled_target = home / path.relative_to(work)
            target = destinations.get(path, bundled_target)
            if (not target.is_absolute() or not ROLLOUT.fullmatch(target.name) or is_link(target)
                    or not any(target.resolve().is_relative_to(home / directory)
                               for directory in SESSION_DIRS)):
                raise ValueError(f"Unsafe rollout destination: {target}")
            if target != bundled_target and bundled_target.exists():
                raise ValueError(f"Multiple destination copies for rollout: {path.name}")
            if target in targets:
                raise ValueError(f"Multiple archived rollouts point to: {target}")
            targets.add(target)
            if target.exists() and not target.is_file():
                raise ValueError(f"Rollout destination is not a file: {target}")
            status = compare_rollouts(path, target) if target.is_file() else "new"
            if status == "ahead":
                ahead.update(ids)
            elif status == "diverged":
                diverged.update(ids)
            plans.append((path, target, ids, status))

    divergent_group = related_ids(diverged, edges)
    skipped = related_ids(ahead - divergent_group, edges)
    for _, target, ids, status in plans:
        if status in {"ahead", "diverged"}:
            print(f"Rollout {status}: {target.name}")
    if keep_existing:
        skipped.update(divergent_group)
    elif divergent_group:
        # A destination-only fork or page may refer to bytes replaced by this import.
        changed_ids = {key for _, _, ids, status in plans
                       if status in {"ahead", "diverged"} and ids & divergent_group for key in ids}
        for folder in SESSION_DIRS:
            for path in (home / folder).rglob("rollout-*.jsonl*"):
                if path in targets or not path.is_file() or not ROLLOUT.fullmatch(path.name):
                    continue
                ids, related = rollout_ids(path)
                affected = (ids | related) & changed_ids
                if affected:
                    skipped.update(related_ids(affected, edges))
                    print(f"Keeping history referenced by destination-only rollout: {path.name}")
    if skipped:
        print(f"Keeping destination history for {len(skipped)} related history IDs.")
    additions = []
    replacements = []
    refreshed = set()
    locations = {}
    for path, target, ids, status in plans:
        if ids & skipped:
            continue
        locations[path.relative_to(work).as_posix()] = target
        if status == "new":
            additions.append((path, target))
        elif status != "same":
            if target.name.endswith(".zst") != path.name.endswith(".zst"):
                raise ValueError(f"Different rollout encodings for {target.name}; retry with --keep-existing "
                                 "for divergent history.")
            replacements.append((path, target, digest(target)))
            refreshed.update(ids)
    return additions, replacements, skipped, refreshed, locations


def plan_skills(home, work):
    planned = {}
    for scope, root in skill_roots(home).items():
        incoming = work / SKILL_PREFIX / scope
        if not incoming.is_dir():
            continue
        if any(is_link(path) or (path.exists() and not path.is_dir()) for path in (root.parent, root)):
            raise ValueError(f"Unsafe skill destination: {root}")
        for folder in sorted(incoming.iterdir()):
            if not (folder / "SKILL.md").is_file():
                raise ValueError(f"Missing SKILL.md in skill: {folder.name}")
            target = root / folder.name
            entries = skill_files(folder)
            if is_link(target) or (target.exists() and not target.is_dir()):
                print(f"Skill conflict, kept destination: {target}")
            elif target.is_dir():
                existing = skill_files(target)
                if existing.keys() != entries.keys() or any(
                        digest(existing[path]) != digest(entries[path]) for path in entries):
                    print(f"Skill conflict, kept destination: {target}")
            else:
                planned[target] = entries
    return planned


def merge_row(db, schema, table, row):
    columns = ",".join(map(quote, row.keys()))
    placeholders = ",".join("?" for _ in row)
    db.execute(f"INSERT INTO {schema}.{quote(table)} ({columns}) VALUES ({placeholders})",
               tuple(row.values()))


def merge_sections(db, source):
    if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SECTIONS,)).fetchone():
        return set()
    sections = list(source.execute(f"SELECT * FROM {SECTIONS} ORDER BY rowid"))
    if not sections:
        return set()
    incoming = set(sections[0].keys())
    target = {column[1] for column in db.execute(f"PRAGMA table_info({SECTIONS})")}
    if incoming != target:
        raise ValueError(f"Incompatible Codex schema for {SECTIONS}. Use matching Codex versions.")
    existing = {key for key, in db.execute(f"SELECT id FROM {SECTIONS}")}
    created = set()
    for section in sections:
        key = section["id"]
        if key not in existing:
            merge_row(db, "main", SECTIONS, dict(section))
            created.add(key)
    return created


def import_rows(db, work, provider, skipped, refreshed, locations):
    with closing(read_db(work / STATE)) as source, closing(read_db(work / HISTORY)) as history:
        for input_db, schema, tables in ((source, "main", STATE_TABLES),
                                         (history, "hist", HISTORY_TABLES)):
            for table in tables:
                incoming = {r[1] for r in input_db.execute(f"PRAGMA table_info({quote(table)})")}
                target = {r[1] for r in db.execute(f"PRAGMA {schema}.table_info({quote(table)})")}
                if not incoming or incoming != target:
                    raise ValueError(f"Incompatible Codex schema for {table}. Use matching Codex versions.")
        roots = defaultdict(list)
        for row in source.execute("SELECT * FROM project_roots ORDER BY position"):
            roots[row["project_id"]].append(dict(row))
        target_roots = defaultdict(list)
        for key, path in db.execute("SELECT project_id, path FROM project_roots ORDER BY position"):
            target_roots[key].append(path_key(path))
        project_map = {}
        for item in source.execute("SELECT * FROM projects"):
            row = dict(item)
            key = row["id"]
            paths = [path_key(r["path"]) for r in roots[key]]
            matches = [other for other, other_paths in target_roots.items() if other_paths == paths]
            if (key in target_roots and key not in matches) or len(matches) > 1:
                raise ValueError(f"Conflicting project registration: {row['name']}")
            if matches:
                project_map[key] = matches[0]
                continue
            project_map[key] = key
            row["metadata"] = "{}"
            if "position" in row:
                row["position"] = db.execute("SELECT coalesce(max(position), 0) + 1 FROM projects").fetchone()[0]
            merge_row(db, "main", "projects", row)
            for root in roots[key]:
                merge_row(db, "main", "project_roots", root)
        new_sections = merge_sections(db, source)
        existing = dict(db.execute("SELECT id, project_id FROM threads"))
        imported = set()
        memberships = {}
        for item in source.execute("SELECT * FROM threads"):
            row = dict(item)
            key = row["id"]
            if key in skipped:
                continue
            relative = PurePosixPath(row["rollout_path"])
            if relative.parts[0] not in SESSION_DIRS or not (work / relative).is_file():
                raise ValueError(f"Invalid primary rollout for {key}")
            if key in existing:
                current_project = existing[key]
                project = current_project or project_map.get(row["project_id"])
                if project:
                    memberships[key] = project
                    if not current_project:
                        db.execute("UPDATE threads SET project_id=? WHERE id=?", (project, key))
                if row.get("thread_section_id") in new_sections:
                    db.execute("UPDATE threads SET thread_section_id=?, section_position=?, "
                               "section_entered_at_ms=? WHERE id=? AND thread_section_id IS NULL",
                               (row["thread_section_id"], row["section_position"],
                                row["section_entered_at_ms"], key))
                if key in refreshed:
                    # Keep local placement and account fields while refreshing history metadata.
                    local_fields = {"id", "project_id", "thread_section_id", "section_position",
                                    "section_entered_at_ms", "creator_account_id", "creator_user_id",
                                    "pinned_at", "is_pinned"}
                    updates = {column: value for column, value in row.items() if column not in local_fields}
                    updates["rollout_path"] = str(locations[relative.as_posix()])
                    if provider and "model_provider" in updates:
                        updates["model_provider"] = provider
                    fields = ", ".join(f"{quote(column)}=?" for column in updates)
                    db.execute(f"UPDATE threads SET {fields} WHERE id=?", (*updates.values(), key))
                continue
            row["rollout_path"] = str(locations[relative.as_posix()])
            row["project_id"] = project_map.get(row["project_id"])
            if row["project_id"]:
                memberships[key] = row["project_id"]
            if provider:
                row["model_provider"] = provider
            merge_row(db, "main", "threads", row)
            imported.add(key)
        for table in STATE_TABLES[3:]:
            id_column = "thread_id" if table == "thread_dynamic_tools" else "child_thread_id"
            db.executemany(f"DELETE FROM main.{quote(table)} WHERE {id_column}=?",
                           ((key,) for key in refreshed))
            for item in source.execute(f"SELECT * FROM {quote(table)}"):
                row = dict(item)
                if row[id_column] in imported or row[id_column] in refreshed:
                    merge_row(db, "main", table, row)
        for table in HISTORY_TABLES:
            db.executemany(f"DELETE FROM hist.{quote(table)} WHERE thread_id=?",
                           ((key,) for key in refreshed))
            primary_key = [column[1] for column in sorted(
                db.execute(f"PRAGMA hist.table_info({quote(table)})"), key=lambda column: column[5])
                if column[5]]
            if not primary_key:
                raise ValueError(f"Missing history primary key: {table}")
            match = " AND ".join(f"{quote(column)}=?" for column in primary_key)
            for item in history.execute(f"SELECT * FROM {quote(table)}"):
                row = dict(item)
                key = row["thread_id"]
                if key in skipped or (key in existing and key not in refreshed):
                    continue
                # A paginated segment can have projection rows without a threads row.
                # Preserve its existing projection when reimporting the same bundle.
                if db.execute(f"SELECT 1 FROM hist.{quote(table)} WHERE {match}",
                              tuple(row[column] for column in primary_key)).fetchone():
                    continue
                merge_row(db, "hist", table, row)
        return imported, set(existing), set(project_map.values()), memberships


def merge_sidebar(original, db, home, project_ids, memberships):
    state = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    registry = state.setdefault("local-projects", {})
    order = state.setdefault("project-order", [])
    mapping = state.setdefault("app-server-project-id-by-legacy-project-id-by-host", {})
    local_host = f"local:{home}"
    host = next((key for key in mapping if key.startswith("local:")
                 and path_key(key[6:]) == path_key(str(home))), local_host)
    modern_ids = mapping.setdefault(host, {})
    assignments = state.setdefault("thread-project-assignments", {})
    assignment_hosts = state.setdefault("thread-project-membership-host-ids", {})
    legacy_ids = {}
    for project in sorted(project_ids | set(memberships.values())):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        roots = [path for path, in db.execute(
            "SELECT path FROM project_roots WHERE project_id=? ORDER BY position", (project,))]
        if row is None or not roots:
            continue
        root_keys = {path_key(path) for path in roots}
        legacy = next((key for key in registry if modern_ids.get(key) == project), None)
        if legacy is None:
            legacy = next((key for key, entry in registry.items()
                           if not modern_ids.get(key)
                           and {path_key(p) for p in entry.get("rootPaths", [])} == root_keys), None)
        if legacy is None:
            legacy = str(uuid5(NAMESPACE_URL, f"codex-conversation-transfer:{project}"))
            if legacy in registry:
                raise ValueError(f"Conflicting sidebar registration: {project}")
            data = dict(row)
            registry[legacy] = {"id": legacy, "name": data["name"], "rootPaths": roots,
                                "createdAt": data.get("created_at_ms", 0),
                                "updatedAt": data.get("updated_at_ms", 0)}
        modern_ids[legacy] = project
        legacy_ids[project] = legacy
        if legacy not in order:
            order.append(legacy)
    for thread, project in memberships.items():
        if project in legacy_ids and thread not in assignments:
            assignments[thread] = {"projectKind": "local", "projectId": legacy_ids[project]}
            assignment_hosts[thread] = "local"
    return json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def replace_file(target, *, content=None, source=None):
    # Stage beside the target so replacement also works across different drives.
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".codex-transfer-",
                                     suffix=".tmp", delete=False) as staged:
        path = Path(staged.name)
        try:
            if source is None:
                staged.write(content)
            else:
                with source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, staged)
            staged.close()
            os.replace(path, target)
        finally:
            staged.close()
            path.unlink(missing_ok=True)


def import_bundle(home, archive, preview, provider, include_skills=True, keep_existing=False):
    require_stopped()
    with tempfile.TemporaryDirectory(prefix="codex-transfer-", dir=archive.parent) as temp:
        work = Path(temp)
        unpack(archive, work, include_skills)
        skills = plan_skills(home, work) if include_skills else {}
        if include_skills:
            print(f"{len(skills)} new user skills.")
        instructions = plan_instructions(home, work)
        print(f"{len(instructions)} new global instruction files.")
        original_index = (home / INDEX).read_bytes() if (home / INDEX).is_file() else None
        original_sidebar = (home / SIDEBAR).read_bytes() if (home / SIDEBAR).is_file() else None
        # Work on database snapshots, leaving the live profile untouched during preview.
        for name in (STATE, HISTORY):
            snapshot(home / name, work / ("target-" + name))
        with closing(sqlite3.connect(work / ("target-" + STATE))) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("ATTACH DATABASE ? AS hist", (str(work / ("target-" + HISTORY)),))
            db.execute("BEGIN")
            additions, replacements, skipped, refreshed, locations = plan_rollouts(work, home, db, keep_existing)
            imported, existing, projects, memberships = import_rows(db, work, provider, skipped, refreshed, locations)
            merge_sidebar(original_sidebar, db, home, projects, memberships)
            db.commit()
        print(f"{len(imported)} new indexed chats, {len(additions)} new rollout files.")
        print(f"{len(replacements)} rollout files to update or overwrite.")
        for _, target, _ in replacements:
            print(f"  {target.name}")
        additions.extend(instructions)
        print(f"{len(projects)} project registrations, {len(memberships)} chat memberships.")
        with closing(read_db(work / STATE)) as source:
            roots = {Path(path) for path, in source.execute("SELECT path FROM project_roots")}
        missing_roots = []
        for root in sorted(roots):
            if not root.is_absolute():
                raise ValueError(f"Project path must be absolute: {root}")
            if not root.is_dir():
                if root.exists():
                    raise ValueError(f"Project path is occupied by a file: {root}")
                missing_roots.append(root)
        if missing_roots:
            print("Project directories to create:\n" + "\n".join(map(str, missing_roots)))
        if preview:
            return
        require_stopped()
        ensure_backup_directory()
        backup = BACKUP_DIR / ("conversation-transfer-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        backup.mkdir()
        for name in (STATE, HISTORY):
            snapshot(home / name, backup / name)
        if original_index is not None:
            (backup / INDEX).write_bytes(original_index)
        if original_sidebar is not None:
            (backup / SIDEBAR).write_bytes(original_sidebar)
        previous = list(index_lines(home / INDEX))
        indexed_ids = {row.get("id") for row in previous}
        replacement_rows = {}
        new_rows = []
        for row in index_lines(work / INDEX):
            key = row.get("id")
            if key in refreshed:
                replacement_rows[key] = row
            elif key not in indexed_ids and key not in existing and key not in skipped:
                new_rows.append(row)
        updated = [replacement_rows.get(row.get("id"), row) for row in previous]
        updated.extend(row for key, row in replacement_rows.items() if key not in indexed_ids)
        created = []
        created_dirs = []
        replaced = []
        index_changed = False
        sidebar_changed = False
        try:
            for root in missing_roots:
                for directory in (*reversed(root.parents), root):
                    if not directory.is_dir():
                        directory.mkdir()
                        created_dirs.append(directory)
            for target, entries in skills.items():
                for directory in reversed(target.parents):
                    if not directory.is_dir():
                        directory.mkdir()
                        created_dirs.append(directory)
                if any(is_link(path) for path in (target.parent, target.parent.parent)):
                    raise ValueError(f"Unsafe skill destination: {target}")
                target.mkdir()
                created_dirs.append(target)
                for relative, source in entries.items():
                    path = target / relative
                    for directory in reversed(relative.parents):
                        parent = target / directory
                        if not parent.is_dir():
                            parent.mkdir()
                            created_dirs.append(parent)
                    with path.open("xb") as output:
                        created.append(path)
                        with source.open("rb") as incoming:
                            shutil.copyfileobj(incoming, output)
            for source, target in additions:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    created.append(target)
                    with source.open("rb") as incoming:
                        shutil.copyfileobj(incoming, output)
            require_stopped()
            with closing(sqlite3.connect(home / STATE)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("ATTACH DATABASE ? AS hist", (str(home / HISTORY),))
                for schema in ("main", "hist"):
                    if db.execute(f"PRAGMA {schema}.journal_mode=DELETE").fetchone()[0] != "delete":
                        raise ValueError("Could not obtain exclusive access to the target databases.")
                db.execute("BEGIN EXCLUSIVE")
                _, _, projects, memberships = import_rows(db, work, provider, skipped, refreshed, locations)
                for source, target, expected in replacements:
                    if is_link(target) or not target.resolve().is_relative_to(home) or digest(target) != expected:
                        raise ValueError(f"Destination rollout changed during import: {target}")
                    saved = backup / "rollouts" / target.relative_to(home)
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, saved)
                    if digest(saved) != expected:
                        raise ValueError(f"Destination rollout changed during backup: {target}")
                    replace_file(target, source=source)
                    replaced.append((saved, target))
                if new_rows or replacement_rows:
                    replace_file(home / INDEX, content="".join(
                        json.dumps(row) + "\n" for row in updated + new_rows).encode())
                    index_changed = True
                sidebar = merge_sidebar(original_sidebar, db, home, projects, memberships)
                replace_file(home / SIDEBAR, content=sidebar)
                sidebar_changed = True
                db.commit()
        except Exception:
            for saved, target in reversed(replaced):
                replace_file(target, source=saved)
            if sidebar_changed:
                if original_sidebar is None:
                    (home / SIDEBAR).unlink()
                else:
                    replace_file(home / SIDEBAR, content=original_sidebar)
            for path in reversed(created):
                path.unlink()
            if index_changed:
                if original_index is None:
                    (home / INDEX).unlink()
                else:
                    replace_file(home / INDEX, content=original_index)
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
        print(f"Imported. Backup: {backup}. Open Codex and check the original chats.")


def cleanup_snapshot(path):
    if is_link(path):
        raise ValueError(f"Cleanup skips links: {path}")
    entries = [path]
    if path.is_dir():
        entries.extend(sorted(path.iterdir()))
        if any(entry.name not in BACKUP_FILES or not entry.is_file() or is_link(entry)
               for entry in entries[1:]):
            raise ValueError(f"Cleanup skips backup with unexpected contents: {path}")
    elif not path.is_file():
        raise ValueError(f"Cleanup skips special file: {path}")
    snapshot = {}
    for entry in entries:
        info = entry.stat()
        snapshot[entry] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
    return snapshot


def format_cleanup_size(size):
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.2f} GB"
    if 0 < size < 10_000:
        return "<0.01 MB"
    return f"{size / 1_000_000:.2f} MB"


def codex_cache_files(home):
    cache = home / "cache"
    if is_link(cache) or not cache.is_dir():
        return set()
    files = set()
    for name, pattern in CACHE_FILE_PATTERNS.items():
        directory = cache / name
        if not is_link(directory) and directory.is_dir():
            files.update(path for path in directory.iterdir() if pattern.fullmatch(path.name))
    return files


def cleanup(home, apply=False, include_cache=False):
    if not home.is_dir():
        raise ValueError(f"Missing Codex directory: {home}")
    cutoff = time.time_ns() - CLEANUP_DAYS * 24 * 60 * 60 * 1_000_000_000
    roots = {home, BACKUP_DIR}
    children = set(home.iterdir())
    cache_files = codex_cache_files(home) if include_cache else set()
    protected = set()
    if BACKUP_DIR.is_dir() and not is_link(BACKUP_DIR):
        children.update(path for path in BACKUP_DIR.iterdir() if BACKUP_NAME.fullmatch(path.name))
    for root in roots:
        backups = [path for path in children if path.parent == root and BACKUP_NAME.fullmatch(path.name)
                   and path.is_dir() and not is_link(path)]
        if backups:
            protected.add(max(backups))
    candidates = []
    total_files = 0
    total_backups = 0
    total_bytes = 0
    for path in sorted(children | cache_files):
        if path in protected:
            continue
        is_backup = bool(BACKUP_NAME.fullmatch(path.name))
        is_cache = path in cache_files
        if not is_backup and not is_cache and not STATE_TEMP_NAME.fullmatch(path.name):
            continue
        try:
            if is_backup and not path.is_dir():
                raise ValueError(f"Cleanup skips invalid backup directory: {path}")
            if not is_backup and not path.is_file():
                raise ValueError(f"Cleanup skips invalid temporary file: {path}")
            snapshot = cleanup_snapshot(path)
            if any(info[2] >= cutoff for info in snapshot.values()):
                continue
        except (ValueError, OSError) as error:
            print(error)
            continue
        files = [info for info in snapshot.values() if stat.S_ISREG(info[3])]
        size = sum(info[1] for info in files)
        total_files += len(files)
        total_backups += int(is_backup)
        total_bytes += size
        candidates.append((path, snapshot, size))
    for path in sorted(protected):
        print(f"Keeping newest import backup: {path}")
    if not apply:
        for path, snapshot, size in candidates:
            if path in cache_files:
                kind = "Codex cache"
            else:
                kind = "Backup directory" if stat.S_ISDIR(snapshot[path][3]) else "Temporary file"
            print(f"{kind}: {path} ({format_cleanup_size(size)})")
        if include_cache:
            sizes = [size for path, _, size in candidates if path in cache_files]
            print(f"Codex cache candidates: {len(sizes):,} files, {format_cleanup_size(sum(sizes))}.")
        print("Preview only. Use cleanup --apply to delete these old files.")
        print(f"\nCleanup preview: {total_files:,} files, {total_backups:,} backup directories, "
              f"{format_cleanup_size(total_bytes)}.")
        return
    require_stopped()
    deleted_files = deleted_backups = deleted_bytes = 0
    deleted_cache_files = deleted_cache_bytes = 0
    try:
        for path, snapshot, _ in candidates:
            if path in cache_files:
                cache = home / "cache"
                allowed = (not is_link(cache) and not is_link(path.parent)
                           and path.resolve().parent == path.parent
                           and path.is_file())
            else:
                allowed = (path.parent in roots and not is_link(path.parent)
                           and path.resolve().parent == path.parent.resolve())
            if not allowed or cleanup_snapshot(path) != snapshot:
                raise ValueError(f"Cleanup candidate changed; stopped: {path}")
            for entry, info in snapshot.items():
                if stat.S_ISREG(info[3]):
                    entry.unlink()
                    deleted_files += 1
                    deleted_bytes += info[1]
                    if path in cache_files:
                        deleted_cache_files += 1
                        deleted_cache_bytes += info[1]
                    print(f"Deleted file: {entry} ({format_cleanup_size(info[1])})")
            if stat.S_ISDIR(snapshot[path][3]):
                path.rmdir()
                deleted_backups += 1
                print(f"Deleted backup directory: {path}")
    finally:
        if include_cache:
            print(f"Codex caches deleted: {deleted_cache_files:,} files, "
                  f"{format_cleanup_size(deleted_cache_bytes)}.")
        print(f"\nCleanup result: {deleted_files:,} files, {deleted_backups:,} backup directories, "
              f"{format_cleanup_size(deleted_bytes)} deleted.")


def read_state(path):
    try:
        if is_link(path) or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def state_snapshots(home):
    if not home.is_dir():
        return []
    found = []
    for path in home.iterdir():
        if path.name == SIDEBAR or "codex-global-state" not in path.name:
            continue
        if read_state(path) is None:
            continue
        found.append(path)
    return sorted(found, key=lambda item: item.stat().st_mtime_ns, reverse=True)


def database_projects(db):
    projects = []
    for row in db.execute(
            "SELECT id, name, created_at_ms, updated_at_ms FROM projects ORDER BY position, created_at_ms"):
        roots = [path for path, in db.execute(
            "SELECT path FROM project_roots WHERE project_id=? AND IFNULL(path, '') != '' ORDER BY position",
            (row["id"],))]
        if not roots:
            continue
        projects.append({"id": row["id"], "name": row["name"] or row["id"], "rootPaths": roots,
                         "createdAt": row["created_at_ms"] or 0, "updatedAt": row["updated_at_ms"] or 0})
    return projects


def database_threads(db):
    return list(db.execute(
        "SELECT id, project_id FROM threads WHERE project_id IS NOT NULL AND project_id != ''"))


def sidebar_project(project, sidebar_id):
    return {"id": sidebar_id, "name": project["name"], "rootPaths": list(project["rootPaths"]),
            "createdAt": project["createdAt"], "updatedAt": project["updatedAt"]}


def root_signature(entry):
    paths = entry.get("rootPaths") if isinstance(entry, dict) else None
    if not isinstance(paths, list):
        return ()
    return tuple(sorted(path_key(path) for path in paths if isinstance(path, str) and path))


def legacy_maps(state):
    raw = state.get(LEGACY_MAP)
    if not isinstance(raw, dict):
        return []
    return [item for item in raw.values() if isinstance(item, dict) and item]


def projects_migrated(state, home):
    home_key = path_key(str(home))
    for key, value in state.items():
        if not isinstance(key, str) or not key.startswith(MIGRATION_PREFIX) or not isinstance(value, dict):
            continue
        for host, entry in value.items():
            if (isinstance(host, str) and host.startswith("local:") and isinstance(entry, dict)
                    and entry.get("projectsMigrated")
                    and path_key(host.removeprefix("local:")) == home_key):
                return True
    return False


def registry_scheme(registry, server_ids):
    if not registry:
        return "empty"
    modern = sum(key in server_ids for key in registry)
    if modern == len(registry):
        return "server"
    if modern == 0:
        return "legacy"
    return "mixed"


def keep_legacy_ids(state, home, scheme):
    if scheme == "mixed":
        raise ValueError(
            "Sidebar project list mixes current database ids and legacy ids. "
            "Repair stopped so Codex does not import the projects a second time.")
    if scheme == "legacy":
        return True
    if scheme == "server" or projects_migrated(state, home):
        return False
    return bool(legacy_maps(state))


def unique_legacy(server_id, state):
    found = []
    for mapping in legacy_maps(state):
        for legacy, modern in mapping.items():
            if modern == server_id and legacy not in found:
                found.append(legacy)
    return found[0] if len(found) == 1 else None


def unique_root(project, registry):
    signature = tuple(sorted(path_key(path) for path in project["rootPaths"]))
    matches = [key for key, entry in registry.items() if root_signature(entry) == signature]
    return matches[0] if len(matches) == 1 else None


def translate_project(source_id, source_state, source_registry, target_registry):
    if not isinstance(source_id, str) or not source_id:
        return None
    if source_id in target_registry:
        return source_id
    for mapping in legacy_maps(source_state):
        modern = mapping.get(source_id)
        if isinstance(modern, str) and modern in target_registry:
            return modern
        for legacy, modern in mapping.items():
            if modern == source_id and legacy in target_registry:
                return legacy
    signature = root_signature(source_registry.get(source_id))
    if not signature:
        return None
    matches = [key for key, entry in target_registry.items() if root_signature(entry) == signature]
    return matches[0] if len(matches) == 1 else None


def section_blobs(state):
    for key, value in state.items():
        if not isinstance(key, str) or not key.startswith("electron-owner-atoms:") or not isinstance(value, dict):
            continue
        groups = value.get(SECTION_ATOM)
        if not isinstance(groups, dict):
            continue
        for account, blob in groups.items():
            if isinstance(account, str) and isinstance(blob, dict) and isinstance(blob.get("sections"), list):
                yield key, account, blob


def ordered_section_names(blob):
    by_id = {section.get("id"): section for section in blob["sections"] if isinstance(section, dict)}
    names = []
    for token in blob.get("sectionOrder") or []:
        if not isinstance(token, str) or not token.startswith("custom:"):
            continue
        section = by_id.get(token.removeprefix("custom:"))
        if isinstance(section, dict) and section.get("name"):
            names.append(section["name"])
    if names:
        return names
    return [section.get("name") for section in blob["sections"]
            if isinstance(section, dict) and section.get("name")]


def sequence_overlap(left, right):
    previous = [0] * (len(right) + 1)
    for item in left:
        current = [0]
        for index, other in enumerate(right, 1):
            current.append(previous[index - 1] + 1 if item == other else max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def candidate_rank(current_owner, current_account, current_names, owner, account, blob):
    names = ordered_section_names(blob)
    overlap = [name for name in names if name in set(current_names)]
    populated = sum(len(section.get("itemKeys") or [])
                    for section in blob["sections"] if isinstance(section, dict))
    if not overlap or populated == 0:
        return None
    score = 10 * len(overlap) + sequence_overlap(names, current_names)
    if owner == current_owner:
        score += 100
    if account == current_account:
        score += 40
    if names == current_names:
        score += 30
    return score


def host_ids(section):
    hosts = section.get("hostSectionIds")
    if not isinstance(hosts, dict):
        return set()
    return {value for value in hosts.values() if isinstance(value, str) and value}


def matching_section(current, blob):
    candidates = [section for section in blob["sections"] if isinstance(section, dict)]
    current_hosts = host_ids(current)
    by_host = [section for section in candidates if current_hosts & host_ids(section)]
    if len(by_host) == 1:
        return by_host[0]
    named = [section for section in candidates
             if section.get("name") == current.get("name") and section.get("itemKeys")]
    return named[0] if named else None


def restored_items(section, source_state, target_registry):
    source_registry = source_state.get("local-projects")
    if not isinstance(source_registry, dict):
        source_registry = {}
    keys = []
    skipped = []
    seen = set()
    for item in section.get("itemKeys") or []:
        if not isinstance(item, str) or not item.startswith(PROJECT_ITEM):
            continue
        source_id = item.removeprefix(PROJECT_ITEM)
        target = translate_project(source_id, source_state, source_registry, target_registry)
        if not target:
            entry = source_registry.get(source_id)
            skipped.append(entry.get("name") if isinstance(entry, dict) and entry.get("name") else source_id)
            continue
        if target in seen:
            continue
        seen.add(target)
        keys.append(PROJECT_ITEM + target)
    return keys, skipped


def best_section_backup(current_owner, current_account, current_names, snapshots):
    best = None
    for index, (label, saved) in enumerate(snapshots):
        for owner, account, blob in section_blobs(saved):
            score = candidate_rank(current_owner, current_account, current_names, owner, account, blob)
            if score is None:
                continue
            rank = (score, -index)
            if best is None or rank > best[0]:
                best = (rank, label, saved, blob)
    return None if best is None else best[1:]


def canonical_state(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_repair(state, home, projects, threads, snapshots):
    if not isinstance(state, dict):
        raise ValueError("Sidebar state is not an object")
    planned = copy.deepcopy(state)
    original = state.get("local-projects")
    if original is None:
        original = {}
    if not isinstance(original, dict):
        raise ValueError("local-projects is not an object")
    server_ids = {project["id"] for project in projects}
    scheme = registry_scheme(original, server_ids)
    keeping_legacy = keep_legacy_ids(state, home, scheme)
    registry = planned.get("local-projects")
    created_registry = registry is None
    if created_registry:
        registry = {}
    elif not isinstance(registry, dict):
        raise ValueError("local-projects is not an object")
    lines = [f"Sidebar id scheme: {'legacy' if keeping_legacy else 'current'} "
             f"({len(projects)} database projects, {len(original)} sidebar projects)."]
    id_for = {}
    added = []
    unmapped = []
    for project in projects:
        if keeping_legacy:
            legacy = unique_legacy(project["id"], state)
            root_match = unique_root(project, registry)
            if legacy and root_match and legacy != root_match:
                unmapped.append(project["name"])
                continue
            sidebar_id = legacy or root_match
            if sidebar_id is None:
                unmapped.append(project["name"])
                continue
        else:
            sidebar_id = project["id"]
        id_for[project["id"]] = sidebar_id
        if sidebar_id not in registry:
            registry[sidebar_id] = sidebar_project(project, sidebar_id)
            added.append(project["name"])
    if created_registry and registry:
        planned["local-projects"] = registry
    if not keeping_legacy and legacy_maps(state):
        planned.pop(LEGACY_MAP, None)
        lines.append("Remove the legacy project id map so Codex does not import these projects again.")
    if added:
        lines.append(f"{len(added)} projects to register:")
        lines.extend(f"  {name}" for name in added)
    if unmapped:
        lines.append("Left unregistered to avoid mixing legacy and current ids:")
        lines.extend(f"  {name}" for name in unmapped)
    order = planned.get("project-order")
    if not isinstance(order, list):
        order = []
    seen = {item for item in order if isinstance(item, str)}
    appended = []
    for project in projects:
        sidebar_id = id_for.get(project["id"])
        if sidebar_id and sidebar_id not in seen:
            appended.append(sidebar_id)
            seen.add(sidebar_id)
    if appended:
        if not isinstance(planned.get("project-order"), list):
            planned["project-order"] = []
        planned["project-order"].extend(appended)
        lines.append(f"{len(appended)} projects to add to the sidebar order.")
    pins = planned.get("pinned-project-ids")
    if not isinstance(pins, list) or not pins:
        for label, saved in snapshots:
            raw = saved.get("pinned-project-ids")
            if not isinstance(raw, list) or not raw:
                continue
            source_registry = saved.get("local-projects")
            if not isinstance(source_registry, dict):
                source_registry = {}
            restored = []
            pin_seen = set()
            for item in raw:
                if not isinstance(item, str):
                    continue
                sidebar_id = translate_project(item, saved, source_registry, registry)
                if sidebar_id and sidebar_id not in pin_seen:
                    restored.append(sidebar_id)
                    pin_seen.add(sidebar_id)
            if restored:
                planned["pinned-project-ids"] = restored
                names = [registry[key].get("name", key) for key in restored]
                lines.append(f"{len(restored)} pins from {label}: " + ", ".join(names))
                break
    assignments = planned.get("thread-project-assignments")
    if assignments is None:
        assignments = {}
    elif not isinstance(assignments, dict):
        raise ValueError("thread-project-assignments is not an object")
    added_threads = []
    for thread, server_id in threads:
        if not isinstance(thread, str) or thread in assignments:
            continue
        sidebar_id = id_for.get(server_id)
        if not sidebar_id:
            continue
        added_threads.append((thread, sidebar_id))
    if added_threads:
        if not isinstance(planned.get("thread-project-assignments"), dict):
            planned["thread-project-assignments"] = assignments
        hosts = planned.get("thread-project-membership-host-ids")
        if not isinstance(hosts, dict):
            hosts = {}
            planned["thread-project-membership-host-ids"] = hosts
        for thread, sidebar_id in added_threads:
            planned["thread-project-assignments"][thread] = {"projectKind": "local", "projectId": sidebar_id}
            hosts.setdefault(thread, "local")
        lines.append(f"{len(added_threads)} chat memberships to restore.")
    restored_sections = False
    for owner, account, blob in section_blobs(planned):
        names = ordered_section_names(blob)
        backup = best_section_backup(owner, account, names, snapshots)
        if backup is None:
            missing = [section.get("name") for section in blob["sections"]
                       if isinstance(section, dict) and not section.get("itemKeys")]
            if any(missing):
                lines.append("No saved section membership for: " + ", ".join(name for name in missing if name))
            continue
        label, saved, source_blob = backup
        for section in blob["sections"]:
            if not isinstance(section, dict):
                continue
            current_items = section.get("itemKeys")
            if isinstance(current_items, list) and current_items:
                continue
            source = matching_section(section, source_blob)
            if source is None:
                lines.append(f"Section {section.get('name', section.get('id'))}: no saved membership in {label}.")
                continue
            keys, skipped = restored_items(source, saved, registry)
            if not keys:
                if skipped:
                    lines.append(f"Section {section.get('name')}: nothing current remains in {label}.")
                continue
            section["itemKeys"] = keys
            restored_sections = True
            visible = [registry[key.removeprefix(PROJECT_ITEM)].get("name", key) for key in keys]
            lines.append(f"Section {section.get('name')} from {label}: " + ", ".join(visible))
            if skipped:
                lines.append("  skipped: " + ", ".join(skipped))
    if restored_sections:
        placed = set()
        for _, _, blob in section_blobs(planned):
            for section in blob["sections"]:
                if not isinstance(section, dict):
                    continue
                for item in section.get("itemKeys") or []:
                    if isinstance(item, str) and item.startswith(PROJECT_ITEM):
                        placed.add(item.removeprefix(PROJECT_ITEM))
        left = [entry.get("name", key) for key, entry in registry.items()
                if key not in placed and isinstance(entry, dict)]
        if left:
            lines.append("Left in Projects: " + ", ".join(left))
    if canonical_state(planned) == canonical_state(state):
        lines.append("Nothing to change.")
        return None, lines
    return planned, lines


def repair(home, apply):
    require_stopped()
    sidebar = home / SIDEBAR
    if not sidebar.is_file():
        raise ValueError(f"Missing sidebar state: {sidebar}")
    state = read_state(sidebar)
    if state is None:
        raise ValueError(f"Unreadable sidebar state: {sidebar}")
    with closing(read_db(home / STATE)) as db:
        projects = database_projects(db)
        threads = [(row["id"], row["project_id"]) for row in database_threads(db)]
    snapshots = []
    for path in state_snapshots(home):
        saved = read_state(path)
        if saved is not None:
            snapshots.append((path.name, saved))
    print(f"Read {len(snapshots)} sidebar backups.")
    updated, lines = plan_repair(state, home, projects, threads, snapshots)
    for line in lines:
        print(line)
    if updated is None:
        return
    if not apply:
        print("Preview only. Use repair --apply to write the sidebar state.")
        return
    require_stopped()
    original = sidebar.read_bytes()
    if read_state(sidebar) != state:
        raise ValueError("Sidebar state changed while it was being read. Quit Codex and run repair again.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    ensure_backup_directory()
    backup = BACKUP_DIR / f"sidebar-repair-backup-{stamp}.json"
    payload = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with backup.open("xb") as stream:
        stream.write(original)
    require_stopped()
    if sidebar.read_bytes() != original:
        raise ValueError("Sidebar state changed. Repair stopped before writing.")
    replace_file(sidebar, content=payload)
    print(f"Sidebar repaired. Backup: {backup}. Open Codex and check Projects and Sections.")


def launch_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cleanup_details = ("Keeping newest import backup:", "Backup directory:", "Temporary file:",
                       "Codex cache:", "Codex cache candidates:", "Codex caches deleted:",
                       "Deleted file:", "Deleted backup directory:", "Preview only.")
    if os.name == "nt":
        from ctypes import wintypes, windll, byref
        user32 = windll.user32
        user32.SetProcessDPIAware()
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("CodexBackupRestore.Desktop")
        work_area = wintypes.RECT()
        if not user32.SystemParametersInfoW(0x0030, 0, byref(work_area), 0):
            work_area = None
    else:
        work_area = None

    class TransferWindow:
        def __init__(self, root):
            self.root = root
            self.running = False
            self.preview_ready = None
            self.events = queue.Queue()
            self.root.title("Codex 备份与恢复")
            surface = "#edf2f8"
            paper = "#ffffff"
            ink = "#182b44"
            muted = "#455a74"
            border = "#bdcbdb"
            accent = "#2456a6"
            banner_color = "#162e50"
            assets = SCRIPT_DIR / "assets"
            icon = assets / "app-icon.ico"
            if os.name == "nt" and icon.is_file():
                self.root.iconbitmap(default=str(icon))
            self.root.configure(background=surface)
            self.root.protocol("WM_DELETE_WINDOW", self.close)
            self.mode = tk.StringVar(value="export")
            self.home = tk.StringVar(value=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            self.archive = tk.StringVar(value=str(DEFAULT_ARCHIVE))
            self.skills = tk.BooleanVar(value=True)
            self.include_cache = tk.BooleanVar(value=True)
            self.keep_existing = tk.BooleanVar(value=False)
            self.status = tk.StringVar(value="就绪")
            style = ttk.Style(root)
            style.theme_use("clam")
            style.configure(".", font=("Microsoft YaHei UI", 10))
            style.configure("TFrame", background=surface)
            style.configure("TLabel", background=surface, foreground=ink)
            style.configure("Banner.TFrame", background=banner_color)
            style.configure("Title.TLabel", background=banner_color, foreground=paper,
                            font=("Microsoft YaHei UI", 17, "bold"))
            style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
            style.configure("Hint.TLabel", foreground=muted)
            style.configure("Done.TLabel", foreground="#17624f")
            style.configure("Error.TLabel", foreground="#a12e37")
            style.configure("TButton", padding=(12, 6), width=0, background=paper, foreground=ink,
                            bordercolor=border, lightcolor=border, darkcolor=border)
            style.map("TButton", background=[("disabled", surface), ("active", "#e0ebfb")],
                      bordercolor=[("focus", accent)])
            style.configure("Primary.TButton", foreground=paper, background=accent,
                            bordercolor=accent, lightcolor=accent, darkcolor=accent)
            style.map("Primary.TButton", background=[("disabled", "#d8e2ef"), ("active", "#1a447f")],
                      foreground=[("disabled", "#536376")])
            style.configure("Mode.Toolbutton", padding=(10, 8), background="#e1e9f3",
                            foreground=ink, bordercolor=border, relief="flat", anchor="center")
            style.map("Mode.Toolbutton", background=[("disabled", "#e1e9f3"),
                                                     ("selected", accent), ("active", "#d2e1f5")],
                      foreground=[("disabled", "#536376"), ("selected", paper)],
                      relief=[("focus", "solid"), ("!focus", "flat")])
            style.configure("TEntry", padding=(8, 5), fieldbackground=paper, foreground=ink,
                            bordercolor=border, lightcolor=border, darkcolor=border)
            style.map("TEntry", bordercolor=[("focus", accent)])
            style.configure("TCheckbutton", background=surface)
            style.configure("Horizontal.TProgressbar", background=accent, troughcolor="#d9e4f1",
                            lightcolor=accent, darkcolor=accent, bordercolor="#d9e4f1")
            self.controls = []
            self.root.columnconfigure(0, weight=1)
            self.root.rowconfigure(1, weight=1)
            banner = ttk.Frame(root, style="Banner.TFrame", padding=(22, 14))
            banner.grid(row=0, column=0, sticky="ew")
            emblem = assets / "app-icon.png"
            self.emblem = tk.PhotoImage(file=str(emblem)) if emblem.is_file() else None
            if self.emblem:
                ttk.Label(banner, image=self.emblem, style="Title.TLabel").pack(side="left", padx=(0, 12))
            ttk.Label(banner, text="Codex 备份与恢复", style="Title.TLabel").pack(side="left")
            body = ttk.Frame(root, padding=(22, 16))
            body.grid(row=1, column=0, sticky="nsew")
            body.columnconfigure(0, weight=1)
            body.rowconfigure(8, weight=1)
            modes = ttk.Frame(body)
            modes.grid(row=1, sticky="ew", pady=(0, 14))
            choices = (("export", "导出会话"), ("import", "导入会话"),
                       ("cleanup", "清理旧文件"), ("repair", "修复侧栏"))
            for column, (mode, label) in enumerate(choices):
                modes.columnconfigure(column, weight=1, uniform="modes")
                button = ttk.Radiobutton(modes, text=label, value=mode, variable=self.mode,
                                         style="Mode.Toolbutton")
                button.grid(row=0, column=column, sticky="ew", padx=(4 if column else 0, 0))
                self.controls.append(button)
            paths = ttk.Frame(body)
            paths.grid(row=2, sticky="ew")
            paths.columnconfigure(1, weight=1)
            self.path_field(paths, 0, "Codex 数据目录", self.home, self.choose_home)
            self.archive_row = self.path_field(paths, 1, "会话 ZIP", self.archive, self.choose_archive)
            self.options = ttk.Frame(body)
            self.options.grid(row=3, sticky="ew", pady=(8, 0))
            skills = ttk.Checkbutton(self.options, text="同时迁移用户 Skills", variable=self.skills)
            skills.grid(row=0, column=0, sticky="w")
            self.controls.append(skills)
            self.keep_button = ttk.Checkbutton(self.options, text="历史分叉时保留本机版本",
                                               variable=self.keep_existing)
            self.keep_button.grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.controls.append(self.keep_button)
            self.cache_button = ttk.Checkbutton(body, text="同时清理 Codex 应用和插件缓存",
                                                variable=self.include_cache)
            self.cache_button.grid(row=3, sticky="w", pady=(8, 0))
            self.controls.append(self.cache_button)
            self.hint = ttk.Label(body, style="Hint.TLabel", justify="left", wraplength=520)
            self.hint.grid(row=4, sticky="ew", pady=(8, 12))
            ttk.Separator(body).grid(row=5, sticky="ew")
            log_header = ttk.Frame(body)
            log_header.grid(row=7, sticky="ew", pady=(12, 6))
            ttk.Label(log_header, text="运行记录", style="Section.TLabel").pack(side="left")
            self.details_button = ttk.Button(log_header, text="查看明细", command=self.toggle_details,
                                             state="disabled")
            self.details_button.pack(side="right")
            log_frame = ttk.Frame(body)
            log_frame.grid(row=8, sticky="nsew")
            log_frame.columnconfigure(0, weight=1)
            log_frame.rowconfigure(0, weight=1)
            self.log = tk.Text(log_frame, wrap="word", state="disabled", background=paper,
                               foreground=ink, font=("Microsoft YaHei UI", 10),
                               width=1, height=5, relief="flat", borderwidth=0,
                               highlightthickness=1, highlightbackground=border,
                               highlightcolor=accent, selectbackground="#cfe1fe", selectforeground=ink,
                               padx=12, pady=10, spacing1=2)
            self.log.grid(row=0, column=0, sticky="nsew")
            self.log.tag_configure("details", elide=True)
            self.log.tag_configure("summary", foreground="#153e76",
                                   font=("Microsoft YaHei UI", 11, "bold"), spacing3=5)
            scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            self.log.configure(yscrollcommand=scrollbar.set)
            footer = ttk.Frame(body)
            footer.grid(row=9, sticky="ew", pady=(12, 0))
            footer.columnconfigure(0, weight=1)
            self.status_label = ttk.Label(footer, textvariable=self.status, style="Hint.TLabel")
            self.status_label.grid(row=0, column=0, sticky="w")
            self.progress = ttk.Progressbar(footer, mode="indeterminate", length=52)
            self.progress.grid(row=0, column=1, padx=(4, 12))
            self.preview_button = ttk.Button(footer, text="预览", command=lambda: self.start(True))
            self.preview_button.grid(row=0, column=2, padx=(0, 8))
            self.run_button = ttk.Button(footer, text="开始导出", style="Primary.TButton",
                                         command=lambda: self.start(False))
            self.run_button.grid(row=0, column=3)
            self.controls.extend((self.preview_button, self.run_button))
            self.mode.trace_add("write", self.mode_changed)
            for variable in (self.home, self.archive, self.skills, self.include_cache):
                variable.trace_add("write", self.selection_changed)
            body.bind("<Configure>", lambda event: self.hint.configure(wraplength=max(1, event.width - 44)))
            self.selection_changed()
            scale = float(root.tk.call("tk", "scaling")) / (96 / 72)
            left, top, right, bottom = ((work_area.left, work_area.top, work_area.right, work_area.bottom)
                                        if work_area else (0, 0, root.winfo_screenwidth(), root.winfo_screenheight()))
            width = min(round(760 * scale), right - left - 32)
            height = min(round(560 * scale), bottom - top - 64)
            root.minsize(min(round(600 * scale), width), min(round(470 * scale), height))
            root.geometry(f"{width}x{height}+{left + (right - left - width) // 2}"
                          f"+{top + (bottom - top - height) // 2}")
            self.write_log("需要退出 Codex 时，窗口会先询问是否自动结束相关进程。\n")
            self.root.after(100, self.poll)

        def path_field(self, frame, row, label, variable, command):
            caption = ttk.Label(frame, text=label)
            caption.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            entry = ttk.Entry(frame, textvariable=variable, width=1)
            entry.grid(row=row, column=1, sticky="nsew", pady=4)
            button = ttk.Button(frame, text="浏览…", command=command)
            button.grid(row=row, column=2, sticky="ns", padx=(8, 0), pady=4)
            self.controls.extend((entry, button))
            return caption, entry, button

        def choose_home(self):
            value = filedialog.askdirectory(parent=self.root, title="选择 Codex 数据目录",
                                             initialdir=self.home.get())
            if value:
                self.home.set(value)

        def choose_archive(self):
            directory = Path(self.archive.get()).parent
            options = dict(parent=self.root, filetypes=[("ZIP 文件", "*.zip")],
                           initialdir=str(directory if directory.is_dir() else SCRIPT_DIR))
            if self.mode.get() == "export":
                value = filedialog.asksaveasfilename(
                    title="导出到 ZIP", defaultextension=".zip", confirmoverwrite=False,
                    initialfile=Path(self.archive.get()).name, **options)
            else:
                value = filedialog.askopenfilename(title="选择会话 ZIP", **options)
            if value:
                self.archive.set(value)

        def mode_changed(self, *_):
            if self.archive.get() in {str(DEFAULT_ARCHIVE), str(LEGACY_ARCHIVE)}:
                default = str(default_archive(self.mode.get()))
                if self.archive.get() != default:
                    self.archive.set(default)
            self.selection_changed()
            self.clear_log()
            self.status.set("就绪")
            self.status_label.configure(style="Hint.TLabel")

        def selection_changed(self, *_):
            self.preview_ready = None
            self.refresh_controls()

        def refresh_controls(self):
            for widget in self.controls:
                widget.configure(state="disabled" if self.running else "normal")
            mode = self.mode.get()
            local_mode = mode in ("cleanup", "repair")
            if mode == "cleanup":
                self.cache_button.grid()
                if not self.details_button.winfo_manager():
                    self.details_button.pack(side="right")
            else:
                self.cache_button.grid_remove()
                self.details_button.pack_forget()
            if local_mode:
                for widget in self.archive_row:
                    widget.grid_remove()
                self.options.grid_remove()
            else:
                for widget in self.archive_row:
                    widget.grid()
                self.options.grid()
            if mode == "import":
                self.keep_button.grid()
            else:
                self.keep_button.grid_remove()
            if self.running:
                self.progress.grid()
            else:
                self.progress.grid_remove()
            labels = {"export": "开始导出", "import": "开始导入",
                      "cleanup": "清理预览中的文件", "repair": "修复预览中的侧栏"}
            self.run_button.configure(text=labels[mode])
            if local_mode and self.preview_ready != (mode, self.home.get()):
                self.run_button.configure(state="disabled")
            hints = {
                "export": "备份项目会话、Section、完整分页历史和全局 AGENTS 规则。导出路径应选择一个新 ZIP。",
                "import": "单边续聊时保留较长历史。两边历史分叉时，默认备份本机记录后用 ZIP 覆盖。",
                "cleanup": "只清理超过 7 天的导入备份和全局状态写入临时文件，保留最新一份导入备份。请先预览。",
                "repair": "从数据库和旧的侧栏备份恢复被清空的项目、置顶和 Section 成员。不猜测新项目的分组。请先预览。",
            }
            self.hint.configure(text=hints[mode])

        def write_log(self, text):
            self.log.configure(state="normal")
            detail = self.mode.get() == "cleanup" and text.startswith(cleanup_details)
            summary = self.mode.get() == "cleanup" and text.startswith(("Cleanup preview:", "Cleanup result:"))
            tags = ("details",) if detail else ("summary",) if summary else ()
            self.log.insert("end", text, tags)
            if detail:
                self.details_button.configure(state="normal")
            self.log.see("end")
            self.log.configure(state="disabled")

        def toggle_details(self):
            expanded = self.root.tk.getboolean(self.log.tag_cget("details", "elide"))
            self.log.tag_configure("details", elide=not expanded)
            self.details_button.configure(text="收起明细" if expanded else "查看明细")
            self.log.see("1.0" if expanded else "end")

        def clear_log(self):
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self.log.tag_configure("details", elide=True)
            self.details_button.configure(text="查看明细", state="disabled")

        def start(self, preview):
            if self.running:
                return
            mode = self.mode.get()
            if not self.home.get().strip():
                messagebox.showerror("路径不完整", "请选择 Codex 数据目录。", parent=self.root)
                return
            if mode in ("export", "import") and not self.archive.get().strip():
                messagebox.showerror("路径不完整", "请选择会话 ZIP。", parent=self.root)
                return
            apply = mode in ("cleanup", "repair") and not preview
            if apply:
                if self.preview_ready != (mode, self.home.get()):
                    return
                prompts = {
                    "cleanup": ("确认清理", "将重新检查并永久删除符合条件的旧文件。\n"
                                "删除的旧备份将失去对应的回退能力。确认继续？"),
                    "repair": ("确认修复", "将按预览写回侧栏状态，并先保存一份备份。\n确认继续？"),
                }
                title, question = prompts[mode]
                if not messagebox.askyesno(title, question, parent=self.root):
                    return
            home = self.home.get()
            command = [sys.executable, "-u", str(Path(__file__).resolve()), mode,
                       "--codex-home", home]
            if mode in ("export", "import"):
                command.insert(4, self.archive.get())
                if not self.skills.get():
                    command.append("--exclude-skills")
                if mode == "import" and self.keep_existing.get():
                    command.append("--keep-existing")
                if preview:
                    command.append("--preview")
            elif apply:
                command.append("--apply")
            if mode == "cleanup" and self.include_cache.get():
                command.append("--include-cache")
            self.running = True
            self.status.set("正在预览…" if preview else "正在处理…")
            self.status_label.configure(style="Hint.TLabel")
            self.clear_log()
            self.write_log(("预览" if preview else "执行") +
                           {"export": "导出", "import": "导入", "cleanup": "清理", "repair": "修复"}[mode] + "\n")
            self.refresh_controls()
            self.progress.start(12)
            threading.Thread(target=self.worker, args=(command, mode, preview, home), daemon=True).start()

        def worker(self, command, mode, preview, home, approved=None):
            try:
                if approved is not None:
                    close_codex_processes(approved)
                    self.events.put(("log", "相关进程已退出，继续操作。\n"))
                elif mode in ("import", "repair") or not preview:
                    active = active_codex_processes()
                    if active:
                        self.events.put(("confirm_close", (command, mode, preview, home, active)))
                        return
                environment = os.environ.copy()
                environment["PYTHONIOENCODING"] = "utf-8"
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace", env=environment,
                                      creationflags=subprocess.CREATE_NO_WINDOW) as process:
                    for line in process.stdout:
                        self.events.put(("log", line))
                    code = process.wait()
                self.events.put(("done", (code, mode, preview, home)))
            except Exception as error:
                self.events.put(("log", str(error) + "\n"))
                self.events.put(("done", (1, mode, preview, home)))

        def poll(self):
            try:
                while True:
                    kind, value = self.events.get_nowait()
                    if kind == "log":
                        self.write_log(value)
                    elif kind == "confirm_close":
                        active = value[-1]
                        names = "、".join(sorted(set(active.values())))
                        if messagebox.askyesno(
                            "退出 Codex 后继续",
                            f"检测到正在运行的进程：{names}\n\n"
                            "是否强制结束这些进程并继续？运行中的任务可能中断。"
                            "请先保存尚未完成的工作。",
                            default=messagebox.NO, parent=self.root,
                        ):
                            self.status.set("正在退出 Codex…")
                            threading.Thread(target=self.worker, args=value, daemon=True).start()
                        else:
                            self.running = False
                            self.progress.stop()
                            self.status.set("已取消")
                            self.refresh_controls()
                    else:
                        code, mode, preview, home = value
                        self.running = False
                        self.progress.stop()
                        self.preview_ready = None
                        if code == 0:
                            self.status.set("预览完成" if preview else "操作完成")
                            self.status_label.configure(style="Done.TLabel")
                            if mode in ("cleanup", "repair") and preview:
                                self.preview_ready = (mode, home)
                        else:
                            self.status.set("操作未完成，请查看运行记录")
                            self.status_label.configure(style="Error.TLabel")
                        self.refresh_controls()
            except queue.Empty:
                pass
            self.root.after(100, self.poll)

        def close(self):
            if self.running:
                messagebox.showinfo("任务进行中", "请等待任务结束后再关闭窗口。", parent=self.root)
            else:
                self.root.destroy()

    root = tk.Tk()
    TransferWindow(root)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("export", "import", "cleanup", "repair", "gui"))
    parser.add_argument("archive", nargs="?", type=Path,
                        help="ZIP path (default: backups/codex-chats.zip beside this script)")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get(
        "CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--apply", action="store_true",
                        help="Write cleanup deletions or the sidebar repair (default: preview only)")
    parser.add_argument("--include-cache", action="store_true",
                        help="Also clean old Codex app and plugin metadata caches (cleanup only)")
    skill_options = parser.add_mutually_exclusive_group()
    skill_options.add_argument("--include-skills", dest="include_skills", action="store_true",
                               help="Transfer user skills (default)")
    skill_options.add_argument("--exclude-skills", dest="include_skills", action="store_false",
                               help="Skip user skills during export or import")
    parser.set_defaults(include_skills=True)
    parser.add_argument("--provider", help="Optional target model_provider for imported thread rows")
    parser.add_argument("--keep-existing", action="store_true",
                        help="Keep destination history when conversations have diverged (import only)")
    if len(sys.argv) == 1:
        launch_gui()
        return
    args = parser.parse_args()
    if args.include_cache and args.mode != "cleanup":
        parser.error("--include-cache is only valid with cleanup")
    if args.keep_existing and args.mode != "import":
        parser.error("--keep-existing is only valid with import")
    if args.apply and args.mode not in ("cleanup", "repair"):
        parser.error("--apply is only valid with cleanup or repair")
    if args.apply and args.preview:
        parser.error("--apply and --preview cannot be combined")
    if args.mode == "gui":
        launch_gui()
        return
    home = args.codex_home.resolve()
    archive = args.archive or default_archive(args.mode)
    if args.mode == "export" and archive.absolute() == DEFAULT_ARCHIVE and is_link(BACKUP_DIR):
        raise ValueError(f"Backup directory is a link: {BACKUP_DIR}")
    archive = archive.resolve()
    if args.mode == "export":
        export(home, archive, args.preview, args.include_skills)
    elif args.mode == "import":
        import_bundle(home, archive, args.preview, args.provider, args.include_skills, args.keep_existing)
    elif args.mode == "cleanup":
        cleanup(home, args.apply, args.include_cache)
    else:
        repair(home, args.apply)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, EOFError, zstd.ZstdError, sqlite3.Error,
            zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        sys.exit(f"Stopped: {exc}")
