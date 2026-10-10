import shutil
import shlex
from pathlib import Path
import subprocess
import os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
import re
import time
from prometheus_client import CollectorRegistry, Gauge, pushadd_to_gateway


# Backup target
BACKUP_ROOT = Path("/mnt/backup/")
# Backup source
BACKUP_SOURCE = Path("/src/")

# used for prometheus metrics
PUSHGATEWAY = os.getenv("PUSHGATEWAY_URL", "prometheus-pushgateway:9091")
INSTANCE = os.getenv("BACKUP_INSTANCE", "backup-app")

# timezone
TZ = ZoneInfo("Europe/Berlin")

# matches a backupname 'YYYY-MM-dd_HH-MM-SS'
SNAPSHOT_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")

LOG_FILE = Path(__file__).parent / "logs/backup.log"

# general settings for rsync
_BASE_FLAGS = ("-a", "--info=stats2", "--numeric-ids")
BACKUP_MODES = {
    "complex":   (*_BASE_FLAGS, "--delete", "-H"),
    "simple":    _BASE_FLAGS
}

# for analysing rsync log
_STATS_PATTERNS = {
    "files_transferred": re.compile(r"Number of regular files transferred: ([\d,.]+)"),
    "total_size_bytes": re.compile(r"Total file size: ([\d,.]+) bytes"),
    "transferred_bytes": re.compile(r"Total transferred file size: ([\d,.]+) bytes"),
}


def push_metrics(success: bool, duration: float,
                 stats: dict[str, int] | None = None,
                 snapshot_count: int | None = None) -> None:
    registry = CollectorRegistry()

    Gauge("backup_last_run_timestamp_seconds",
          "Timestamp of the last backup", registry=registry).set_to_current_time()
    Gauge("backup_success",
          "One for success else 0", registry=registry).set(int(success))
    Gauge("backup_duration_seconds",
          "Duration of the last run", registry=registry).set(duration)

    if success:
        Gauge("backup_last_success_timestamp_seconds",
              "Timestamp of the last successful backup", registry=registry).set_to_current_time()

        for key, value in (stats or {}).items():
            Gauge(f"backup_{key}", f"rsync: {key}", registry=registry).set(value)

        if snapshot_count is not None:
            Gauge("backup_snapshots", "Number of the last Snapshots",
                  registry=registry).set(snapshot_count)

    try:
        pushadd_to_gateway(PUSHGATEWAY, job="backup",
                           grouping_key={"instance": INSTANCE},
                           registry=registry, timeout=10)
    except Exception as exc:
        with open(LOG_FILE, "a") as f:
            f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} | "
                    f"WARNING: Push to {PUSHGATEWAY} failed: {exc}\n")

def parse_rsync_stats(lines: list[str]) -> dict[str, int]:
    stats = {}
    for line in lines:
        for key, pattern in _STATS_PATTERNS.items():
            if match := pattern.search(line):
                stats[key] = int(re.sub(r"[,.]", "", match.group(1)))
    return stats

def find_rsync() -> str:
    path = shutil.which("rsync")
    if path: 
        return path
    
    default = "/usr/bin/rsync"
    if Path(default).exists():
        return default
    
    raise FileNotFoundError(
        "rsync not found"
    )

def check_dst(root: Path) -> None:
    """ 
    Checks whether the destination folder exists and 
    whether the markerfile contained is contained within it. 
    """

    # the folder exists?
    if not root.is_dir():
        raise FileNotFoundError(
            f"Destination folder not found: '{root}'")

    # is the markerfile found in the real target folder?
    if not (root / ".backuptarget").is_file():
        raise RuntimeError(
            f"Destination is not a mountpoint!")
    

def do_backup(src: Path, dst: str, mode: str = "complex", 
              link_dest: str | None = None) -> dict[str, int]:
    # make sure the log folder exists
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    flags = BACKUP_MODES.get(mode, BACKUP_MODES["simple"])
    output: list[str] = []
    if mode == "complex" and link_dest and Path(link_dest).is_dir():
        flags = [*flags, f"--link-dest={Path(link_dest).resolve()}"]

    # do we see the folder to backup?
    if not src.is_dir():
        raise FileNotFoundError(
            f"Source not found! {src.absolute()}"
        )

    # we only want the content, not the folder itself. 
    src = str(src.absolute()).rstrip("/") + "/"
    cmd = [find_rsync(), src, dst, *flags]

    with open(LOG_FILE, "a", encoding="utf-8") as log_fh, \
         subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace") as process:
        log_fh.write(f"\n=== {datetime.now(TZ):%Y-%m-%d %H:%M:%S} | {shlex.join(cmd)}\n")
        for line in process.stdout:
            line = line.rstrip()
            output.append(line)
            if line:
                log_fh.write(line + "\n")

    if process.returncode != 0 and process.returncode != 24:
        raise RuntimeError(
            f"rsync terminated with an error {process.returncode}. "
            "Check backup.log for details."
        )

    return parse_rsync_stats(output)


def list_snapshots() -> list[Path]:
    """All completed snapshots, oldest first."""
    return sorted(
        p for p in BACKUP_ROOT.iterdir()
        if p.is_dir() and SNAPSHOT_RE.fullmatch(p.name)
    )

def prune(keep: int = 14) -> None:
    for old in list_snapshots()[:-keep]:
        shutil.rmtree(old)


def main() -> None:
    start = time.monotonic()
    success = False
    stats: dict[str, int] = {}
    try:
        # remove all incorrect, incomplete backups
        for leftover in BACKUP_ROOT.glob("*.incomplete"):
            shutil.rmtree(leftover)

        check_dst(BACKUP_ROOT)

        snapshots = list_snapshots()
        previous = snapshots[-1] if snapshots else None

        stamp = datetime.now(TZ).strftime("%Y-%m-%d_%H-%M-%S")
        final_dir = BACKUP_ROOT / stamp

        # Incomplete while the backup is running
        work_dir = BACKUP_ROOT / f"{stamp}.incomplete"
        work_dir.mkdir()

        stats = do_backup(
            src=BACKUP_SOURCE,
            dst=str(work_dir),
            mode="complex",
            link_dest=str(previous) if previous else None,
        )

        # Folder gets its final name 
        work_dir.rename(final_dir)

        # cleanup old snapshots
        prune()

        # Backup state for metrics
        success = True
    finally:
        push_metrics(
            success=success,
            duration=time.monotonic() - start,
            stats=stats,
            snapshot_count=len(list_snapshots()) if success else None,
        )

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()