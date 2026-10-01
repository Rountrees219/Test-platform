"""PackageResolver — resolve the distribution URL and download/unzip it.

Returns an explicit ``Staging`` (dir + version) rather than mutating shared state.
"""

from __future__ import annotations

import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from core.metadata import service_environment
from processes.upgrade.core import Config, Done, Outcome
from processes.upgrade.reporting import Reporter


@dataclass
class Staging:
    dir: Path
    version: str


class PackageResolver:
    def __init__(self, config: Config, reporter: Reporter):
        self._c = config
        self._r = reporter

    def resolve_url(self) -> str:
        c = self._c
        if c.package_url_override:
            return c.package_url_override
        if c.cdn_base:
            base = c.cdn_base
        else:
            environment = service_environment()
            if environment in ("beta", "gamma"):
                base = f"https://apps.super.{environment}myninja.ai"
            elif environment == "prod":
                base = "https://apps.super.myninja.ai"
            else:
                self._r.notify(
                    "error",
                    f"unknown environment '{environment}' — cannot resolve package URL",
                )
                raise Done(1, Outcome.ERROR)
        # A pinned --version targets the immutable versioned artifact; otherwise
        # the channel's moving latest pointer.
        artifact = (
            f"phantom-{c.target_version}.zip"
            if c.target_version
            else "phantom-latest.zip"
        )
        return f"{base}/_dist/ninja/{c.channel}/{artifact}"

    def download(self, url: str | None = None) -> Staging:
        """Fetch and unpack the package, resolving this channel's URL by default."""
        url = url or self.resolve_url()
        staging_dir = Path(tempfile.mkdtemp(prefix="ninja-upgrade."))
        pkg = staging_dir / "pkg.zip"
        self._r.log(f"Downloading package: {url}")
        # Bounded timeout so a stalled CDN/connection can't hang the run forever
        # (applies to connect + each read; a mid-transfer stall raises). Mirrors
        # curl's bounded behaviour in the shell.
        try:
            with urllib.request.urlopen(
                url, timeout=self._c.download_timeout
            ) as resp, open(pkg, "wb") as out:
                shutil.copyfileobj(resp, out)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            self._r.notify("error", f"download failed: {url}")
            raise Done(1, Outcome.ERROR)
        try:
            with zipfile.ZipFile(pkg) as zf:
                if zf.testzip() is not None:
                    raise zipfile.BadZipFile("bad member")
                zf.extractall(staging_dir)
        except (zipfile.BadZipFile, OSError):
            self._r.notify("error", "corrupt zip — aborting, will retry next cycle")
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise Done(1, Outcome.ERROR)
        version_file = staging_dir / "ninja" / "VERSION"
        if not version_file.is_file():
            self._r.notify("error", "zip missing ninja/VERSION — aborting")
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise Done(1, Outcome.ERROR)
        return Staging(dir=staging_dir, version=version_file.read_text().strip())
