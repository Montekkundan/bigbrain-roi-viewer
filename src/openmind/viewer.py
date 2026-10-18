"""Focused Trame/VTK viewer for a bounded, local histology region.

This viewer deliberately loads one selected pyramid level. It does not implement
the view-dependent brick cache required for whole-brain out-of-core rendering.
"""

from __future__ import annotations

import gc
import math
from pathlib import Path
import time
from uuid import uuid4


def camera_corner_planes(bounds, camera_xyz, pivot_xyz):
    """Define a nondegenerate crop box from the pivot toward the camera.

    VTK's inverse central-region flags remove this box and retain the other
    seven octants. Coincident plane pairs are avoided: they blank the GPU
    mapper even when other region flags are enabled.
    """
    return tuple(
        coordinate
        for axis, (camera, pivot) in enumerate(zip(camera_xyz, pivot_xyz))
        for coordinate in (
            (pivot, bounds[2 * axis + 1])
            if camera >= pivot
            else (bounds[2 * axis], pivot)
        )
    )


def physical_pivot(bounds, percentages):
    """Convert three bounded percentages to world coordinates."""
    return tuple(
        bounds[2 * axis]
        + (bounds[2 * axis + 1] - bounds[2 * axis])
        * min(100.0, max(0.0, float(percentages[axis])))
        / 100.0
        for axis in range(3)
    )


def voxel_cell_bounds(shape_xyz, spacing_xyz, origin_xyz):
    """Physical cell edges, identical across block-averaged pyramid levels."""
    return tuple(
        edge
        for size, spacing, origin in zip(shape_xyz, spacing_xyz, origin_xyz)
        for edge in (origin - spacing / 2, origin + (size - 0.5) * spacing)
    )


def spacing_label(spacing):
    return " × ".join(f"{float(value):.3g}" for value in spacing) + " µm"


