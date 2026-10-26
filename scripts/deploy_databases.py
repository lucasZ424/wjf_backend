"""Field deployment of the archive databases: one PostgreSQL cluster per storage role, on the volume the site names for it
(终版 §13.5: the normal, the removable and the emergency database; the removable one on a volume of its own).

    python scripts/deploy_databases.py plan      what would be done, and what stands in the way; changes nothing (the default)
    python scripts/deploy_databases.py install   folders, clusters, databases with their schema, then the connection file and the
                                                 storage paths of the deployment configuration
    python scripts/deploy_databases.py register  (elevated PowerShell) every cluster becomes an automatic Windows service that is
                                                 restarted when it stops by itself; then the task HTSG-Backend runs the supervisor
                                                 (scripts/supervise.py) as SYSTEM at boot and every five minutes; all are started
    python scripts/deploy_databases.py status    folders, clusters, services, connections, the backend task

All four read .local/deploy.json (or --input FILE), which names volumes, not folders - a drive letter is enough:

    {"volumes": {"primary": "D:", "emergency": "C:", "external": "F:"},
     "pg_bin": "C:/Program Files/PostgreSQL/18/bin",
     "config": "config/production.json"}

Optional: leave "external" out when there is no removable disk; "folder" (default "HTSG"), "ports" (default 55440-55442),
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

ROLES = ("primary", "emergency", "external")
PORTS = {"primary": 55440, "emergency": 55441, "external": 55442}
PG_MAJOR = 18  # the PostgreSQL the archive was developed and tested with
MIN_FREE = 20 * 1024**3
SERVICE_ACCOUNT = r"NT AUTHORITY\NetworkService"
SERVICE_SID = "*S-1-5-20"  # NetworkService under any language edition of Windows
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
        raise ValueError(f"{text!r} is neither a drive letter nor an absolute local folder (network paths are not accepted)")
    return drive.upper() + ntpath.normpath(tail)


def volume(path: str) -> dict:
    """The volume an existing path lies on, by its GUID name (two letters or a mount folder of one disk are one volume), its kind
    of drive and its free space."""
    kernel = ctypes.windll.kernel32
    mount = ctypes.create_unicode_buffer(261)
    if not kernel.GetVolumePathNameW(path, mount, 261):
        raise OSError(f"no volume found for {path}")
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
        raise SystemExit(f"No deployment file at {path}; see the top of scripts/deploy_databases.py for its form.") from None
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} is not valid JSON: {error}") from None
    if not isinstance(raw, dict) or set(raw) - {"volumes", "pg_bin", "config", "database_file", "folder", "ports"}:
        raise SystemExit(f"{path}: unknown keys; allowed are volumes, pg_bin, config, database_file, folder, ports")
    volumes = raw.get("volumes")
    if not isinstance(volumes, dict) or not {"primary", "emergency"} <= set(volumes) or set(volumes) - set(ROLES):
        raise SystemExit(f'{path}: "volumes" needs "primary" and "emergency", and may have "external"')
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
            problems.append(f"{name}: {error}")
            continue
        role = Role(name, root, ntpath.join(root, inp["folder"], name), inp["ports"][name])
        roles.append(role)
        if not os.path.isdir(root):
            problems.append(f"{name}: {root} does not exist - is the disk attached, is the letter right?")
            continue
        if not role.folder.isascii():
            problems.append(f"{name}: {role.folder} has non-ASCII characters; PostgreSQL on Windows does not reliably accept them")
        try:
            info = facts[name] = volume_of(root)
        except OSError as error:
            problems.append(f"{name}: the volume of {root} cannot be read ({error})")
            continue
        if info["kind"] == "network":
            problems.append(f"{name}: {root} is a network drive; the archive needs a local disk")
        data = Path(role.data)
        if (data / "PG_VERSION").exists():
            info["state"] = "existing cluster"
            if cluster_port(role.data) != role.port:
                problems.append(f"{name}: the cluster in {role.data} is configured for port {cluster_port(role.data)}, not {role.port}")
        elif data.exists() and any(data.iterdir()):
            problems.append(f"{name}: {role.data} is not empty and holds no cluster; it is left alone")
        else:
            info["state"] = "new cluster"
            if not port_free(role.port):
                problems.append(f"{name}: port {role.port} is in use; choose another under \"ports\"")
        if info["free"] < MIN_FREE:
            notes.append(f"{name}: only {info['free'] / 1024**3:.1f} GiB free on {info['mount']}")
    seen = {}
    for role in roles:
        ident = facts.get(role.name, {}).get("id")
        if ident is not None and ident in seen:
            problems.append(f"{seen[ident]} and {role.name} are on the same volume ({facts[role.name]['mount']}): each role needs its own disk")
        seen.setdefault(ident, role.name)
    for tool in ("initdb.exe", "pg_ctl.exe"):
        if not (inp["pg_bin"] / tool).exists():
            problems.append(f"pg_bin: no {tool} in {inp['pg_bin']}")
    if (inp["pg_bin"] / "initdb.exe").exists():
        out = subprocess.run([str(inp["pg_bin"] / "initdb.exe"), "--version"], capture_output=True, text=True).stdout
        found = re.search(r"\)\s*(\d+)\.", out)
        if not found or int(found.group(1)) != PG_MAJOR:
            problems.append(f"pg_bin: PostgreSQL {PG_MAJOR} is required (tested); found {out.strip() or 'nothing'}")
    reserve = None
    try:
        checked = Settings.model_validate(with_storage(json.loads(inp["config"].read_text(encoding="utf-8-sig")), roles))
        reserve = checked.storage.emergency_reserve_bytes
    except FileNotFoundError:
        problems.append(f"config: {inp['config']} does not exist")
    except ValueError as error:  # JSON or validation
        problems.append(f"config: {inp['config']} with these storage paths is not valid: {str(error).splitlines()[0][:160]}")
    existing = read_connections(inp["database_file"])
    if existing is not None:
        password = existing.get("password", "")
        wanted = {role.name: role.dsn(password) for role in roles}
        if {k: v for k, v in existing.items() if k != "password"} != wanted:
            problems.append(f"database_file: {inp['database_file']} exists and names other databases; it is not replaced")
    elif any(facts.get(r.name, {}).get("state") == "existing cluster" for r in roles):
        problems.append(f"database_file: a cluster exists but {inp['database_file']} (which holds its password) does not; restore it")
    emergency = facts.get("emergency")
    if emergency and reserve is not None and emergency["free"] < reserve:
        notes.append(
            f"emergency: {emergency['free'] / 1024**3:.0f} GiB free is less than the emergency reserve of {reserve / 1024**3:.0f} GiB: "
            "the emergency route would refuse every group"
        )
    return roles, facts, problems, notes


def with_storage(config: dict, roles: list[Role]) -> dict:
    """The deployment configuration with the storage paths of these roles; nothing else in it is touched."""
    storage = dict(config.get("storage", {}))
    for role in roles:
        storage[f"{role.name}_path"] = role.folder.replace("\\", "/")
    if any(role.name == "external" for role in roles):
        storage["external_enabled"] = True
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
        where = f"{info['mount']} {info['kind']}, {info['free'] / 1024**3:.0f} GiB free" if info else "volume unknown"
        print(f"{role.name:<10} {role.folder:<28} port {role.port}  {info.get('state', '-'):<17} ({where})")
    for note in notes:
        print("note:", note)
    for problem in problems:
        print("PROBLEM:", problem)
    print("Nothing stands in the way." if not problems else f"{len(problems)} problem(s): nothing was changed.")


# ------------------------------------------------------------------------------------------------------ the actions


def run(args, what):
    done = subprocess.run([str(a) for a in args], capture_output=True)
    if done.returncode:
        text = (done.stdout + done.stderr).decode("utf-8", errors="replace").strip()
        raise SystemExit(f"{what} failed ({done.returncode}): {text[-600:]}")
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
        raise SystemExit(f"start {role.name} failed ({done.returncode}): {tail}")


def running(inp, role) -> bool:
    return subprocess.run([str(inp["pg_bin"] / "pg_ctl.exe"), "-D", role.data, "status"], capture_output=True).returncode == 0


def install(inp: dict, volume_of=volume):
    import psycopg

    from htsg.archive import ArchiveStore, SchemaError
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
    started = []
    try:
        for role in roles:
            os.makedirs(role.folder, exist_ok=True)
            if facts[role.name]["state"] == "new cluster":
                secret = Path(tempfile.mkdtemp(prefix="htsg-deploy-")) / "pw"
                try:
                    secret.write_text(password, encoding="utf-8")
                    run([inp["pg_bin"] / "initdb.exe", "-D", role.data, "-U", "htsg", "--pwfile", secret,
                         "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"], f"initdb {role.name}")  # fmt: skip
                finally:
                    secret.unlink(missing_ok=True)
                    secret.parent.rmdir()
                with open(Path(role.data) / "postgresql.conf", "a", encoding="utf-8") as f:
                    f.write(SETTINGS.format(port=role.port))
                print(f"{role.name}: cluster created in {role.data}")
            if not running(inp, role):
                start(inp, role)
                started.append(role)
            with psycopg.connect(role.dsn(password, "postgres"), autocommit=True, connect_timeout=10) as conn:
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname='htsg'").fetchone():
                    conn.execute("CREATE DATABASE htsg")
            store = ArchiveStore(role.dsn(password))
            try:
                store.initialize(settings)
            except SchemaError as error:
                raise SystemExit(f"{role.name}: database htsg cannot be used ({error}); nothing more was changed") from error
            finally:
                store.close()
            print(f"{role.name}: database htsg is at the current archive generation")
        connections = {role.name: role.dsn(password) for role in roles} | {"password": password}
        if existing != connections:
            write_atomically(inp["database_file"], json.dumps(connections))
            print(f"connections written to {inp['database_file']} (not printed)")
        if updated != config_raw:
            backup = inp["config"].with_name(f"{inp['config'].name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(inp["config"], backup)
            write_atomically(inp["config"], json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
            print(f"storage paths written into {inp['config']} (previous version kept as {backup.name})")
    finally:
        for role in started:  # the services take over from here; a cluster this run did not start is left running
            subprocess.run([str(inp["pg_bin"] / "pg_ctl.exe"), "-D", role.data, "-w", "-m", "fast", "stop"], capture_output=True)
    print("Next, in an elevated PowerShell: python scripts/deploy_databases.py register")


def service_state(name: str) -> str:
    done = subprocess.run(["sc", "query", name], capture_output=True)
    if done.returncode:
        return "not registered"
    text = done.stdout.decode("utf-8", errors="replace")
    return next((s for s in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING") if s in text), "registered")


def register(inp: dict):
    import psycopg

    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise SystemExit("Registering services needs an elevated PowerShell (Run as administrator).")
    roles, _, _, _ = plan(inp)
    connections = read_connections(inp["database_file"])
    missing = [r.name for r in roles if not (Path(r.data) / "PG_VERSION").exists()]
    if missing or not connections:
        raise SystemExit(f"Run install first (no cluster for: {', '.join(missing) or '-'}; connections: {bool(connections)}).")
    for role in roles:
        if service_state(role.service) == "not registered":
            if running(inp, role):
                raise SystemExit(f"{role.name}: the cluster is running outside a service; stop it first (pg_ctl -D {role.data} stop)")
            run(["icacls", role.folder, "/grant", f"{SERVICE_SID}:(OI)(CI)M", "/T", "/Q"], f"permissions {role.name}")
            run([inp["pg_bin"] / "pg_ctl.exe", "register", "-N", role.service, "-D", role.data, "-S", "auto",
                 "-U", SERVICE_ACCOUNT], f"register {role.name}")  # fmt: skip
            run(["sc", "failure", role.service, "reset=", "86400", "actions=", "restart/60000/restart/60000/restart/60000"],
                f"restart policy {role.name}")  # fmt: skip
            print(f"{role.name}: service {role.service} registered (automatic start, restart one minute after it stops by itself)")
        if service_state(role.service) != "RUNNING":
            subprocess.run(["sc", "start", role.service], capture_output=True)
        deadline = time.monotonic() + 60
        while True:
            try:
                psycopg.connect(connections[role.name], connect_timeout=3).close()
                print(f"{role.name}: service {role.service} is running and accepts connections")
                break
            except psycopg.Error:
                if time.monotonic() > deadline:  # e.g. the removable disk is not plugged in: the supervisor keeps trying
                    print(f"WARNING {role.name}: service {role.service} did not accept connections within 60 s")
                    break
                time.sleep(1)
    xml = Path(tempfile.mkdtemp(prefix="htsg-task-")) / "task.xml"
    try:
        xml.write_text(task_xml(sys.executable, inp["input"]), encoding="utf-16")  # schtasks reads the encoding the XML declares
        run(["schtasks", "/Create", "/TN", TASK, "/XML", xml, "/F"], "register the backend task")
    finally:
        xml.unlink(missing_ok=True)
        xml.parent.rmdir()
    run(["schtasks", "/Run", "/TN", TASK], "start the backend task")
    print(f"task {TASK} registered and started: the supervisor runs as SYSTEM at boot and every five minutes; "
          "create .local/STOP to stop the backend for maintenance")  # fmt: skip


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
        answer = "no connection configured"
        if role.name in connections:
            try:
                with psycopg.connect(connections[role.name], connect_timeout=3) as conn:
                    row = conn.execute("SELECT value FROM htsg_meta WHERE key='schema_generation'").fetchone()
                    answer = f"accepts connections, archive generation {row[0] if row else 'none'}"
            except psycopg.Error as error:
                answer = f"no connection ({type(error).__name__})"
        print(f"{role.name:<10} {role.folder:<28} cluster {'yes' if cluster else 'NO':<4} service {service_state(role.service):<15} {answer}")
    stop = ROOT / ".local/STOP"
    print(f"task {TASK}: {task_state()}" + ("; .local/STOP is present: the supervisor starts nothing" if stop.exists() else ""))


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
