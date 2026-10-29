"""Install/start isolated development PostgreSQL clusters only under D:\\HTSG-dev, and build their database.

    install   first time: the two clusters and the credentials in .local/database.json
    start     start them; create an EMPTY database `htsg` where it is missing
    build     bring `htsg` of both clusters to the current archive generation (终版 §13.2): an empty one gets its
              schema, one already at this generation is accepted, a legacy (frame-based) or foreign one is refused here
    new       --name htsg_<x>: a NEW pair <x> on both clusters at the current generation, its connections in
              .local/database-<x>.json; `htsg` and .local/database.json are left as they are (a new generation goes into new
              databases, the connection file is switched by hand once they are checked - 终版 §13.6)
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path("D:/HTSG-dev")
PROJECT = Path(__file__).resolve().parents[1]
URL = "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260488"
NEW_NAME = re.compile(r"htsg_[a-z0-9_]{1,58}")


def run(args):
    subprocess.run(
        [str(x) for x in args],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def build(local):
    """The schema is created here, not left to the first start of the backend: until then a database is only a name,
    and a legacy one would be found out as `refused` at run time, with the backend running on without an archive."""
    from htsg.archive import ArchiveStore, SchemaError
    from htsg.config import load_settings

    conf = json.loads((local / "database.json").read_text())
    config = load_settings(PROJECT / "config/development.json")
    for name in ["primary", "emergency"]:
        store = ArchiveStore(conf[name])
        try:
            store.initialize(config)
        except SchemaError as error:
            raise SystemExit(f"{name}: {error}.  Drop the database and run `start` and `build` again.") from error
        finally:
            store.close()
        print(f"{name}: database htsg is at the current archive generation.")


def new_databases(conf: dict, name: str, out: Path):
    """A NEW pair `name` on both clusters at the current archive generation, its connections written to `out`.  The databases
    in use and their connection file are not touched; a name that exists is refused - an old database is never taken for a new one."""
    import psycopg
    from psycopg import sql

    from htsg.archive import ArchiveStore
    from htsg.config import load_settings

    if not NEW_NAME.fullmatch(name):
        raise SystemExit(f"{name!r}: a new database name is htsg_ and then lower-case letters, digits or _ (63 characters at most)")
    if out.exists():
        raise SystemExit(f"{out} already exists")
    admin = {role: conf[role].rsplit("/", 1)[0] + "/postgres" for role in ("primary", "emergency")}
    for role, dsn in admin.items():
        with psycopg.connect(dsn) as conn:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                raise SystemExit(f"{role}: database {name} already exists; a new generation goes into a new database")
    config = load_settings(PROJECT / "config/development.json")
    written = {"password": conf["password"]}
    for role, dsn in admin.items():
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        written[role] = conf[role].rsplit("/", 1)[0] + "/" + name
        store = ArchiveStore(written[role])
        try:
            store.initialize(config)
        finally:
            store.close()
        print(f"{role}: new database {name} is at the current archive generation.")
    out.write_text(json.dumps(written))
    print(f"Connections in {out} (not printed); .local/database.json is unchanged.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "start", "build", "new", "stop", "status"])
    parser.add_argument("--name", help="new: the name of the new pair of databases")
    args = parser.parse_args()
    if os.name != "nt" or ROOT.resolve().drive.upper() != "D:":
        raise SystemExit("Development databases must be on D:")
    binary = ROOT / "pgsql/bin"
    local = PROJECT / ".local"
    if args.action == "install":
        ROOT.mkdir(exist_ok=True)
        if shutil.disk_usage(ROOT).free < 4 * 1024**3:
            raise SystemExit("Need at least 4 GiB free on D:; nothing installed")
        archive = ROOT / "postgresql-18.6.zip"
        if not (binary / "initdb.exe").exists():
            if not archive.exists():
                run(
                    [
                        "curl.exe",
                        "--fail",
                        "--location",
                        "--retry",
                        "2",
                        "--max-time",
                        "600",
                        URL,
                        "--output",
                        archive,
                    ]
                )
            with zipfile.ZipFile(archive) as z:
                for item in z.infolist():
                    target = (ROOT / item.filename).resolve()
                    if not target.is_relative_to(ROOT.resolve()):
                        raise SystemExit("Unsafe archive member")
                z.extractall(ROOT)
            (ROOT / "download.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
        local.mkdir(exist_ok=True)
        credential = local / "database.json"
        if credential.exists():
            conf = json.loads(credential.read_text())
        else:
            password = secrets.token_urlsafe(32)
            conf = {
                name: f"postgresql://htsg:{password}@127.0.0.1:{port}/htsg"
                for name, port in [("primary", 55432), ("emergency", 55433)]
            }
            conf["password"] = password
            credential.write_text(json.dumps(conf), encoding="utf-8")
        for name, port in [("primary", 55432), ("emergency", 55433)]:
            data = ROOT / name
            if not (data / "PG_VERSION").exists():
                if data.exists() and any(data.iterdir()):
                    raise SystemExit(f"Refusing nonempty uninitialized directory: {data}")
                pw = ROOT / f"{name}.pw"
                try:
                    pw.write_text(conf["password"], encoding="utf-8")
                    run(
                        [
                            binary / "initdb.exe",
                            "-D",
                            data,
                            "-U",
                            "htsg",
                            "--pwfile",
                            pw,
                            "--auth=scram-sha-256",
                            "--encoding=UTF8",
                            "--locale=C",
                        ]
                    )
                finally:
                    pw.unlink(missing_ok=True)
                with (data / "postgresql.conf").open("a") as f:
                    f.write(
                        f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n"
                        "shared_buffers = '32MB'\nmax_connections = 15\nwork_mem = '2MB'\n"
                        "temp_file_limit = '32MB'\nmax_wal_size = '128MB'\nmin_wal_size = '32MB'\n"
                        "logging_collector = on\nlog_directory = 'log'\n"
                        "log_filename = 'postgresql-%w.log'\nlog_rotation_age = '1d'\n"
                        "log_truncate_on_rotation = on\nstatement_timeout = '5s'\n"
                    )
        print("Installed two isolated D: clusters; credentials in .local/database.json (not printed).")
    if args.action == "build":
        build(local)
        return
    if args.action == "new":
        if not args.name:
            raise SystemExit("new needs --name htsg_<x>")
        new_databases(json.loads((local / "database.json").read_text()), args.name, local / f"database-{args.name}.json")
        return
    for name in ["primary", "emergency"]:
        data = ROOT / name
        if args.action == "start":
            status = subprocess.run(
                [str(binary / "pg_ctl.exe"), "-D", str(data), "status"], capture_output=True
            )
            if status.returncode:
                run([binary / "pg_ctl.exe", "-D", data, "-l", ROOT / f"{name}-startup.log", "-w", "start"])
            conf = json.loads((local / "database.json").read_text())
            import psycopg

            dsn = conf[name].rsplit("/", 1)[0] + "/postgres"
            with psycopg.connect(dsn, autocommit=True) as conn:
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname='htsg'").fetchone():
                    conn.execute("CREATE DATABASE htsg")
        elif args.action == "stop":
            run([binary / "pg_ctl.exe", "-D", data, "-w", "-m", "fast", "stop"])
        elif args.action == "status":
            run([binary / "pg_ctl.exe", "-D", data, "status"])


if __name__ == "__main__":
    main()
