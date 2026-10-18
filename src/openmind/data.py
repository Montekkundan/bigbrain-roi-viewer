"""Prepare multi-resolution ROI data and enforce the viewer's input budget."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import shutil
import uuid

import numpy as np
import zarr
from zarr.codecs import BloscCodec

from .source import PrecomputedSource


@dataclass
class LoadedVolume:
    array: np.ndarray
    spacing_um_xyz: tuple[float, float, float]
    origin_um_xyz: tuple[float, float, float]
    name: str
    metadata: dict


def load_catalog(path: Path) -> dict:
    path = Path(path)
    if path.is_dir():
        path = path / "catalog.json"
    catalog = json.loads(path.read_text())
    if catalog.get("format_version") != 1 or not catalog.get("levels"):
        raise ValueError("Unsupported or empty dataset catalog")
    for level in catalog["levels"]:
        relative = Path(level["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Level paths must stay inside the dataset directory")
        if len(level["shape_zyx"]) != 3 or any(n <= 0 for n in level["shape_zyx"]):
            raise ValueError("Levels need a positive 3D shape")
        if len(level["spacing_um_xyz"]) != 3 or any(not math.isfinite(s) or s <= 0 for s in level["spacing_um_xyz"]):
            raise ValueError("Levels need finite positive physical spacing")
    return catalog


def load_level(path: Path, level_name: str, max_bytes=192 * 1024**2) -> LoadedVolume:
    """Load one complete ROI level, rejecting oversized requests before I/O.

    This is an explicit bounded-ROI VTK prototype, not a full-volume GPU cache.
    The limit covers scalar input; VTK, framebuffers and the process add overhead.
    """
    path = Path(path)
    base = path if path.is_dir() else path.parent
    catalog = load_catalog(path)
    level = next((item for item in catalog["levels"] if item["name"] == level_name), None)
    if level is None:
        raise ValueError(f"Unknown resolution {level_name!r}")
    dtype = np.dtype(level["dtype"])
    required = math.prod(level["shape_zyx"]) * dtype.itemsize
    if required > max_bytes:
        raise ValueError(f"Resolution needs {required / 1024**2:.1f} MiB of scalar data; limit is {max_bytes / 1024**2:.1f} MiB. Select a coarser level or a smaller region.")
    stored = zarr.open_array(str(base / level["path"]), mode="r")
    if tuple(stored.shape) != tuple(level["shape_zyx"]) or stored.dtype != dtype:
        raise ValueError("Stored array disagrees with the dataset catalog")
    array = np.asarray(stored[:], order="C")
    return LoadedVolume(array, tuple(level["spacing_um_xyz"]), tuple(level["origin_um_xyz"]), level_name, {**catalog, "selected_level": level})


def block_mean(array: np.ndarray, factor: int) -> np.ndarray:
    """Integer block average with nearest-even rounding, retaining scalar dtype."""
    if factor < 1 or any(n % factor for n in array.shape):
        raise ValueError("Each region dimension must be divisible by its integer scale factor")
    if factor == 1:
        return array.copy()
    z, y, x = array.shape
    averaged = array.reshape(z // factor, factor, y // factor, factor, x // factor, factor).mean(axis=(1, 3, 5), dtype=np.float64)
    if np.issubdtype(array.dtype, np.integer):
        averaged = np.rint(averaged)
    return averaged.astype(array.dtype)


def _array(root, key, shape, dtype):
    return root.create_array(key, shape=shape, chunks=tuple(min(64, n) for n in shape), dtype=dtype,
                             compressors=[BloscCodec(cname="zstd", clevel=3, shuffle="bitshuffle")],
                             dimension_names=("z", "y", "x"))


def _digest(array) -> str:
    result = hashlib.sha256()
    for z in range(array.shape[0]):
        result.update(np.asarray(array[z, :, :], order="C").tobytes())
    return result.hexdigest()


def prepare(config: dict, output: Path, *, progress=print, source=None) -> Path:
    """Fetch only the requested region, then build coarser Zarr levels in blocks."""
    output = Path(output)
    start = tuple(int(n) for n in config["start_xyz"])
    shape_xyz = tuple(int(n) for n in config["shape_xyz"])
    requested_factors = config.get("factors", [1, 5])
    if any(isinstance(f, bool) or not isinstance(f, int) for f in requested_factors):
        raise ValueError("Scale factors must be positive integers")
    factors = sorted(set(requested_factors))
    if len(start) != 3 or len(shape_xyz) != 3 or not factors or factors[0] != 1:
        raise ValueError("Supply XYZ region coordinates and factors including native factor 1")
    if any(f < 1 for f in factors) or any(n <= 0 or any(n % f for f in factors) for n in shape_xyz):
        raise ValueError("Positive region dimensions must be divisible by every positive scale factor")
    if output.exists():
        existing = load_catalog(output)
        if existing.get("preparation_config") != config:
            raise ValueError("Output already contains a different dataset; choose another output folder")
        progress("Dataset already prepared; keeping its verified files")
        return output / "catalog.json"
    source = source or PrecomputedSource(config["url"], config["scale"])
    spec = source.info
    if math.prod(shape_xyz) * spec.dtype.itemsize > config.get("max_roi_mib", 256) * 1024**2:
        raise ValueError("Requested ROI exceeds max_roi_mib; choose a smaller cube or explicitly raise the data budget")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / ("." + output.name + ".preparing-" + uuid.uuid4().hex[:8])
    staging.mkdir()
    try:
        root = zarr.open_group(str(staging / "volume.zarr"), mode="w", zarr_format=3)
        native = _array(root, "s0", shape_xyz[::-1], spec.dtype)
        received = 0
        for slices, block in source.blocks(start, shape_xyz, workers=config.get("workers", 6)):
            native[slices] = block
            received += block.size
            progress(f"Downloaded {received / math.prod(shape_xyz):.0%} of requested cube")
        if received != math.prod(shape_xyz):
            raise ValueError("Downloaded region is incomplete")
        levels = []
        datasets = []
        nominal = config.get("nominal_resolution_um")
        for index, factor in enumerate(factors):
            key = f"s{index}"
            shape = tuple(n // factor for n in native.shape)
            target = native if factor == 1 else _array(root, key, shape, spec.dtype)
            if factor != 1:
                # Keep reduction inputs around 128³ even for a 20x mean.
                # Factors larger than 128 read one output voxel at a time;
                # the explicit native ROI budget still bounds that input.
                block_edge = max(1, min(32, 128 // factor))
                for z, y, x in itertools.product(*(range(0, n, block_edge) for n in shape)):
                    lo = (z, y, x)
                    hi = tuple(min(a + block_edge, n) for a, n in zip(lo, shape))
                    block = np.asarray(native[tuple(slice(a * factor, b * factor) for a, b in zip(lo, hi))])
                    target[tuple(slice(a, b) for a, b in zip(lo, hi))] = block_mean(block, factor)
                progress(f"Built {factor}x coarser level")
            spacing = tuple(s * factor for s in spec.spacing_um_xyz)
            # Block means belong at the center of each physical source block.
            origin = tuple((a + (factor - 1) / 2) * s for a, s in zip(start, spec.spacing_um_xyz))
            label = (f"{nominal * factor:g} µm nominal" if nominal else f"{factor}x voxel spacing")
            if factor == 1:
                label += " (native)"
            else:
                label += f" ({factor}× mean)"
            level = {"name": label, "path": f"volume.zarr/{key}", "shape_zyx": list(shape),
                     "spacing_um_xyz": list(spacing), "origin_um_xyz": list(origin),
                     "dtype": str(spec.dtype), "factor": factor, "sha256": _digest(target)}
            levels.append(level)
            datasets.append({"path": key, "coordinateTransformations": [
                {"type": "scale", "scale": list(spacing[::-1])},
                {"type": "translation", "translation": list(origin[::-1])}]})
        root.attrs["ome"] = {"version": "0.5", "multiscales": [{"name": config["name"],
            "axes": [{"name": axis, "type": "space", "unit": "micrometer"} for axis in ("z", "y", "x")],
            "datasets": datasets, "type": "mean", "metadata": {"description": "Integer block means; nearest-even rounding"}}]}
        catalog = {"format_version": 1, "name": config["name"], "preparation_config": config,
            "prepared_utc": datetime.now(timezone.utc).isoformat(), "levels": levels,
            "region": {"start_xyz": list(start), "shape_xyz": list(shape_xyz)},
            "source": {"url": source.url, "scale": spec.key, "metadata": source.metadata,
                **config.get("source", {})},
            "scope": "One bounded 3D ROI; each selected level is loaded whole by VTK. Full-volume out-of-core GPU rendering is not implemented."}
        if config.get("comparison_note"):
            catalog["comparison_note"] = str(config["comparison_note"])
        if "background_value" in config:
            catalog["background_value"] = config["background_value"]
        (staging / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
        staging.rename(output)
    except BaseException:
        shutil.rmtree(staging)
        raise
    progress(f"Ready: {output / 'catalog.json'}")
    return output / "catalog.json"
