"""Read bounded blocks from an unsharded Neuroglancer image volume.

Coordinates at this interface are XYZ voxel indices. Returned arrays are ZYX,
with X contiguous. Missing chunks are errors, never silently invented tissue.
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import gzip
import io
import itertools
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np


def fetch_bytes(url: str, limit: int = 8 * 1024**2) -> bytes:
    """Bound each HTTP response and retry transient network failures only."""
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "OpenMindDemo/0.1", "Accept-Encoding": "identity"})
            with urlopen(request, timeout=45) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"Response exceeds {limit} bytes: {url}")
            return data
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(attempt + 1)
    raise RuntimeError("Unreachable retry state")


@dataclass(frozen=True)
class VolumeInfo:
    shape_xyz: tuple[int, int, int]
    offset_xyz: tuple[int, int, int]
    spacing_um_xyz: tuple[float, float, float]
    chunk_xyz: tuple[int, int, int]
    dtype: np.dtype
    key: str
    encoding: str


class PrecomputedSource:
    """One selected scale, downloaded a bounded number of chunks at a time."""

    def __init__(self, url: str, scale: str, *, fetch=fetch_bytes):
        self.url = url.rstrip("/")
        self._fetch = fetch
        self.metadata = json.loads(fetch(self.url + "/info", 1024**2))
        if self.metadata.get("num_channels") != 1:
            raise ValueError("Only one-channel image volumes are supported")
        selected = next((s for s in self.metadata["scales"] if s["key"] == scale), None)
        if selected is None:
            raise ValueError(f"Scale {scale!r} not found")
        if selected.get("sharding"):
            raise ValueError("Use an unsharded source for this bounded demo")
        dtype = np.dtype(self.metadata["data_type"]).newbyteorder("<")
        if dtype.kind not in "uif" or dtype.itemsize > 4:
            raise ValueError(f"Unsupported scalar type {dtype}")
        self.info = VolumeInfo(
            tuple(selected["size"]), tuple(selected.get("voxel_offset", [0, 0, 0])),
            tuple(float(n) / 1000 for n in selected["resolution"]),
            tuple(selected["chunk_sizes"][0]), dtype, scale, selected["encoding"],
        )
        if any(n <= 0 for n in (*self.info.shape_xyz, *self.info.chunk_xyz, *self.info.spacing_um_xyz)):
            raise ValueError("Shape, chunk size, and spacing must be positive")
        if self.info.encoding not in ("raw", "jpeg"):
            raise ValueError(f"Unsupported chunk encoding {self.info.encoding}")
        self.max_decoded_bytes = math.prod(self.info.chunk_xyz) * dtype.itemsize
        if self.max_decoded_bytes > 64 * 1024**2:
            raise ValueError("Source chunks exceed the 64 MiB safety limit")

    def _chunk(self, start_xyz):
        spec = self.info
        end = tuple(min(a + c, o + n) for a, c, o, n in zip(start_xyz, spec.chunk_xyz, spec.offset_xyz, spec.shape_xyz))
        key = "_".join(f"{a}-{b}" for a, b in zip(start_xyz, end))
        shape_zyx = tuple(b - a for a, b in zip(start_xyz, end))[::-1]
        expected = math.prod(shape_zyx) * spec.dtype.itemsize
        payload = self._fetch(f"{self.url}/{spec.key}/{key}", max(expected * 2, 1024**2))
        if payload.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                payload = stream.read(expected + 1)
        if spec.encoding == "jpeg":
            from PIL import Image
            with Image.open(io.BytesIO(payload)) as image:
                if image.width * image.height != math.prod(shape_zyx):
                    raise ValueError(f"Wrong JPEG dimensions for {key}")
                return np.asarray(image.convert("L")).reshape(shape_zyx)
        if len(payload) != expected:
            raise ValueError(f"Wrong byte count for {key}: {len(payload)} != {expected}")
        return np.frombuffer(payload, dtype=spec.dtype).reshape(shape_zyx)

    def blocks(self, start_xyz, shape_xyz, *, workers=6):
        """Yield (local ZYX slices, block) with at most workers requests alive."""
        if not 1 <= workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        spec = self.info
        stop = tuple(a + n for a, n in zip(start_xyz, shape_xyz))
        if any(n <= 0 for n in shape_xyz) or any(a < o or b > o + n for a, b, o, n in zip(start_xyz, stop, spec.offset_xyz, spec.shape_xyz)):
            raise ValueError("Requested region is outside the source volume")
        first = tuple(o + ((a - o) // c) * c for a, o, c in zip(start_xyz, spec.offset_xyz, spec.chunk_xyz))
        starts = iter(itertools.product(*(range(a, b, c) for a, b, c in zip(first, stop, spec.chunk_xyz))))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {}

            def submit_one():
                pos = next(starts, None)
                if pos is not None:
                    pending[pool.submit(self._chunk, pos)] = pos

            for _ in range(workers):
                submit_one()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    pos = pending.pop(future)
                    chunk = future.result()
                    lo = tuple(max(a, p) for a, p in zip(start_xyz, pos))
                    hi = tuple(min(b, p + n) for b, p, n in zip(stop, pos, chunk.shape[::-1]))
                    local = tuple(slice(a - s, b - s) for a, b, s in zip(lo, hi, start_xyz))[::-1]
                    inside = tuple(slice(a - p, b - p) for a, b, p in zip(lo, hi, pos))[::-1]
                    yield local, chunk[inside]
                    submit_one()
