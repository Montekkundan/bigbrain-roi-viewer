"""Resolution alignment and failed-switch recovery without a render context."""

from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch

import numpy as np

from openmind.viewer import VolumeViewer, physical_pivot, voxel_cell_bounds


class FakeState(SimpleNamespace):
    def update(self, **values):
        vars(self).update(values)


class PhysicalAlignmentTests(unittest.TestCase):
    def test_percentage_cuts_keep_the_same_position_after_block_averaging(self):
        spacing = (21.166666666666668, 20.0, 21.166666666666668)
        origin = (77216.0, 81920.0, 66378.66666666667)
        percentages = (25.0, 70.0, 35.0)
        native_bounds = voxel_cell_bounds((320, 320, 320), spacing, origin)
        coarse_bounds = voxel_cell_bounds(
            (64, 64, 64),
            tuple(value * 5 for value in spacing),
            tuple(position + 2 * step for position, step in zip(origin, spacing)),
        )

        for native_edge, coarse_edge in zip(native_bounds, coarse_bounds):
            self.assertAlmostEqual(native_edge, coarse_edge, places=8)
        native_pivot = physical_pivot(native_bounds, percentages)
        coarse_pivot = physical_pivot(coarse_bounds, percentages)
        for axis, (native_position, coarse_position) in enumerate(
            zip(native_pivot, coarse_pivot)
        ):
            expected = (
                origin[axis]
                - spacing[axis] / 2
                + 320 * spacing[axis] * percentages[axis] / 100
            )
            self.assertAlmostEqual(native_position, expected, places=8)
            self.assertAlmostEqual(coarse_position, expected, places=8)


class LevelSwitchRecoveryTests(unittest.TestCase):
    def make_viewer(self):
        viewer = VolumeViewer.__new__(VolumeViewer)
        viewer._building = False
        viewer.current_level = "native"
        viewer.state = SimpleNamespace(
            level="coarse", busy=False, error_message=""
        )
        viewer.renderer = Mock()
        viewer.update = Mock()
        return viewer

    def test_preflight_failure_restores_the_selected_loaded_level(self):
        viewer = self.make_viewer()
        viewer._load_level = Mock(side_effect=ValueError("Volume exceeds the budget"))

        viewer._level_changed(level="coarse")

        viewer._load_level.assert_called_once_with("coarse")
        self.assertEqual(viewer.current_level, "native")
        self.assertEqual(viewer.state.level, "native")
        self.assertEqual(viewer.state.error_message, "Volume exceeds the budget")
        self.assertFalse(viewer.state.busy)


    def test_failure_after_unloading_recovers_previous_level_and_keeps_error(self):
        viewer = self.make_viewer()

        def load_level(name):
            if name == "coarse":
                viewer.current_level = None
                raise OSError("Coarse level could not be read")
            viewer.current_level = name
            # A successful real load clears this, so the caller must retain
            # the original failure after restoring the previous level.
            viewer.state.error_message = ""

        viewer._load_level = Mock(side_effect=load_level)

        viewer._level_changed(level="coarse")

        self.assertEqual(
            viewer._load_level.call_args_list, [call("coarse"), call("native")]
        )
        self.assertEqual(viewer.current_level, "native")
        self.assertEqual(viewer.state.level, "native")
        self.assertEqual(viewer.state.error_message, "Coarse level could not be read")
        self.assertFalse(viewer.state.busy)


