import subprocess
import sys

SYSTEM_NOTIFICATION_START = "<system notification>"
SYSTEM_NOTIFICATION_END = "</system notification>"


def get_disk_warning() -> str | None:
    """Return a <system notification> block if disk usage is ≥ 90%, else None.

    Runs df -h directly (phantom runs inside the sandbox).
    """
    try:
        result = subprocess.run(
            ["df", "-h", "/", "--output=source,size,used,avail,pcent"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return None

        headers = lines[0].lower().split()
        data = lines[-1].split()

        size = data[headers.index("size")]
        used = data[headers.index("used")]
        avail = data[headers.index("avail")]
        usage = int(data[headers.index("use%")].replace("%", ""))

        if usage < 90:
            return None

        df_output = f"Source    Size  Used Avail Use%\n/dev/root  {size}  {used}  {avail}  {usage}%"
        return f"{SYSTEM_NOTIFICATION_START}\n{df_output}\n{SYSTEM_NOTIFICATION_END}"
    except Exception as e:
        print(f"⚠️ Could not check disk usage: {e}", file=sys.stderr)
        return None
