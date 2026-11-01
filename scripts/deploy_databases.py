"""Field deployment of the archive databases: one PostgreSQL cluster per storage role, on the volume the site names for it
(终版 §13.5: the normal and the emergency database, each on a volume of its own).

    python scripts/deploy_databases.py plan      what would be done, and what stands in the way; changes nothing (the default)
    python scripts/deploy_databases.py install   folders, clusters, databases with their schema, then the connection file, the
                                                 only administrator (in accounts.json beside the connection file, with its record
                                                 in the primary database; its credential in initial-credentials.json, for the owner, and
                                                 the hash of that initial password, the fallback, in admin-fallback.json) and
                                                 the storage paths of the deployment configuration
    python scripts/deploy_databases.py register  (elevated PowerShell) every cluster becomes an automatic Windows service that is
                                                 restarted when it stops by itself; then the task HTSG-Backend runs the supervisor
                                                 (scripts/supervise.py) as SYSTEM at boot and every five minutes; all are started
    python scripts/deploy_databases.py status    folders, clusters, services, connections, the backend task

All four read .local/deploy.json (or --input FILE), which names volumes, not folders - a drive letter is enough:

    {"volumes": {"primary": "D:", "emergency": "C:"},
     "pg_bin": "C:/Program Files/PostgreSQL/18/bin",
     "config": "config/production.json"}

Optional: "folder" (default "HTSG"), "ports" (default 55440-55441),
"database_file" (default .local/database.json, where the backend reads its connections).  A volume may also be given as an
absolute local folder ("E:/Data").  On each volume the role lives in <volume>\\<folder>\\<role>: that folder is the storage path
written into the configuration and its `pgdata` holds the cluster, so the disk the backend measures and the disk the data is on are
the same by construction.  Two roles never share a volume.

Nothing is ever deleted.  A folder that holds anything but this script's own cluster is not used, an existing connection file that
names other databases is not replaced, and a second run skips what is already there.  The backend never creates any of this.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import ntpath
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROLES = ("primary", "emergency")
PORTS = {"primary": 55440, "emergency": 55441}
PG_MAJOR = 18  # the PostgreSQL the archive was developed and tested with
MIN_FREE = 20 * 1024**3
INSTALL_STATEMENT_SECONDS = 600  # laying down the schema once, on a disk that may be slow at first (修复轮17)
INSTALL_LOCK_SECONDS = 60  # nothing else uses the new databases during install
SERVICE_ACCOUNT = r"NT AUTHORITY\NetworkService"
SERVICE_SID = "*S-1-5-20"  # NetworkService under any language edition of Windows
# What the site reads (修复轮16): the roles, states and volume kinds in Chinese; the values compared in the code stay as they are.
ROLE_TEXT = {"primary": "正常库", "emergency": "应急库"}
STATE_TEXT = {"existing cluster": "已有实例", "new cluster": "新建实例", "not registered": "未注册", "registered": "已注册",
              "RUNNING": "运行中", "STOPPED": "已停止", "START_PENDING": "正在启动", "STOP_PENDING": "正在停止",
              "Ready": "就绪", "Running": "运行中", "Disabled": "已禁用"}  # fmt: skip
KIND_TEXT = {"removable": "可移动盘", "fixed": "本地硬盘", "network": "网络盘", "cd-rom": "光驱", "ram disk": "内存盘", "unknown": "类型不明"}


def zh(word: str) -> str:
    return ROLE_TEXT.get(word) or STATE_TEXT.get(word) or KIND_TEXT.get(word) or word


TASK = "HTSG-Backend"  # the name the superseded scripts/history/register_task.ps1 used: registering here replaces that task
# Appended to postgresql.conf of a new cluster.  max_connections: the backend keeps one connection per worker thread that has read the
# history (up to the size of the default thread pool, 20 on a 16-thread CPU) plus the writer's; 60 leaves room for maintenance.
SETTINGS = """
# HTSG field deployment (scripts/deploy_databases.py)
listen_addresses = '127.0.0.1'
port = {port}
max_connections = 60
shared_buffers = '256MB'
work_mem = '4MB'
max_wal_size = '1GB'
min_wal_size = '80MB'
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%a.log'
log_rotation_age = '1d'
log_truncate_on_rotation = on
"""


@dataclass(frozen=True)
class Role:
    name: str
    root: str  # the volume or folder as the site wrote it, normalised
    folder: str  # <root>\<folder>\<name>: the storage path of the configuration
    port: int

    @property
    def data(self) -> str:
        return ntpath.join(self.folder, "pgdata")

    @property
    def service(self) -> str:
        return f"HTSG-PostgreSQL-{self.name}"

    def dsn(self, password: str, dbname: str = "htsg") -> str:
        return f"postgresql://htsg:{password}@127.0.0.1:{self.port}/{dbname}"


# ----------------------------------------------------------------------------------------------- paths and volumes


def normalize(text) -> str:
    """A drive letter or an absolute local folder as the site writes it ("D", "d:", "D:/", "E:\\Data") in one spelling."""
    value = str(text).strip().replace("/", "\\")
    if re.fullmatch(r"[A-Za-z]:?\\?", value):
        return value[0].upper() + ":\\"
    drive, tail = ntpath.splitdrive(value)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\"):
        raise ValueError(f"{text!r} 既不是盘符也不是本机的绝对路径（不接受网络路径）")
    return drive.upper() + ntpath.normpath(tail)


def volume(path: str) -> dict:
    """The volume an existing path lies on, by its GUID name (two letters or a mount folder of one disk are one volume), its kind
    of drive and its free space."""
    kernel = ctypes.windll.kernel32
    mount = ctypes.create_unicode_buffer(261)
    if not kernel.GetVolumePathNameW(path, mount, 261):
        raise OSError(f"找不到 {path} 所在的卷")
    guid = ctypes.create_unicode_buffer(64)
    ident = guid.value if kernel.GetVolumeNameForVolumeMountPointW(mount.value, guid, 64) else mount.value
    kind = {2: "removable", 3: "fixed", 4: "network", 5: "cd-rom", 6: "ram disk"}.get(kernel.GetDriveTypeW(mount.value), "unknown")
    usage = shutil.disk_usage(path)
    return {"mount": mount.value, "id": ident.lower(), "kind": kind, "free": usage.free, "total": usage.total}


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def cluster_port(data: str) -> int | None:
    """The port an existing cluster is configured for (the last `port =` line wins, as in PostgreSQL)."""
    found = None
    for name in ("postgresql.conf", "postgresql.auto.conf"):
        conf = Path(data) / name
        if conf.exists():
            for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"\s*port\s*=\s*'?(\d+)'?", line)
                if match:
                    found = int(match.group(1))
    return found


# ------------------------------------------------------------------------------------------------------- the plan


def read_input(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(f"没有部署文件 {path}：写法见 scripts/deploy_databases.py 开头（一键安装会自动写它）。") from None
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} 不是有效的 JSON：{error}") from None
    if not isinstance(raw, dict) or set(raw) - {"volumes", "pg_bin", "config", "database_file", "folder", "ports"}:
        raise SystemExit(f"{path}：有不认识的键；只能有 volumes、pg_bin、config、database_file、folder、ports")
    volumes = raw.get("volumes")
    if not isinstance(volumes, dict) or set(volumes) != set(ROLES):
        raise SystemExit(f'{path}："volumes" 只能且必须有 "primary"（正常库）和 "emergency"（应急库）')
    for key in ("pg_bin", "config"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise SystemExit(f'{path}: "{key}" is required')
    folder = raw.get("folder", "HTSG")
    if not isinstance(folder, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", folder):
        raise SystemExit(f'{path}: "folder" must be a plain name of letters, digits, - or _')
    if not isinstance(raw.get("ports", {}), dict):
        raise SystemExit(f'{path}: "ports" must be an object such as {{"primary": 55440}}')
    ports = {**PORTS, **raw.get("ports", {})}
    if any(not isinstance(ports[r], int) or not 1024 <= ports[r] <= 65535 for r in volumes) or len({ports[r] for r in volumes}) < len(volumes):
        raise SystemExit(f'{path}: "ports" must be distinct numbers from 1024 to 65535')
    resolve = lambda p: Path(p) if Path(p).is_absolute() else ROOT / p  # noqa: E731
    return {
        "volumes": volumes, "folder": folder, "ports": ports, "pg_bin": Path(raw["pg_bin"]), "input": path.resolve(),
        "config": resolve(raw["config"]), "database_file": resolve(raw.get("database_file", ".local/database.json")),
    }  # fmt: skip


def plan(inp: dict, volume_of=volume) -> tuple[list[Role], dict, list[str], list[str]]:
    """(roles, facts per role, problems, notes).  Problems stop `install`; notes are only said.  Reads, never writes."""
    from htsg.config import Settings

    roles, facts, problems, notes = [], {}, [], []
    for name in (r for r in ROLES if r in inp["volumes"]):
        try:
            root = normalize(inp["volumes"][name])
        except ValueError as error:
            problems.append(f"{zh(name)}：{error}")
            continue
        role = Role(name, root, ntpath.join(root, inp["folder"], name), inp["ports"][name])
        roles.append(role)
        if not os.path.isdir(root):
            problems.append(f"{zh(name)}：{root} 不存在——盘接上了吗？盘符对吗？")
            continue
        if not role.folder.isascii():
            problems.append(f"{zh(name)}：{role.folder} 含中文或其他非英文字符，PostgreSQL 在 Windows 上不能可靠处理")
        try:
            info = facts[name] = volume_of(root)
        except OSError as error:
            problems.append(f"{zh(name)}：读不出 {root} 所在的卷（{error}）")
            continue
        if info["kind"] == "network":
            problems.append(f"{zh(name)}：{root} 是网络盘；归档要用本机硬盘")
        data = Path(role.data)
        if (data / "PG_VERSION").exists():
            info["state"] = "existing cluster"
            if cluster_port(role.data) != role.port:
                problems.append(f"{zh(name)}：{role.data} 里已有的实例端口是 {cluster_port(role.data)}，不是 {role.port}")
        elif data.exists() and any(data.iterdir()):
            problems.append(f"{zh(name)}：{role.data} 不是空的，里面也不是本程序的数据库实例；不动它")
        else:
            info["state"] = "new cluster"
            if not port_free(role.port):
                problems.append(f"{zh(name)}：端口 {role.port} 已被占用；在部署文件的 \"ports\" 里换一个")
        if info["free"] < MIN_FREE:
            notes.append(f"{zh(name)}：{info['mount']} 只剩 {info['free'] / 1024**3:.1f} GiB")
    seen = {}
    for role in roles:
        ident = facts.get(role.name, {}).get("id")
        if ident is not None and ident in seen:
            problems.append(f"{zh(seen[ident])}和{zh(role.name)}在同一块盘上（{facts[role.name]['mount']}）：两个库要各用一块盘")
        seen.setdefault(ident, role.name)
    for tool in ("initdb.exe", "pg_ctl.exe"):
        if not (inp["pg_bin"] / tool).exists():
            problems.append(f"数据库软件：{inp['pg_bin']} 里没有 {tool}")
    if (inp["pg_bin"] / "initdb.exe").exists():
        out = subprocess.run([str(inp["pg_bin"] / "initdb.exe"), "--version"], capture_output=True, text=True).stdout
        found = re.search(r"\)\s*(\d+)\.", out)
        if not found or int(found.group(1)) != PG_MAJOR:
            problems.append(f"数据库软件：要 PostgreSQL {PG_MAJOR}（测试过的版本），找到的是 {out.strip() or '（无）'}")
    reserve = None
    try:
        checked = Settings.model_validate(with_storage(json.loads(inp["config"].read_text(encoding="utf-8-sig")), roles))
        reserve = checked.storage.emergency_reserve_bytes
    except FileNotFoundError:
        problems.append(f"配置：{inp['config']} 不存在")
    except ValueError as error:  # JSON or validation
        problems.append(f"配置：{inp['config']} 加上这两个存储路径后不能通过校验：{str(error).splitlines()[0][:160]}")
    existing = read_connections(inp["database_file"])
    if existing is not None:
        password = existing.get("password", "")
        wanted = {role.name: role.dsn(password) for role in roles}
        if {k: v for k, v in existing.items() if k != "password"} != wanted:
            problems.append(f"连接文件：{inp['database_file']} 已存在且指向别的数据库；不替换它")
    elif any(facts.get(r.name, {}).get("state") == "existing cluster" for r in roles):
        problems.append(f"连接文件：数据库实例已有，但存放其口令的 {inp['database_file']} 不在了；先把它恢复回来")
    emergency = facts.get("emergency")
    if emergency and reserve is not None and emergency["free"] < reserve:
        notes.append(
            f"应急库：所在盘剩 {emergency['free'] / 1024**3:.0f} GiB，少于应急库要预留的 {reserve / 1024**3:.0f} GiB："
            "转到应急库时数据会写不进去"
        )
    return roles, facts, problems, notes


def with_storage(config: dict, roles: list[Role]) -> dict:
    """The deployment configuration with the storage paths of these roles; nothing else in it is touched."""
    storage = dict(config.get("storage", {}))
    for role in roles:
        storage[f"{role.name}_path"] = role.folder.replace("\\", "/")
    return {**config, "storage": storage}


def read_connections(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {"unreadable": True}


def show(roles, facts, problems, notes):
    for role in roles:
        info = facts.get(role.name, {})
        where = f"{info['mount']} {zh(info['kind'])}，剩 {info['free'] / 1024**3:.0f} GiB" if info else "卷不明"
        print(f"{zh(role.name)}  {role.folder}  端口 {role.port}  {zh(info.get('state', '-'))}（{where}）")
    for note in notes:
        print("提示：", note)
    for problem in problems:
        print("问题：", problem)
    print("检查通过，可以安装。" if not problems else f"有 {len(problems)} 个问题：什么都没有改动。")


# ------------------------------------------------------------------------------------------------------ the actions


def run(args, what):
    done = subprocess.run([str(a) for a in args], capture_output=True)
    if done.returncode:
        text = (done.stdout + done.stderr).decode("utf-8", errors="replace").strip()
        raise SystemExit(f"{what}失败（{done.returncode}）：{text[-600:]}")
    return done


def write_atomically(path: Path, text: str):
    """Written next to the target, flushed to the disk, then put in its place: a power cut leaves the old or the new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def start(inp, role):
    """The server pg_ctl leaves running inherits its output handles, so they must not be pipes: a capture would wait for an end of
    file that never comes.  Why a start failed is in the server's own log."""
    log = Path(role.folder) / "deploy-start.log"
    done = subprocess.run(
        [str(inp["pg_bin"] / "pg_ctl.exe"), "-D", role.data, "-l", str(log), "-w", "-t", "60", "start"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    if done.returncode:
        tail = log.read_text(encoding="utf-8", errors="replace")[-600:] if log.exists() else ""
        raise SystemExit(f"启动{zh(role.name)}失败（{done.returncode}）：{tail}")


def running(inp, role) -> bool:
    return subprocess.run([str(inp["pg_bin"] / "pg_ctl.exe"), "-D", role.data, "status"], capture_output=True).returncode == 0


def password_folder() -> Path:
    """Where initdb reads the password from.  Not mkdtemp: since Python 3.13 its folder admits only its creator, and when the
    installer runs elevated that is the Administrators group - initdb, which drops its administrator rights before it runs,
    could not read the password (rehearsal 2026-11-01).  A plain folder inherits the temporary folder's access, the user's own."""
    folder = Path(tempfile.gettempdir()) / f"htsg-deploy-{secrets.token_hex(8)}"
    folder.mkdir()
    return folder


def install(inp: dict, volume_of=volume):
    import psycopg

    from htsg.archive import ArchiveStore, SchemaError
    from htsg.cli import init_accounts
    from htsg.config import Settings

    roles, facts, problems, notes = plan(inp, volume_of)
    show(roles, facts, problems, notes)
    if problems:
        raise SystemExit(1)
    existing = read_connections(inp["database_file"])
    password = existing["password"] if existing else secrets.token_urlsafe(32)
    config_raw = json.loads(inp["config"].read_text(encoding="utf-8-sig"))
    updated = with_storage(config_raw, roles)
    settings = Settings.model_validate(updated)
    # 修复轮17 (site test 2026-11-01): the schema is laid down once, on a disk that may be slow on its first writes, with
    # nothing else running - the backend's 3 s statement limit would cancel it there.  Install has limits of its own.
    timing = settings.timing.model_copy(update={"db_statement_timeout": INSTALL_STATEMENT_SECONDS, "db_lock_timeout": INSTALL_LOCK_SECONDS})
    connections = {role.name: role.dsn(password) for role in roles} | {"password": password}
    if existing != connections:
        # The password is on disk before any cluster is made with it: a run that stops half way leaves clusters the next run
        # opens and goes on with, never one whose password nobody knows (site test 2026-11-01).
        write_atomically(inp["database_file"], json.dumps(connections))
        print(f"连接信息已写入 {inp['database_file']}（不显示）")
    started = []
    try:
        for role in roles:
            os.makedirs(role.folder, exist_ok=True)
            if facts[role.name]["state"] == "new cluster":
                secret = password_folder() / "pw"
                try:
                    secret.write_text(password, encoding="utf-8")
                    run([inp["pg_bin"] / "initdb.exe", "-D", role.data, "-U", "htsg", "--pwfile", secret,
                         "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"], f"创建{zh(role.name)}实例")  # fmt: skip
                finally:
                    secret.unlink(missing_ok=True)
                    secret.parent.rmdir()
                with open(Path(role.data) / "postgresql.conf", "a", encoding="utf-8") as f:
                    f.write(SETTINGS.format(port=role.port))
                print(f"{zh(role.name)}：实例已建在 {role.data}")
            if not running(inp, role):
                start(inp, role)
                started.append(role)
            with psycopg.connect(role.dsn(password, "postgres"), autocommit=True, connect_timeout=10) as conn:
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname='htsg'").fetchone():
                    conn.execute("CREATE DATABASE htsg")
            store = ArchiveStore(role.dsn(password), timing)
            try:
                store.initialize(settings)
            except SchemaError as error:
                raise SystemExit(f"{zh(role.name)}：数据库 htsg 不能用（{error}）；之后的步骤都没有做") from error
            finally:
                store.close()
            print(f"{zh(role.name)}：数据库 htsg 已是当前归档版本")
        # 修复轮02: the deployment never reaches its first start without an account; read-only users are added on the page
        saved = init_accounts(inp["database_file"].with_name("accounts.json"), connections["primary"], timing)
        print(f"管理员账户已建：初始口令在 {saved}，交给业主离线保管后从这台电脑上删掉该文件；初始口令永久有效，是兜底凭据（不显示）"
              if saved else "管理员账户：已经有了")
        if updated != config_raw:
            backup = inp["config"].with_name(f"{inp['config'].name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(inp["config"], backup)
            write_atomically(inp["config"], json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
            print(f"存储路径已写入 {inp['config']}（原文件留底为 {backup.name}）")
    finally:
        for role in started:  # the services take over from here; a cluster this run did not start is left running
            subprocess.run([str(inp["pg_bin"] / "pg_ctl.exe"), "-D", role.data, "-w", "-m", "fast", "stop"], capture_output=True)
    print("下一步：注册数据库服务与开机任务（一键安装会接着自动做；手动做时在管理员 PowerShell 里运行 deploy_databases.py register）")


def service_state(name: str) -> str:
    done = subprocess.run(["sc", "query", name], capture_output=True)
    if done.returncode:
        return "not registered"
    text = done.stdout.decode("utf-8", errors="replace")
    return next((s for s in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING") if s in text), "registered")


def register(inp: dict):
    import psycopg

    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise SystemExit("注册服务需要管理员权限：用“以管理员身份运行”的 PowerShell，或双击“安装.cmd”。")
    roles, _, _, _ = plan(inp)
    connections = read_connections(inp["database_file"])
    missing = [r.name for r in roles if not (Path(r.data) / "PG_VERSION").exists()]
    if missing or not connections:
        raise SystemExit(f"先做 install（没有实例的：{'、'.join(zh(m) for m in missing) or '无'}；连接文件：{'有' if connections else '没有'}）。")
    for role in roles:
        if service_state(role.service) == "not registered":
            if running(inp, role):
                raise SystemExit(f"{zh(role.name)}：实例正在服务之外运行；先停掉它（pg_ctl -D {role.data} stop）")
            run(["icacls", role.folder, "/grant", f"{SERVICE_SID}:(OI)(CI)M", "/T", "/Q"], f"设置{zh(role.name)}目录权限")
            run([inp["pg_bin"] / "pg_ctl.exe", "register", "-N", role.service, "-D", role.data, "-S", "auto",
                 "-U", SERVICE_ACCOUNT], f"注册{zh(role.name)}服务")  # fmt: skip
            run(["sc", "failure", role.service, "reset=", "86400", "actions=", "restart/60000/restart/60000/restart/60000"],
                f"设置{zh(role.name)}服务的自动重启")  # fmt: skip
            print(f"{zh(role.name)}：服务 {role.service} 已注册（开机自动启动，意外停止后一分钟自动重启）")
        if service_state(role.service) != "RUNNING":
            subprocess.run(["sc", "start", role.service], capture_output=True)
        deadline = time.monotonic() + 60
        while True:
            try:
                psycopg.connect(connections[role.name], connect_timeout=3).close()
                print(f"{zh(role.name)}：服务 {role.service} 已运行，可以连接")
                break
            except psycopg.Error:
                if time.monotonic() > deadline:  # e.g. its disk is not ready yet: the supervisor keeps trying
                    print(f"注意：{zh(role.name)}服务 {role.service} 60 秒内没能连上（看守程序会继续重试）")
                    break
                time.sleep(1)
    xml = Path(tempfile.mkdtemp(prefix="htsg-task-")) / "task.xml"
    try:
        xml.write_text(task_xml(sys.executable, inp["input"]), encoding="utf-16")  # schtasks reads the encoding the XML declares
        run(["schtasks", "/Create", "/TN", TASK, "/XML", xml, "/F"], "注册后端开机任务")
    finally:
        xml.unlink(missing_ok=True)
        xml.parent.rmdir()
    run(["schtasks", "/Run", "/TN", TASK], "启动后端任务")
    print(f"开机任务 {TASK} 已注册并启动：看守程序开机时、并每 5 分钟运行一次；停机维护时在 .local 下建 STOP 文件")  # fmt: skip


def task_xml(python: str, deploy_file: Path) -> str:
    """The task that keeps the backend running: the supervisor, as SYSTEM (no logon, no stored password that could expire), at boot
    and every five minutes (a run already going is not doubled - the five-minute trigger only revives a supervisor that was ended),
    also on battery (a UPS that reports one must not stop it), at normal priority (the default 7 is below normal), no time limit."""
    from xml.sax.saxutils import escape

    arguments = f'"{ROOT / "scripts" / "supervise.py"}" --input "{deploy_file}"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>HTSG backend supervisor (scripts/deploy_databases.py register)</Description></RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
    <TimeTrigger>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_state() -> str:
    done = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"(Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue).State"],
        capture_output=True, text=True,
    )  # fmt: skip
    return done.stdout.strip() or "not registered"


def status(inp: dict):
    import psycopg

    roles, facts, problems, notes = plan(inp)
    connections = read_connections(inp["database_file"]) or {}
    for role in roles:
        cluster = (Path(role.data) / "PG_VERSION").exists()
        answer = "没有配置连接"
        if role.name in connections:
            try:
                with psycopg.connect(connections[role.name], connect_timeout=3) as conn:
                    row = conn.execute("SELECT value FROM htsg_meta WHERE key='schema_generation'").fetchone()
                    answer = f"可以连接，归档版本 {row[0] if row else '无'}"
            except psycopg.Error as error:
                answer = f"连不上（{type(error).__name__}）"
        print(f"{zh(role.name)}  {role.folder}  实例{'有' if cluster else '【没有】'}  服务{zh(service_state(role.service))}  {answer}")
    stop = ROOT / ".local/STOP"
    print(f"开机任务 {TASK}：{zh(task_state())}" + ("；.local/STOP 存在：看守程序什么都不启动" if stop.exists() else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", default="plan", choices=("plan", "install", "register", "status"))
    parser.add_argument("--input", type=Path, default=ROOT / ".local/deploy.json", help="the deployment file; default .local/deploy.json")
    args = parser.parse_args(argv)
    inp = read_input(args.input)
    if args.action == "plan":
        found = plan(inp)
        show(*found)
        if found[2]:
            raise SystemExit(1)
    elif args.action == "install":
        install(inp)
    elif args.action == "register":
        register(inp)
    else:
        status(inp)


if __name__ == "__main__":
    main()
