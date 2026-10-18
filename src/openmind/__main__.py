"""Small CLI shared by the Mac demo and a future Linux GPU deployment."""

import argparse
import json
from pathlib import Path

from .data import _digest, load_catalog, prepare


def main():
    parser = argparse.ArgumentParser(description="BigBrain ROI demo using existing Trame/VTK rendering")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare", help="Download only a configured ROI and build coarser resolutions")
    build.add_argument("--config", type=Path, default=Path("configs/mac-demo.json"))
    build.add_argument("--output", type=Path, default=Path("data/bigbrain-cube"))
    serve = commands.add_parser("serve", help="Start the local browser viewer")
    serve.add_argument("--catalog", type=Path, default=Path("data/bigbrain-cube/catalog.json"))
    serve.add_argument("--compare-catalog", type=Path, action="append", default=[], help="Additional dataset catalog for the region selector; repeat as needed")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8081)
    serve.add_argument("--max-volume-mib", type=int, default=192)
    serve.add_argument("--open-browser", action="store_true")
    check = commands.add_parser("check", help="Verify stored voxel hashes and report the dataset")
    check.add_argument("--catalog", type=Path, default=Path("data/bigbrain-cube/catalog.json"))
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(json.loads(args.config.read_text()), args.output)
    elif args.command == "serve":
        from .viewer import serve as start
        start(args.catalog, host=args.host, port=args.port, max_volume_mib=args.max_volume_mib, open_browser=args.open_browser, comparison_catalogs=args.compare_catalog)
    else:
        import zarr
        catalog = load_catalog(args.catalog)
        base = args.catalog if args.catalog.is_dir() else args.catalog.parent
        for level in catalog["levels"]:
            array = zarr.open_array(str(base / level["path"]), mode="r")
            if _digest(array) != level["sha256"]:
                raise ValueError(f"Voxel hash mismatch: {level['name']}")
        print(json.dumps({"name": catalog["name"], "region": catalog["region"], "levels": catalog["levels"], "hashes": "verified"}, indent=2))


if __name__ == "__main__":
    main()
