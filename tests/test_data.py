import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import zarr

from openmind.data import block_mean, load_catalog, load_level, prepare
from openmind.source import PrecomputedSource


class DataTests(unittest.TestCase):
    def source(self, shape=(10, 10, 10), chunk=(4, 4, 4), gzip_chunks=True):
        array = np.arange(np.prod(shape), dtype=np.uint16).reshape(shape[::-1])
        metadata = {"data_type": "uint16", "num_channels": 1, "scales": [{"key": "test", "size": shape,
            "resolution": [1000, 2000, 3000], "chunk_sizes": [chunk], "encoding": "raw"}]}

        def fetch(url, limit):
            if url.endswith("/info"):
                return json.dumps(metadata).encode()
            bounds = [tuple(map(int, pair.split("-"))) for pair in url.rsplit("/", 1)[1].split("_")]
            data = array[tuple(slice(a, b) for a, b in bounds)[::-1]].tobytes()
            return gzip.compress(data) if gzip_chunks else data

        return PrecomputedSource("https://example.test", "test", fetch=fetch), array

    def test_chunk_assembly_preserves_xyz_order_and_partial_edges(self):
        source, full = self.source()
        output = np.zeros((7, 6, 5), dtype=full.dtype)
        for slices, block in source.blocks((3, 2, 1), (5, 6, 7), workers=2):
            output[slices] = block
        np.testing.assert_array_equal(output, full[1:8, 2:8, 3:8])

    def test_bad_byte_count_and_outside_region_are_errors(self):
        source, _ = self.source()
        source._fetch = lambda url, limit: b"bad"
        with self.assertRaisesRegex(ValueError, "byte count"):
            list(source.blocks((0, 0, 0), (4, 4, 4), workers=1))
        with self.assertRaisesRegex(ValueError, "outside"):
            list(source.blocks((9, 0, 0), (2, 1, 1)))

    def test_factor_five_mean_and_integer_rounding(self):
        array = np.arange(1000, dtype=np.uint16).reshape(10, 10, 10)
        result = block_mean(array, 5)
        expected = np.empty((2, 2, 2), dtype=np.uint16)
        for z, y, x in np.ndindex(expected.shape):
            expected[z, y, x] = np.rint(array[z*5:z*5+5, y*5:y*5+5, x*5:x*5+5].mean())
        np.testing.assert_array_equal(result, expected)
        with self.assertRaises(ValueError):
            block_mean(array, 3)

    def test_preparation_physical_alignment_hashes_and_budget_guard(self):
        source, full = self.source()
        config = {"name": "fixture", "url": source.url, "scale": "test", "start_xyz": [0, 0, 0],
                  "shape_xyz": [10, 10, 10], "factors": [1, 5], "workers": 2, "nominal_resolution_um": 1}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            catalog_path = prepare(config, output, source=source, progress=lambda _: None)
            catalog = load_catalog(catalog_path)
            native, coarse = catalog["levels"]
            np.testing.assert_array_equal(load_level(catalog_path, native["name"]).array, full)
            loaded = load_level(catalog_path, coarse["name"])
            np.testing.assert_array_equal(loaded.array, block_mean(full, 5))
            self.assertEqual(loaded.spacing_um_xyz, (5, 10, 15))
            self.assertEqual(loaded.origin_um_xyz, (2, 4, 6))
            fine_bounds = np.array(native["origin_um_xyz"]) - np.array(native["spacing_um_xyz"]) / 2
            coarse_bounds = np.array(coarse["origin_um_xyz"]) - np.array(coarse["spacing_um_xyz"]) / 2
            np.testing.assert_array_equal(fine_bounds, coarse_bounds)
            root = zarr.open_group(str(output / "volume.zarr"), mode="r")
            self.assertEqual(root.attrs["ome"]["version"], "0.5")
            with patch("openmind.data.zarr.open_array", side_effect=AssertionError("I/O happened before guard")):
                with self.assertRaisesRegex(ValueError, "limit"):
                    load_level(catalog_path, native["name"], max_bytes=1)
            self.assertEqual(prepare(config, output, source=source, progress=lambda _: None), catalog_path)

    def test_catalog_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps({"format_version": 1, "levels": [{"path": "../private"}]}))
            with self.assertRaisesRegex(ValueError, "inside"):
                load_catalog(path)

    def test_native_one_to_twenty_micrometer_means_keep_the_same_field(self):
        source, full = self.source(shape=(40, 40, 40), chunk=(16, 16, 16))
        config = {"name": "1um fixture", "url": source.url, "scale": "test",
                  "start_xyz": [0, 0, 0], "shape_xyz": [40, 40, 40],
                  "factors": [1, 5, 20], "workers": 2, "nominal_resolution_um": 1,
                  "comparison_note": "Same tissue at all three resolutions"}
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = prepare(config, Path(directory) / "result", source=source, progress=lambda _: None)
            catalog = load_catalog(catalog_path)
            self.assertEqual(catalog["comparison_note"], config["comparison_note"])
            for level, factor in zip(catalog["levels"], (1, 5, 20)):
                loaded = load_level(catalog_path, level["name"])
                np.testing.assert_array_equal(loaded.array, block_mean(full, factor))
                spacing = np.array(loaded.spacing_um_xyz)
                origin = np.array(loaded.origin_um_xyz)
                np.testing.assert_array_equal(origin - spacing / 2, [-0.5, -1, -1.5])
                np.testing.assert_array_equal(origin + (np.array(loaded.array.shape[::-1]) - 0.5) * spacing,
                                              [39.5, 79, 118.5])
            invalid = {**config, "factors": [1, 2.5]}
            with self.assertRaisesRegex(ValueError, "integers"):
                prepare(invalid, Path(directory) / "invalid", source=source)


if __name__ == "__main__":
    unittest.main()