class DatasetSwitchTests(unittest.TestCase):
    primary = "/tmp/coarse/catalog.json"
    comparison = "/tmp/micro/catalog.json"

    def make_viewer(self):
        viewer = VolumeViewer.__new__(VolumeViewer)
        viewer.np = np
        viewer.max_bytes = 4096
        viewer._building = False
        viewer.current_dataset = None
        viewer.current_level = None
        viewer._appearance_calibrations = {}
        viewer.state = FakeState(busy=False, error_message="")
        viewer.properties = Mock()
        viewer.update = Mock()
        viewer.catalogs = {
            self.primary: {
                "name": "Whole-brain source region",
                "source": {"title": "20 micron source", "url": "https://example.org/coarse", "doi": "10/coarse"},
                "background_value": 255,
                "levels": [{"name": "20um", "shape_zyx": [4, 4, 4], "spacing_um_xyz": [20, 20, 20], "dtype": "uint8"}],
            },
            self.comparison: {
                "name": "Micron VOI region",
                "source": {"title": "VOI source", "url": "https://example.org/micro", "doi": "10/micro"},
                "background_value": 0,
                "comparison_note": "Native 1 micron region with derived 5 and 20 micron levels.",
                "levels": [
                    {"name": "1um", "shape_zyx": [8, 8, 8], "spacing_um_xyz": [1, 1, 1], "dtype": "uint8"},
                    {"name": "5um", "shape_zyx": [2, 2, 2], "spacing_um_xyz": [5, 5, 5], "dtype": "uint8"},
                ],
            },
        }
        viewer._set_catalog(self.primary)
        viewer.current_level = "20um"
        viewer.state.dataset = self.comparison
        return viewer

    def test_switch_updates_catalog_provenance_and_resets_camera(self):
        viewer = self.make_viewer()

        def load_level(name, *, reset_camera=False):
            self.assertEqual(viewer.catalog_path, Path(self.comparison))
            viewer.current_level = name

        viewer._load_level = Mock(side_effect=load_level)

        viewer._dataset_changed(dataset=self.comparison)

        viewer._load_level.assert_called_once_with("1um", reset_camera=True)
        self.assertEqual(viewer.current_dataset, self.comparison)
        self.assertEqual(viewer.state.dataset_name, "Micron VOI region")
        self.assertEqual(viewer.state.source_url, "https://example.org/micro")
        self.assertEqual(viewer.state.source_title, "VOI source")
        self.assertEqual(viewer.state.source_doi, "10/micro")
        self.assertEqual(viewer.state.comparison_note, viewer.catalog["comparison_note"])
        self.assertEqual(viewer.state.level, "1um")
        self.assertEqual([item["value"] for item in viewer.state.level_items], ["1um", "5um"])
        self.assertFalse(viewer.state.busy)
        self.assertFalse(viewer._building)

    def test_no_usable_target_level_keeps_current_volume_and_selector(self):
        viewer = self.make_viewer()
        viewer.max_bytes = 1
        viewer._load_level = Mock()

        viewer._dataset_changed(dataset=self.comparison)

        viewer._load_level.assert_not_called()
        self.assertEqual(viewer.current_dataset, self.primary)
        self.assertEqual(viewer.current_level, "20um")
        self.assertEqual(viewer.state.dataset, self.primary)
        self.assertEqual(viewer.state.level, "20um")
        self.assertIn("memory limit", viewer.state.error_message)
        self.assertFalse(viewer.state.busy)

    def test_failed_target_read_recovers_previous_catalog_and_level(self):
        viewer = self.make_viewer()

        def load_level(name, *, reset_camera=False):
            if viewer.current_dataset == self.comparison:
                viewer.current_level = None
                raise OSError("Micron voxels unavailable")
            viewer.current_level = name
            viewer.state.error_message = ""

        viewer._load_level = Mock(side_effect=load_level)

        viewer._dataset_changed(dataset=self.comparison)

        self.assertEqual(viewer._load_level.call_args_list, [
            call("1um", reset_camera=True), call("20um", reset_camera=True)
        ])
        self.assertEqual(viewer.catalog_path, Path(self.primary))
        self.assertEqual(viewer.current_level, "20um")
        self.assertEqual(viewer.state.dataset, self.primary)
        self.assertEqual(viewer.state.level, "20um")
        self.assertEqual(viewer.state.source_url, "https://example.org/coarse")
        self.assertEqual(viewer.state.error_message, "Micron voxels unavailable")
        self.assertFalse(viewer.state.busy)

    def test_level_changes_keep_intensity_and_world_distance_calibration(self):
        viewer = self.make_viewer()
        viewer._set_catalog(self.comparison)
        native = np.arange(256, dtype=np.uint8).reshape(4, 8, 8)
        viewer._calibrate_appearance(native)
        native_intensities = viewer.intensities

        viewer._calibrate_appearance(np.full((2, 2, 2), 100, dtype=np.uint8))

        self.assertEqual(viewer.intensities, native_intensities)
        self.assertEqual(viewer.properties.SetScalarOpacityUnitDistance.call_args_list, [call(1), call(1)])
        self.assertEqual(viewer.background_value, 0)
        self.assertEqual(len(viewer._appearance_calibrations), 1)
        viewer._set_catalog(self.primary)
        viewer._calibrate_appearance(np.full((4, 4, 4), 180, dtype=np.uint8))
        self.assertNotEqual(viewer.intensities, native_intensities)
        self.assertEqual(viewer.background_value, 255)
        viewer.properties.SetScalarOpacityUnitDistance.assert_called_with(20)
        viewer._set_catalog(self.comparison)
        viewer._calibrate_appearance(np.full((2, 2, 2), 90, dtype=np.uint8))
        self.assertEqual(viewer.intensities, native_intensities)

    def test_budget_forced_coarse_level_still_uses_native_opacity_distance(self):
        viewer = self.make_viewer()
        viewer.max_bytes = 8

        selected = viewer._set_catalog(self.comparison)
        viewer._calibrate_appearance(np.full((2, 2, 2), 100, dtype=np.uint8))

        self.assertEqual(selected, "5um")
        viewer.properties.SetScalarOpacityUnitDistance.assert_called_once_with(1)


class ViewerCliTests(unittest.TestCase):
    def test_compare_catalog_option_can_be_repeated(self):
        from openmind.__main__ import main

        with patch("sys.argv", ["openmind", "serve", "--catalog", "primary.json",
                                "--compare-catalog", "micro.json", "--compare-catalog", "other.json"]):
            with patch("openmind.viewer.serve") as start:
                main()
        self.assertEqual(start.call_args.args, (Path("primary.json"),))
        self.assertEqual(start.call_args.kwargs["comparison_catalogs"], [Path("micro.json"), Path("other.json")])



if __name__ == "__main__":
    unittest.main()