class VolumeViewer:
    """Application object, also exposing its render window for smoke checks."""

    def __init__(self, catalog_path: Path, *, max_volume_mib=192, comparison_catalogs=()):
        import numpy as np
        import vtk
        from trame.app import get_server

        from .data import load_catalog
        from . import browser_memory

        self.np = np
        self.vtk = vtk
        primary = str(Path(catalog_path).resolve())
        self.max_bytes = int(float(max_volume_mib) * 1024**2)
        if self.max_bytes <= 0:
            raise ValueError("max_volume_mib must be positive")
        paths = dict.fromkeys([primary, *(str(Path(path).resolve()) for path in comparison_catalogs)])
        # Register metadata only. Scalar buffers are loaded for one selection.
        self.catalogs = {path: load_catalog(Path(path)) for path in paths}
        self.server = get_server(f"openmind-{uuid4().hex}", client_type="vue3")
        self.server.enable_module(browser_memory)
        self.state = self.server.state
        self.ctrl = self.server.controller
        self.view = None
        self.current_level = None
        self.current_dataset = None
        self._appearance_calibrations = {}
        self._array = self._image = self._vtk_scalars = None
        self._building = True
        self._last_stats = 0.0
        self._render_started = 0.0
        self._pivot = (0.0, 0.0, 0.0)
        self.bounds = (0.0,) * 6

        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(0.035, 0.045, 0.055)
        self.render_window = vtk.vtkRenderWindow()
        self.render_window.AddRenderer(self.renderer)
        self.render_window.SetSize(1100, 850)
        self.render_window.OffScreenRenderingOn()
        self.render_window.SetMultiSamples(0)
        self.interactor = vtk.vtkRenderWindowInteractor()
        self.interactor.SetRenderWindow(self.render_window)
        self.interactor.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())

        self.mapper = vtk.vtkGPUVolumeRayCastMapper()
        # This constrains the VTK texture budget, not total process memory.
        self.mapper.SetMaxMemoryInBytes(256 * 1024**2)
        self.mapper.SetMaxMemoryFraction(0.8)
        self.mapper.SetAutoAdjustSampleDistances(True)
        self.properties = vtk.vtkVolumeProperty()
        self.properties.SetInterpolationTypeToLinear()
        self.properties.ShadeOff()
        self.volume = vtk.vtkVolume()
        self.volume.SetMapper(self.mapper)
        self.volume.SetProperty(self.properties)
        self.renderer.AddVolume(self.volume)
        self.camera = self.renderer.GetActiveCamera()
        self.camera.AddObserver("ModifiedEvent", self._camera_changed)
        self.render_window.AddObserver("StartEvent", self._render_start)
        self.render_window.AddObserver("EndEvent", self._render_end)

        self.state.update(
            trame__title="Open the Mind | local histology",
            dataset_items=[
                {"title": str(catalog.get("name", Path(path).parent.name)), "value": path}
                for path, catalog in self.catalogs.items()
            ],
            cut_mode="corner",
            cut_x=50,
            cut_y=50,
            cut_z=50,
            preset="tissue",
            cut_mode_items=[{"title": "Camera-facing corner", "value": "corner"}, {"title": "XYZ crop planes", "value": "planes"}, {"title": "Show full region", "value": "none"}],
            preset_items=[{"title": "Tissue grayscale", "value": "tissue"}, {"title": "Dark Nissl structures", "value": "nissl"}],
            density=1.0,
            busy=False,
            error_message="",
            shape_label="",
            spacing_label="",
            extent_label="",
            loaded_mib=0.0,
            volume_loaded=False,
            rss_mib=None,
            peak_mib=None,
            render_ms=0.0,
            cut_position_label="",
            release_message="",
        )
        first = self._set_catalog(primary)
        self._load_level(str(first), reset_camera=True)
        self._build_ui()
        self.state.change("dataset")(self._dataset_changed)
        self.state.change("level")(self._level_changed)
        self.state.change("cut_mode", "cut_x", "cut_y", "cut_z")(self._cuts_changed)
        self.state.change("preset", "density")(self._appearance_changed)
        self.ctrl.on_server_ready.add(self.update)
        self._building = False

    def _estimated_bytes(self, level):
        shape = level.get("shape_zyx", level.get("shape", []))
        return math.prod(shape) * self.np.dtype(level.get("dtype", "uint8")).itemsize

    def _set_catalog(self, dataset, *, preferred_level=None):
        """Preflight metadata before replacing the active catalog context."""
        if dataset not in self.catalogs:
            raise ValueError(f"Unknown dataset: {dataset}")
        catalog = self.catalogs[dataset]
        levels = {str(level["name"]): level for level in catalog["levels"]}
        usable = [level for level in levels.values() if self._estimated_bytes(level) <= self.max_bytes]
        if not usable:
            raise ValueError("No catalog level fits the configured volume memory limit")
        first = min(usable, key=lambda level: math.prod(level["spacing_um_xyz"]))["name"]
        if preferred_level in levels and self._estimated_bytes(levels[preferred_level]) <= self.max_bytes:
            first = preferred_level
        source = catalog.get("source", {})
        if not isinstance(source, dict):
            source = {"url": str(source)}
        source_url = str(source.get("url", ""))
        finest = min(levels.values(), key=lambda level: math.prod(level["spacing_um_xyz"]))
        note = catalog.get("comparison_note") or (
            f'Native voxel spacing is {spacing_label(finest["spacing_um_xyz"])}. '
            "Coarser levels are derived from this same physical region."
        )
        self.catalog_path = Path(dataset)
        self.catalog = catalog
        self.levels = levels
        self.current_dataset = dataset
        self.state.update(
            dataset=dataset,
            dataset_name=str(catalog.get("name", "Histology region")),
            level=str(first),
            level_items=[
                {"title": f'{level["name"]} · {spacing_label(level["spacing_um_xyz"])}',
                 "value": str(level["name"])}
                for level in levels.values()
            ],
            source_url=source_url if source_url.startswith(("https://", "http://")) else "",
            source_title=str(source.get("title", source.get("name", "Dataset source"))),
            source_doi=str(source.get("doi", "")),
            comparison_note=str(note),
        )
        return str(first)

    def _dataset_changed(self, dataset=None, **_):
        if self._building or dataset == self.current_dataset:
            return
        previous_dataset, previous_level = self.current_dataset, self.current_level
        previous_selection = self.state.level
        self.state.busy = True
        self._building = True
        try:
            first = self._set_catalog(str(dataset))
            self._load_level(first, reset_camera=True)
            self.update()
        except (ValueError, OSError, MemoryError) as exc:
            message = str(exc)
            if self.current_dataset != previous_dataset:
                try:
                    self._set_catalog(previous_dataset, preferred_level=previous_level or previous_selection)
                    if previous_level is not None:
                        self._load_level(previous_level, reset_camera=True)
                except (ValueError, OSError, MemoryError) as recovery_error:
                    message += f" Recovery failed: {recovery_error}. No volume loaded."
            self.state.dataset = self.current_dataset
            self.state.level = self.current_level or previous_selection
            self.state.error_message = message
            self.update()
        finally:
            self._building = False
            self.state.busy = False

    def _calibrate_appearance(self, array):
        """Keep intensity and physical opacity calibration fixed within a dataset."""
        calibration = self._appearance_calibrations.get(self.current_dataset)
        if calibration is None:
            flattened = array.reshape(-1)
            sample = flattened[:: max(1, math.ceil(flattened.size / 1_000_000))]
            scalar_max = float(self.np.iinfo(array.dtype).max)
            background = float(self.catalog.get("background_value", scalar_max))
            tissue = sample[sample != background]
            intensities = (
                tuple(float(v) for v in self.np.percentile(tissue, [1, 20, 65, 95, 99.5]))
                if tissue.size else (0.0, 1.0, 2.0, 3.0, 4.0)
            )
            calibration = (scalar_max, background, intensities)
            # Store only scalar statistics, never the sampled voxel buffer.
            self._appearance_calibrations[self.current_dataset] = calibration
        self.scalar_max, self.background_value, self.intensities = calibration
        finest = min(self.levels.values(), key=lambda level: math.prod(level["spacing_um_xyz"]))
        self.properties.SetScalarOpacityUnitDistance(min(finest["spacing_um_xyz"]))

    def _load_level(self, name, *, reset_camera=False):
        from vtk.util.numpy_support import numpy_to_vtk

        from .data import load_level

        if name not in self.levels:
            raise ValueError(f"Unknown pyramid level: {name}")
        estimated = self._estimated_bytes(self.levels[name])
        if estimated > self.max_bytes:
            raise ValueError(
                f"{name} needs {estimated / 1024**2:.1f} MiB of scalar data; "
                f"the configured limit is {self.max_bytes / 1024**2:.0f} MiB."
            )
        self._release_loaded_volume()
        loaded = load_level(self.catalog_path, name, max_bytes=self.max_bytes)
        array = loaded.array
        if array.ndim == 4:
            if array.shape[0] != 1:
                raise ValueError("Only one scalar channel can be rendered")
            array = array[0]
        if array.ndim != 3 or array.dtype not in (self.np.dtype("uint8"), self.np.dtype("uint16")):
            raise ValueError("The viewer requires a 3D uint8 or uint16 scalar array")
        if not array.flags.c_contiguous:
            raise ValueError("The data loader must return a contiguous CZYX array")
        if array.nbytes > self.max_bytes:
            raise ValueError("Loaded scalar data exceeds the configured memory limit")
        self._array = array
        self._image = self.vtk.vtkImageData()
        self._image.SetDimensions(*reversed(array.shape))
        self._image.SetSpacing(*loaded.spacing_um_xyz)
        self._image.SetOrigin(*loaded.origin_um_xyz)
        self._vtk_scalars = numpy_to_vtk(array.reshape(-1), deep=False)
        self._vtk_scalars.SetName("histology")
        self._image.GetPointData().SetScalars(self._vtk_scalars)
        self.mapper.SetInputData(self._image)
        self.volume.VisibilityOn()
        self.bounds = self._image.GetBounds()
        self.cut_bounds = voxel_cell_bounds(
            tuple(reversed(array.shape)), loaded.spacing_um_xyz, loaded.origin_um_xyz
        )
        self.current_level = name
        self._calibrate_appearance(array)
        extent = [array.shape[2 - axis] * loaded.spacing_um_xyz[axis] for axis in range(3)]
        self.state.update(
            shape_label=" × ".join(str(v) for v in reversed(array.shape)) + " voxels",
            spacing_label=spacing_label(loaded.spacing_um_xyz),
            extent_label=" × ".join(f"{value / 1000:.3g}" for value in extent) + " mm",
            loaded_mib=array.nbytes / 1024**2,
            volume_loaded=True,
            release_message="",
            error_message="",
        )
        self.refresh_memory()
        self._apply_appearance()
        self._apply_cuts()
        if reset_camera:
            self.reset_camera()

    def _level_changed(self, level=None, **_):
        if self._building or level == self.current_level:
            return
        previous = self.current_level
        self.state.busy = True
        try:
            self._load_level(str(level))
            self.renderer.ResetCameraClippingRange()
            self.update()
        except (ValueError, OSError, MemoryError) as exc:
            message = str(exc)
            if self.current_level is None and previous is not None:
                try:
                    self._load_level(previous)
                except (ValueError, OSError, MemoryError) as recovery_error:
                    message += f" Recovery failed: {recovery_error}. No volume loaded."
            if self.current_level is not None:
                self.state.level = self.current_level
            self.state.error_message = message
            self.update()
        finally:
            self.state.busy = False

    def _cuts_changed(self, **_):
        if not self._building:
            self._apply_cuts()
            self.update()

    def _appearance_changed(self, **_):
        if not self._building:
            self._apply_appearance()
            self.update()

    def _apply_cuts(self):
        if self._image is None:
            return
        self._pivot = physical_pivot(self.cut_bounds, [self.state.cut_x, self.state.cut_y, self.state.cut_z])
        x, y, z = self._pivot
        self.state.cut_position_label = f"X {x / 1000:.3f} · Y {y / 1000:.3f} · Z {z / 1000:.3f} mm"
        if self.state.cut_mode == "none":
            self.mapper.CroppingOff()
        elif self.state.cut_mode == "planes":
            self.mapper.CroppingOn()
            self.mapper.SetCroppingRegionPlanes(self.bounds[0], x, self.bounds[2], y, self.bounds[4], z)
            self.mapper.SetCroppingRegionFlagsToSubVolume()
        else:
            self._apply_corner()

    def _apply_corner(self):
        planes = camera_corner_planes(self.bounds, self.camera.GetPosition(), self._pivot)
        if any(planes[2 * axis] >= planes[2 * axis + 1] for axis in range(3)):
            # Removing a zero-width corner removes no data.
            self.mapper.CroppingOff()
            return
        self.mapper.CroppingOn()
        if planes != self.mapper.GetCroppingRegionPlanes():
            self.mapper.SetCroppingRegionPlanes(*planes)
        self.mapper.SetCroppingRegionFlags(((1 << 27) - 1) ^ (1 << 13))

    def _camera_changed(self, *_):
        if self._image is not None and self.state.cut_mode == "corner":
            self._apply_corner()

    def _apply_appearance(self):
        if self._array is None:
            return
        low, p20, p65, high, top = self.intensities
        density = float(self.state.density)
        colors = self.vtk.vtkColorTransferFunction()
        opacity = self.vtk.vtkPiecewiseFunction()
        bright_background = self.background_value == self.scalar_max
        if self.state.preset == "nissl":
            # The public BigBrain source has 255-valued exterior. Emphasise
            # darker stain while keeping that exterior fully transparent.
            colors.AddRGBPoint(low, 0.88, 0.84, 0.75)
            colors.AddRGBPoint(top, 0.22, 0.25, 0.28)
            opacity.AddPoint(0, 0.16 * density if bright_background else 0)
            opacity.AddPoint(low, 0.16 * density)
            opacity.AddPoint(p20, 0.12 * density)
            opacity.AddPoint(p65, 0.035 * density)
            opacity.AddPoint(top, 0.002 * density)
            opacity.AddPoint(self.scalar_max, 0)
        else:
            colors.AddRGBPoint(low, 0.12, 0.16, 0.19)
            colors.AddRGBPoint(p20, 0.42, 0.48, 0.51)
            colors.AddRGBPoint(p65, 0.77, 0.80, 0.79)
            colors.AddRGBPoint(top, 0.98, 0.96, 0.88)
            opacity.AddPoint(0, 0.20 * density if bright_background else 0)
            opacity.AddPoint(low, 0.20 * density)
            opacity.AddPoint(p20, 0.16 * density)
            opacity.AddPoint(p65, 0.12 * density)
            opacity.AddPoint(high, 0.08 * density)
            opacity.AddPoint(top, 0.04 * density)
            opacity.AddPoint(self.scalar_max, 0 if bright_background else 0.04 * density)
        self.mapper.SetBlendModeToComposite()
        self.properties.SetColor(colors)
        self.properties.SetScalarOpacity(opacity)

    def reset_camera(self, *_):
        if self._image is None:
            return
        self.camera.SetPosition(1.0, -1.0, 0.8)
        self.camera.SetViewUp(0, 0, 1)
        self.renderer.ResetCamera()
        center = [(self.bounds[2 * axis] + self.bounds[2 * axis + 1]) / 2 for axis in range(3)]
        distance = self.camera.GetDistance()
        self.camera.SetFocalPoint(*center)
        direction = (0.65, -0.8, 0.62)
        length = math.sqrt(sum(value**2 for value in direction))
        self.camera.SetPosition(*(center[axis] + distance * direction[axis] / length for axis in range(3)))
        self.renderer.ResetCameraClippingRange()
        self._camera_changed()
        self.update()

    def update(self, **_):
        if self.view is not None:
            self.view.update()

    def _render_start(self, *_):
        self._render_started = time.perf_counter()

    def _render_end(self, *_):
        now = time.perf_counter()
        if now - self._last_stats >= 0.25:
            self._last_stats = now
            with self.state:
                self.state.render_ms = round((now - self._render_started) * 1000, 1)
                self.refresh_memory()

    def refresh_memory(self, **_):
        """Sample current and historical process RAM without triggering a render."""
        from .memory import current_rss_bytes, peak_rss_bytes

        current, peak = current_rss_bytes(), peak_rss_bytes()
        with self.state:
            self.state.rss_mib = None if current is None else current / 1024**2
            self.state.peak_mib = None if peak is None else peak / 1024**2

    def _release_loaded_volume(self):
        """Drop owned voxel references and request release of VTK graphics resources."""
        self.volume.VisibilityOff()
        self.mapper.ReleaseGraphicsResources(self.render_window)
        self.mapper.RemoveAllInputs()
        if self._image is not None:
            self._image.GetPointData().SetScalars(None)
        self._array = self._image = self._vtk_scalars = None
        self.current_level = None
        self.state.update(loaded_mib=0.0, volume_loaded=False, shape_label="No volume loaded")
        gc.collect()

    def release_volume(self, **_):
        released = float(self.state.loaded_mib)
        self._release_loaded_volume()
        self.state.release_message = (
            f"Detached {released:.2f} MiB of voxel data and requested graphics release. "
            "VTK caches, browser buffers, allocators and drivers may retain memory."
        )
        self.refresh_memory()

    def reload_volume(self, **_):
        if self.current_level is None:
            self._level_changed(level=self.state.level)

    def _build_ui(self):
        from trame.ui.vuetify3 import SinglePageWithDrawerLayout
        from trame.widgets import client, html, vtk as vtk_widgets, vuetify3 as v3
        from .browser_memory import widget as browser_memory_widget

        with SinglePageWithDrawerLayout(self.server, width=326, vuetify_config={"theme": {"defaultTheme": "dark"}}) as layout:
            client.Style("""
                html,body,#app{height:100%;overflow:hidden}
                .v-application{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#11161c}
                .v-app-bar{background:#171d25!important;border-bottom:1px solid #2d3540;box-shadow:none!important}
                .v-navigation-drawer{background:#171d25!important;border-right:1px solid #2d3540!important}
                .om-section{padding:22px 24px;border-bottom:1px solid #2d3540}
                .om-eyebrow{font-size:10px;letter-spacing:.14em;color:#96a6b8;font-weight:650;text-transform:uppercase;margin-bottom:14px}
                .om-small{font-size:12px;color:#a7b3c2;line-height:1.65}
                .om-value{font-size:13px;color:#e1e8ef;line-height:1.6}
                .om-label{font-size:11px;color:#8e9bab;margin-top:12px}
                .om-canvas{position:relative;height:calc(100vh - 64px);width:100%;background:#090c10}
                .om-canvas-hint{position:absolute;bottom:24px;left:26px;pointer-events:none;font-size:12px;color:#8291a1;letter-spacing:.015em}
                .om-canvas-tag{position:absolute;top:24px;left:26px;pointer-events:none;font-size:11px;color:#a5b4c4;letter-spacing:.1em;text-transform:uppercase}
                .om-stats{position:absolute;top:24px;right:24px;width:295px;max-width:calc(100% - 48px);background:#171d25ed;border:1px solid #303b47;padding:12px 15px;border-radius:9px;font-size:11px;color:#95a5b7;line-height:1.9}
                .om-stats b{color:#e4ebf1;font-weight:500;font-variant-numeric:tabular-nums}
                .om-memory-title{font-size:10px;letter-spacing:.1em;color:#b3c4d4;margin-bottom:5px}
                .om-browser-memory{margin-top:10px;padding-top:10px;border-top:1px solid #303b47}
                .om-memory-note{font-size:10px;line-height:1.5;color:#8c9caf;margin-top:5px}
                .om-memory-chart{width:100%;height:32px;margin-top:5px;border-bottom:1px solid #303b47}
                .om-link{color:#b8d1dd;text-decoration:none;font-size:12px}
                .v-slider .v-label{font-size:12px}.v-toolbar-title{font-size:18px;font-weight:600;letter-spacing:-.03em}
            """)
            layout.title.set_text("Open the Mind")
            layout.footer.hide()
            with layout.toolbar:
                v3.VChip("LOCAL REGION", size="x-small", variant="outlined", color="blue-grey-lighten-2", classes="mr-4", style="letter-spacing:.12em")
                v3.VSpacer()
                v3.VBtn("Release data", prepend_icon="mdi-memory", variant="text", size="small", click=self.release_volume, disabled=("busy || !volume_loaded",))
                v3.VBtn("Reload data", prepend_icon="mdi-reload", variant="text", size="small", click=self.reload_volume, disabled=("busy || volume_loaded",))
                v3.VBtn("Reset view", prepend_icon="mdi-camera-retake-outline", variant="text", size="small", click=self.reset_camera, disabled=("!volume_loaded",), classes="mr-3")
            with layout.drawer:
                with html.Div(classes="om-section"):
                    html.Div("Volume", classes="om-eyebrow")
                    v3.VSelect(v_model=("dataset",), items=("dataset_items",), label="Dataset region", variant="outlined", density="compact", hide_details=True, disabled=("busy",), v_if=("dataset_items.length > 1",), classes="mb-4")
                    html.Div("{{ dataset_name }}", classes="om-value", style="font-size:15px;font-weight:550;margin-bottom:16px")
                    v3.VSelect(v_model=("level",), items=("level_items",), label="Pyramid level", variant="outlined", density="compact", hide_details=True, disabled=("busy",))
                    html.Div("Voxel spacing · X / Y / Z", classes="om-label")
                    html.Div("{{ spacing_label }}", classes="om-value")
                    html.Div("Region size · X / Y / Z", classes="om-label")
                    html.Div("{{ extent_label }}", classes="om-value")
                    html.Div("{{ shape_label }}", classes="om-small", style="margin-top:5px")
                    html.Div("{{ comparison_note }}", classes="om-small", style="margin-top:12px")
                    html.Div("Datasets show different regions and are not atlas aligned.", v_if=("dataset_items.length > 1",), classes="om-small", style="margin-top:8px")
                with html.Div(classes="om-section"):
                    html.Div("Cut into the tissue", classes="om-eyebrow")
                    v3.VSelect(v_model=("cut_mode",), items=("cut_mode_items",), variant="outlined", density="compact", hide_details=True, classes="mb-5")
                    for axis, color in (("x", "#c99b94"), ("y", "#9ab09b"), ("z", "#95acc9")):
                        v3.VSlider(v_model=(f"cut_{axis}",), label=axis.upper(), min=0, max=100, step=1, color=color, hide_details=True, thumb_label=True, density="compact", disabled=("cut_mode === 'none'",), classes="mb-2")
                    html.Div("{{ cut_position_label }}", classes="om-small", style="font-size:10px;margin-top:12px")
                    html.Div("The corner follows the camera as you orbit.", v_if=("cut_mode === 'corner'",), classes="om-small", style="margin-top:8px")
                with html.Div(classes="om-section"):
                    html.Div("Appearance", classes="om-eyebrow")
                    v3.VSelect(v_model=("preset",), items=("preset_items",), variant="outlined", density="compact", hide_details=True, classes="mb-4")
                    v3.VSlider(v_model=("density",), label="Opacity", min=0.15, max=3, step=0.05, color="blue-grey-lighten-2", hide_details=True, thumb_label=True, density="compact")
                with html.Div(classes="om-section", style="border-bottom:0"):
                    html.Div("About this view", classes="om-eyebrow")
                    html.Div("Real reconstructed histology, a bounded region. The selected level is loaded into memory; whole-brain brick streaming is future work.", classes="om-small")
                    html.A("{{ source_title }} ↗", href=("source_url",), v_if=("!!source_url",), target="_blank", rel="noopener noreferrer", classes="om-link", style="display:block;margin-top:12px")
                    html.Div("DOI: {{ source_doi }}", v_if=("!!source_doi",), classes="om-small", style="font-size:10px;margin-top:6px;overflow-wrap:anywhere")
            with layout.content:
                with html.Div(classes="om-canvas"):
                    self.view = vtk_widgets.VtkRemoteView(self.render_window, ref="brain_view", v_if=("volume_loaded",), interactive_quality=80, interactive_ratio=0.7, still_quality=95, still_ratio=1, style="width:100%;height:100%")
                    with html.Div(v_if=("!volume_loaded",), style="position:absolute;top:50%;left:8%;right:8%;color:#b3c4d4;text-align:center"):
                        html.Div("View released", style="font-size:22px;margin-bottom:12px")
                        html.Div("{{ release_message }}", classes="om-small")
                        v3.VBtn("Reload selected data", click=self.reload_volume, variant="outlined", classes="mt-5", disabled=("busy",))
                    html.Div("SERVER-RENDERED HISTOLOGY", classes="om-canvas-tag")
                    with html.Div(classes="om-stats"):
                        html.Div("SERVER · THIS MAC / REMOTE GPU HOST", classes="om-memory-title")
                        html.Div('Loaded voxel data <b>{{ loaded_mib === 0 ? "0 B" : (loaded_mib >= 0.01 ? loaded_mib.toFixed(2) + " MiB" : (loaded_mib * 1024).toFixed(1) + " KiB") }}</b>')
                        html.Div('Current process RAM <b>{{ rss_mib === null ? "Unavailable" : rss_mib.toFixed(1) + " MiB" }}</b>')
                        html.Div('Historical process peak <b>{{ peak_mib === null ? "Unavailable" : peak_mib.toFixed(1) + " MiB" }}</b>')
                        html.Div('VTK render <b>{{ render_ms.toFixed(1) }} ms</b>')
                        html.Div("Orbiting keeps this fixed crop loaded. Peak never decreases. Exact GPU allocation is unavailable.", classes="om-memory-note")
                        browser_memory_widget(sample_memory=self.refresh_memory)
                    html.Div("Drag to orbit · Shift + drag to pan · Scroll to zoom", classes="om-canvas-hint")
                    v3.VAlert(text=("error_message",), v_if=("!!error_message",), type="error", variant="tonal", style="position:absolute;bottom:70px;left:24px;right:24px")
                    v3.VProgressLinear(indeterminate=True, v_if=("busy",), color="blue-grey-lighten-2", style="position:absolute;top:0;left:0;right:0")


def serve(catalog_path: Path, *, host="127.0.0.1", port=8081, max_volume_mib=192, open_browser=False, comparison_catalogs=()):
    """Run the local browser viewer until interrupted."""
    app = VolumeViewer(catalog_path, max_volume_mib=max_volume_mib, comparison_catalogs=comparison_catalogs)
    app.server.start(host=host, port=int(port), open_browser=open_browser, timeout=0)
