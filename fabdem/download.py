"""Download the FABDEM tiles covering every reference site into ``fabdem/data/tiles/``.

FABDEM V1-2 (Hawker et al. 2022, https://doi.org/10.5523/bris.s5hqmjcdj8yo2ibzi9b4ew3sn; licence
CC BY-NC-SA 4.0) is published as 10 x 10 degree zips of 1 x 1 degree GeoTIFFs. The server supports
HTTP range requests, so each zip's directory is read remotely and only the needed tiles are
fetched. A tile missing from its zip is all sea (except in Armenia and Azerbaijan,
which FABDEM leaves out: avoid sites there). Already downloaded tiles are skipped, and so are
sites whose square is already cut into ``data/windows/`` (delete a square to fetch its tiles again).

Run from the project root: ``python fabdem/download.py``.
"""

import io
import math
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from locations import CITIES
from locations import WILD

BASE_URL = "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn"
TILES = Path(__file__).resolve().parent / "data" / "tiles"
# Squares already cut by reference.py; their sites need no tiles.
WINDOWS = Path(__file__).resolve().parent / "data" / "windows"
# Side of a reference square, plus a margin for the corners, in km.
SIDE_KM = 57.344 * 1.1
KM_PER_DEGREE = 111.32
# Zips fetched at once (the server limits each connection's speed).
PARALLEL = 8


class RemoteFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests."""

    def __init__(self, url: str) -> None:
        self.url, self.pos = url, 0
        head = urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=120)
        self.size = int(head.headers["Content-Length"])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def readinto(self, buffer: memoryview) -> int:
        if self.pos >= self.size:
            return 0
        end = min(self.pos + len(buffer), self.size) - 1
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        data = urllib.request.urlopen(request, timeout=300).read()
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)


def corner(lat: int, lon: int) -> str:
    """FABDEM's name for the corner at whole degrees, e.g. ``N43E005`` or ``S34W072``."""
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def tiles_for(lat: float, lon: float) -> set[tuple[int, int]]:
    """South-west corners of the 1-degree tiles a reference square centred on ``(lat, lon)`` touches."""
    half_lat = SIDE_KM / 2 / KM_PER_DEGREE
    half_lon = half_lat / math.cos(math.radians(lat))
    return {(a, b) for a in range(math.floor(lat - half_lat), math.floor(lat + half_lat) + 1)
            for b in range(math.floor(lon - half_lon), math.floor(lon + half_lon) + 1)}


def window_path(name: str) -> Path:
    """Where the cut square of the site called ``name`` is saved."""
    return WINDOWS / f"{name.lower().replace(' ', '_')}.npz"


def zip_name(lat: int, lon: int) -> str:
    """The 10-degree zip holding the tile at ``(lat, lon)``."""
    a, b = 10 * math.floor(lat / 10), 10 * math.floor(lon / 10)
    # The far corner of the last column is written W180, not E180.
    far = corner(a + 10, b + 10 if b + 10 < 180 else -180)
    return f"{corner(a, b)}-{far}_FABDEM_V1-2.zip"


def fetch(name: str, tiles: list[tuple[int, int]]) -> None:
    """Fetch ``tiles`` from the zip ``name``; a tile missing from it is all sea."""
    todo = [t for t in tiles if not (TILES / f"{corner(*t)}_FABDEM_V1-2.tif").exists()
            and not (TILES / f"{corner(*t)}.sea").exists()]
    if not todo:
        return
    archive = zipfile.ZipFile(io.BufferedReader(RemoteFile(f"{BASE_URL}/{name}"), 1 << 22))
    members = set(archive.namelist())
    for tile in todo:
        member = f"{corner(*tile)}_FABDEM_V1-2.tif"
        if member in members:
            # Write under a temporary name, so an interrupted download is fetched again next time.
            (TILES / f"{member}.part").write_bytes(archive.read(member))
            (TILES / f"{member}.part").rename(TILES / member)
            print(f"  {member}", flush=True)
        else:
            # Leave a marker so the tile isn't looked up again.
            (TILES / f"{corner(*tile)}.sea").touch()
            print(f"  {corner(*tile)}: sea", flush=True)


def main() -> None:
    """Fetch every missing tile of the sites not yet cut, several zips at a time."""
    TILES.mkdir(parents=True, exist_ok=True)
    sites = {name: p for name, p in {**CITIES, **WILD}.items() if not window_path(name).exists()}
    needed = set().union(*(tiles_for(*p) for p in sites.values()))
    by_zip: dict[str, list[tuple[int, int]]] = {}
    for tile in sorted(needed):
        by_zip.setdefault(zip_name(*tile), []).append(tile)
    print(f"{len(sites)} sites to cut: {len(needed)} tiles in {len(by_zip)} zips")
    with ThreadPoolExecutor(PARALLEL) as pool:
        for future in [pool.submit(fetch, name, tiles) for name, tiles in by_zip.items()]:
            future.result()


if __name__ == "__main__":
    main()
