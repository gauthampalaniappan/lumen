"""GTK4 / libadwaita interface for Lumen."""

from __future__ import annotations

import os
import threading
import traceback
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango

from lumen.editors import resolve_editor
from lumen.imaging import is_raw_path, EXPORT_FORMATS, PREVIEW_LIVE_EDGE, crop_fits, current_size, edit_sig, ensure_thumb, export_copy, flatten_rgb, jpeg_bytes, neutral_tone, ops_without_tone, prepare_editor_file, preview_base_image, render, render_tone_on, rgb_histogram, scaled_thumb_bytes
from lumen.photobook import describe_book, load_pages, run_print
from lumen.store import THUMB_SIZE_MAX, THUMB_SIZE_MIN, Store


THEME_SCHEMES = {
    "system": Adw.ColorScheme.DEFAULT,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
}

EDITORS = (
    ("darktable", "Darktable", "Open the selected photos in Darktable"),
    ("gimp", "GIMP", "Open the selected photos in GIMP"),
)


def apply_color_scheme(theme: str) -> None:
    scheme = THEME_SCHEMES.get(theme, Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


def _stars(rating: int) -> str:
    rating = max(0, min(5, int(rating)))
    return "★" * rating + "☆" * (5 - rating)


class CropDialog(Gtk.Window):
    def __init__(self, parent: Gtk.Window, size: tuple[int, int], on_apply) -> None:
        super().__init__(transient_for=parent, modal=True, title="Crop")
        self._on_apply = on_apply
        self.set_destroy_with_parent(True)
        self.set_default_size(360, 280)
        width, height = size
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        note = Gtk.Label(
            label=f"Current size {width} × {height}. The crop is applied to the edited image, not saved over the original.",
            wrap=True,
            xalign=0,
        )
        box.append(note)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        self._spins = {}
        for row, (key, label, upper, value) in enumerate(
            (
                ("x", "X", width - 1, 0),
                ("y", "Y", height - 1, 0),
                ("w", "Width", width, width),
                ("h", "Height", height, height),
            )
        ):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            adj = Gtk.Adjustment(value=value, lower=0, upper=max(upper, 0), step_increment=1, page_increment=10)
            spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0)
            spin.set_hexpand(True)
            self._spins[key] = spin
            grid.attach(spin, 1, row, 1, 1)
        box.append(grid)
        self._error = Gtk.Label(label="", xalign=0)
        self._error.add_css_class("error")
        box.append(self._error)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        apply_btn = Gtk.Button(label="Crop")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._apply)
        buttons.append(cancel)
        buttons.append(apply_btn)
        box.append(buttons)
        self.set_child(box)
        self._size = size

    def _apply(self, *_args) -> None:
        x = self._spins["x"].get_value_as_int()
        y = self._spins["y"].get_value_as_int()
        w = self._spins["w"].get_value_as_int()
        h = self._spins["h"].get_value_as_int()
        if not crop_fits(self._size, x, y, w, h):
            width, height = self._size
            self._error.set_text(f"Crop must sit inside {width} × {height}.")
            return
        self._on_apply(x, y, w, h)
        self.close()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app)
        self.app = app
        self.store = Store()
        apply_color_scheme(self.store.get_theme())
        self.set_title("Lumen")
        self._photos: list[dict] = []
        self._filter: dict = {}
        self._search = ""
        self._scan_gen = 0
        self._thumb_gen = 0
        self._view_gen = 0
        self._pics: dict[int, Gtk.Picture] = {}
        self._index = 0
        self.photo: dict | None = None
        self._texture = None
        self._texture_bytes = None
        self._fit = True
        self._zoom = 1.0
        self._self_check_pending = False
        self._did_self_check = False
        self._screenshot_done = False
        self._shot_tries = 0
        self._search_src = 0
        self._checked: set[int] = set()
        self._checks: dict[int, Gtk.CheckButton] = {}
        self._anchor: int | None = None
        self._updating_checks = False
        self._press_xy = (0.0, 0.0)
        self._press_hadj = 0.0
        self._press_vadj = 0.0
        self._suspend_scroll_focus = False
        self._rubber_on = False
        self._ignore_drag = False
        self._rubber_base: list[int] = []
        self._hold = False
        self._tray_order: list[int] = []
        self._tray_gen = 0
        self._tray_pics: dict[int, Gtk.Picture] = {}
        self._tray_sync = False
        self._pending_export: list[dict] = []
        self._thumb_size = self.store.get_thumb_size()
        self._histogram = None
        self._tone_timer = 0
        self._tone_sync = False
        self._tone_pending = None
        self._tone_scales = {}
        self._tone_readouts = {}
        # Un-toned preview (about 1200px) reused while sliders move.
        self._tone_base = None
        self._tone_base_key = None
        self._tone_base_lock = threading.Lock()
        self._live_busy = False
        self._live_again = False
        self._live_serial = 0
        self._live_applied = 0
        self._full_epoch = 0
        self._preview_kind = None
        self._build()
        self._fit_to_monitor()

    def _fit_to_monitor(self) -> None:
        width, height = 1100, 720
        display = Gdk.Display.get_default()
        if display is not None:
            monitors = display.get_monitors()
            if monitors is not None and monitors.get_n_items() > 0:
                geo = monitors.get_item(0).get_geometry()
                gw = getattr(geo, "width", None)
                gh = getattr(geo, "height", None)
                if gw is None:
                    gw, gh = geo[2], geo[3]
                width = min(width, max(640, gw - 48))
                height = min(height, max(480, gh - 64))
        self.set_default_size(width, height)

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(
            """
            .lumen-library-title { font-weight: 700; }
            .lumen-bottom { padding: 2px; }
            .lumen-edit-panel {
              background-color: alpha(@window_fg_color, 0.05);
              border-left: 1px solid alpha(@window_fg_color, 0.14);
            }
            .lumen-edit-panel scale {
              min-height: 18px;
              padding-top: 0;
              padding-bottom: 0;
            }
            .lumen-tone-value { font-family: monospace; }
            .lumen-viewer-nav { padding-bottom: 2px; }
            """
        )
        display = Gdk.Display.get_default()
        if display is None:
            return
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build(self) -> None:
        self._install_css()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle.new("Lumen", "Add a folder to begin")
        header.set_title_widget(self.title_widget)
        add = Gtk.Button(label="Add folder")
        add.set_tooltip_text("Scan a folder of photos")
        add.connect("clicked", self._on_add_folder)
        header.pack_start(add)
        import_btn = Gtk.Button(label="Import")
        import_btn.set_tooltip_text("Import from a camera, SD card, or folder")
        import_btn.connect("clicked", self._on_import)
        header.pack_start(import_btn)
        backup_btn = Gtk.Button(label="Backup")
        backup_btn.set_tooltip_text("Copy the library to a folder, NAS, or cloud sync folder")
        backup_btn.connect("clicked", self._on_backup)
        header.pack_start(backup_btn)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search filename, folder, album, or tag")
        self.search_entry.set_size_request(280, -1)
        self.search_entry.connect("search-changed", self._on_search)
        header.pack_end(self.search_entry)
        self._build_theme_menu(header)

        self.side = Gtk.ListBox()
        self.side.add_css_class("navigation-sidebar")
        self.side.connect("row-activated", self._on_side)
        side_scroll = Gtk.ScrolledWindow()
        side_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side_scroll.set_vexpand(True)
        side_scroll.set_child(self.side)
        library_title = Gtk.Label(label="Library", xalign=0)
        library_title.add_css_class("title-4")
        library_title.add_css_class("lumen-library-title")
        library_title.set_margin_start(12)
        library_title.set_margin_end(12)
        library_title.set_margin_top(10)
        library_title.set_margin_bottom(2)
        side_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side_wrap.set_size_request(260, -1)
        side_wrap.append(library_title)
        side_wrap.append(side_scroll)

        self.flow = Gtk.FlowBox()
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_homogeneous(True)
        self.flow.set_min_children_per_line(1)
        self.flow.set_max_children_per_line(24)
        self.flow.set_column_spacing(8)
        self.flow.set_row_spacing(8)
        self.flow.set_margin_start(12)
        self.flow.set_margin_end(12)
        self.flow.set_margin_top(12)
        self.flow.set_margin_bottom(12)
        # Click selects into the photo tray. Double-click still opens the viewer.
        self.flow.set_activate_on_single_click(False)
        self.flow.set_focusable(True)
        self.flow.connect("child-activated", self._on_thumb_activated)
        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_rubber_begin)
        drag.connect("drag-update", self._on_rubber_update)
        drag.connect("drag-end", self._on_rubber_end)
        self.flow.add_controller(drag)
        flow_scroll = Gtk.ScrolledWindow()
        flow_scroll.set_child(self.flow)
        self.empty_label = Gtk.Label(label="No photos yet.\nUse Import or Add folder, or start with --library DIR.")
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.empty_label.set_wrap(True)
        grid_overlay = Gtk.Overlay()
        grid_overlay.set_child(flow_scroll)
        self.empty_label.set_halign(Gtk.Align.CENTER)
        self.empty_label.set_valign(Gtk.Align.CENTER)
        grid_overlay.add_overlay(self.empty_label)
        self.grid_overlay = grid_overlay
        self.band = Gtk.DrawingArea()
        self.band.set_can_target(False)
        self.band.set_focusable(False)
        self.band.set_hexpand(False)
        self.band.set_vexpand(False)
        self.band.set_halign(Gtk.Align.START)
        self.band.set_valign(Gtk.Align.START)
        self.band.set_visible(False)
        self.band.set_draw_func(self._draw_band)
        grid_overlay.add_overlay(self.band)

        self.viewer = self._build_viewer()
        self.stack = Gtk.Stack()
        self.stack.add_named(grid_overlay, "grid")
        self.stack.add_named(self.viewer, "viewer")

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_start_child(side_wrap)
        paned.set_end_child(self.stack)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(True)
        paned.set_position(260)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(paned)
        toolbar.add_bottom_bar(self._build_bottom_bar())
        self.set_content(toolbar)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self._rebuild_sidebar()

    def _build_viewer(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        nav = Gtk.Box(spacing=6)
        nav.add_css_class("lumen-viewer-nav")
        nav.set_margin_start(8)
        nav.set_margin_end(8)
        nav.set_margin_top(6)
        nav.set_margin_bottom(4)

        def add_button(label: str, tip: str, handler) -> Gtk.Button:
            button = Gtk.Button(label=label)
            button.set_tooltip_text(tip)
            button.connect("clicked", handler)
            return button

        library = add_button("Library", "Back to the library (Esc)", lambda *_: self.show_grid())
        nav.append(library)
        self.prev_btn = add_button("Previous", "Previous photo (Left)", lambda *_: self.show_prev())
        self.next_btn = add_button("Next", "Next photo (Right)", lambda *_: self.show_next())
        nav.append(self.prev_btn)
        nav.append(self.next_btn)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        nav.append(spacer)
        nav.append(add_button("Fit", "Fit the photo in the window (0)", lambda *_: self.zoom_fit()))
        nav.append(add_button("Zoom out", "Zoom out (-)", lambda *_: self.zoom_out()))
        nav.append(add_button("Zoom in", "Zoom in (+)", lambda *_: self.zoom_in()))
        page.append(nav)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.set_hexpand(True)
        body.set_vexpand(True)

        photo_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        photo_col.set_hexpand(True)
        photo_col.set_vexpand(True)

        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        self.picture.set_halign(Gtk.Align.CENTER)
        self.picture.set_valign(Gtk.Align.CENTER)
        self.image_bin = Gtk.Box()
        self.image_bin.set_hexpand(True)
        self.image_bin.set_vexpand(True)
        self.image_bin.append(self.picture)
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_hexpand(True)
        self.scrolled.set_vexpand(True)
        photo_col.append(self.image_bin)

        tools = Adw.WrapBox()
        tools.set_margin_start(8)
        tools.set_margin_end(8)
        tools.set_margin_top(4)
        tools.set_margin_bottom(4)
        tools.set_child_spacing(6)
        tools.set_line_spacing(6)
        rotate = Gtk.Box(spacing=0)
        rotate.add_css_class("linked")
        rotate.append(add_button("Rotate left", "Rotate left, stored as an edit", lambda *_: self._edit_rotate("left")))
        rotate.append(add_button("Rotate right", "Rotate right, stored as an edit", lambda *_: self._edit_rotate("right")))
        tools.append(rotate)
        tools.append(add_button("Crop", "Crop with x, y, width, and height", lambda *_: self._open_crop()))
        enhance = add_button("Enhance", "Autocontrast and a mild color bump", lambda *_: self._edit_enhance())
        enhance.add_css_class("suggested-action")
        tools.append(enhance)
        tools.append(add_button("Revert all", "Drop rotate, crop, enhance, and adjustments", lambda *_: self._edit_revert()))
        tools.append(add_button("Export copy", "Save a new file with edits applied", lambda *_: self._export()))
        photo_col.append(tools)

        meta = Gtk.Box(spacing=6)
        meta.set_margin_start(8)
        meta.set_margin_end(8)
        meta.set_margin_bottom(6)
        self.star_buttons = []
        for value in range(1, 6):
            button = Gtk.Button(label="☆")
            button.set_tooltip_text(f"Rate {value}")
            button.connect("clicked", self._on_star, value)
            self.star_buttons.append(button)
            meta.append(button)
        clear = Gtk.Button(label="Clear")
        clear.set_tooltip_text("Clear the rating")
        clear.connect("clicked", self._on_star, 0)
        meta.append(clear)
        self.tags_entry = Gtk.Entry(placeholder_text="Tags, comma-separated")
        self.tags_entry.set_hexpand(True)
        self.tags_entry.set_tooltip_text("Free-text tags. Press Enter to save.")
        self.tags_entry.connect("activate", lambda *_: self._save_tags())
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._save_tags())
        self.tags_entry.add_controller(focus)
        meta.append(self.tags_entry)
        self.album_btn = Gtk.MenuButton(label="Album")
        self.album_pop = Gtk.Popover()
        self.album_btn.set_popover(self.album_pop)
        self.album_pop.connect("show", self._rebuild_album_popover)
        meta.append(self.album_btn)
        photo_col.append(meta)

        body.append(photo_col)
        body.append(self._build_edit_panel())
        page.set_vexpand(True)
        page.append(body)
        return page

    def _build_edit_panel(self) -> Gtk.Widget:
        """Right-hand adjustments. Sliders map to a stored tone edit, not a copy of the file."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("lumen-edit-panel")
        outer.set_size_request(248, -1)
        outer.set_vexpand(True)
        outer.set_hexpand(False)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        # Natural height would force the window to grow and then clip the sliders.
        scroll.set_propagate_natural_height(False)
        scroll.set_min_content_height(120)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.set_margin_top(10)
        inner.set_margin_bottom(12)

        title = Gtk.Label(label="Adjust", xalign=0)
        title.add_css_class("title-4")
        inner.append(title)

        self.histogram = Gtk.DrawingArea()
        self.histogram.set_size_request(220, 76)
        self.histogram.set_hexpand(True)
        self.histogram.set_draw_func(self._draw_histogram)
        frame = Gtk.Frame()
        frame.set_child(self.histogram)
        inner.append(frame)

        specs = (
            ("exposure", "Exposure", -2.0, 2.0, 0.05, 0.25, "Brighten or darken the whole photo, in stops"),
            ("contrast", "Contrast", -100.0, 100.0, 1.0, 10.0, "Push tones away from mid gray, or flatten them"),
            ("highlights", "Highlights", -100.0, 100.0, 1.0, 10.0, "Lift or pull down the bright parts"),
            ("shadows", "Shadows", -100.0, 100.0, 1.0, 10.0, "Lift or crush the dark parts"),
            ("whites", "Whites", -100.0, 100.0, 1.0, 10.0, "Move the brightest end"),
            ("blacks", "Blacks", -100.0, 100.0, 1.0, 10.0, "Move the darkest end"),
            ("saturation", "Saturation", -100.0, 100.0, 1.0, 10.0, "Color strength. -100 is grayscale"),
        )
        light = Gtk.Label(label="Light", xalign=0)
        light.add_css_class("heading")
        light.set_margin_top(6)
        inner.append(light)
        for spec in specs:
            if spec[0] == "saturation":
                color = Gtk.Label(label="Color", xalign=0)
                color.add_css_class("heading")
                color.set_margin_top(8)
                inner.append(color)
            inner.append(self._tone_row(*spec))

        scroll.set_child(inner)
        outer.append(scroll)

        reset_row = Gtk.Box(spacing=8)
        reset_row.set_margin_start(12)
        reset_row.set_margin_end(12)
        reset_row.set_margin_top(6)
        reset_row.set_margin_bottom(10)
        note = Gtk.Label(label="Does not change the file", xalign=0)
        note.add_css_class("dim-label")
        note.set_hexpand(True)
        note.set_wrap(True)
        reset_row.append(note)
        reset = Gtk.Button(label="Reset")
        reset.set_tooltip_text("Clear these sliders. Rotate, crop, and enhance stay.")
        reset.connect("clicked", lambda *_: self._reset_tone())
        reset_row.append(reset)
        outer.append(reset_row)
        return outer

    def _tone_row(
        self,
        key: str,
        label: str,
        lower: float,
        upper: float,
        step: float,
        page: float,
        tip: str,
    ) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        head = Gtk.Box(spacing=8)
        name = Gtk.Label(label=label, xalign=0)
        name.set_hexpand(True)
        name.set_halign(Gtk.Align.START)
        readout = Gtk.Label(label=self._format_tone(key, 0.0), xalign=1)
        readout.add_css_class("dim-label")
        readout.add_css_class("lumen-tone-value")
        readout.set_width_chars(6)
        head.append(name)
        head.append(readout)
        adjustment = Gtk.Adjustment(
            value=0.0,
            lower=lower,
            upper=upper,
            step_increment=step,
            page_increment=page,
        )
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adjustment)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_tooltip_text(tip)
        # value-changed fires while the slider is dragged, not only on release.
        scale.connect("value-changed", self._on_tone_changed, key)
        self._tone_scales[key] = scale
        self._tone_readouts[key] = readout
        box.append(head)
        box.append(scale)
        return box

    def _format_tone(self, key: str, value: float) -> str:
        if key == "exposure":
            return f"{value:+.2f}"
        return f"{value:+.0f}"

    def _tone_values(self) -> dict[str, float]:
        values = neutral_tone()
        for key, scale in self._tone_scales.items():
            number = float(scale.get_value())
            if key == "exposure":
                number = round(number, 2)
            else:
                number = float(round(number))
            values[key] = number
        return values

    def _on_tone_changed(self, scale, key: str) -> None:
        number = float(scale.get_value())
        if key != "exposure":
            number = float(round(number))
        self._tone_readouts[key].set_label(self._format_tone(key, number))
        if self._tone_sync:
            return
        # Live (downscaled) while the value is moving. Full size after it stops.
        self._arm_tone_settle()
        self._queue_live_tone()

    def _arm_tone_settle(self) -> None:
        """Full-resolution preview once the slider has been still. Dragging postpones it."""
        if self._tone_timer:
            GLib.source_remove(self._tone_timer)
        self._tone_timer = GLib.timeout_add(350, self._tone_settled)

    def _tone_settled(self) -> bool:
        # This source is already finishing. Do not remove it again.
        self._tone_timer = 0
        self._commit_tone(full=True)
        return False

    def _on_tone_slider_released(self, *_args) -> bool:
        """Button up, or a test standing in for it. Swap in the full-size preview."""
        if self._tone_timer:
            GLib.source_remove(self._tone_timer)
            self._tone_timer = 0
        self._commit_tone(full=True)
        return False

    def _commit_tone(self, full: bool) -> None:
        if self._tone_timer:
            GLib.source_remove(self._tone_timer)
            self._tone_timer = 0
        self._live_again = False
        if not self.photo:
            self._tone_pending = None
            return
        photo_id = int(self.photo["id"])
        values = self._tone_values()
        self._tone_pending = None
        self.store.set_tone(photo_id, values)
        if full and self.stack.get_visible_child_name() == "viewer":
            self._load_view()

    def _queue_live_tone(self) -> None:
        if not self.photo:
            return
        if self.stack.get_visible_child_name() != "viewer":
            return
        self._tone_pending = (int(self.photo["id"]), self._tone_values())
        if self._live_busy:
            self._live_again = True
            return
        self._start_live_tone()

    def _start_live_tone(self) -> None:
        pending = self._tone_pending
        if not pending or not self.photo:
            return
        photo_id, values = pending
        if int(self.photo["id"]) != photo_id:
            return
        path = self.photo["path"]
        mtime = int(self.photo["mtime_ns"])
        geometric = ops_without_tone(self.store.get_edits(photo_id))
        key = (str(path), mtime, edit_sig(geometric))
        with self._tone_base_lock:
            cached = self._tone_base.copy() if self._tone_base is not None and self._tone_base_key == key else None
        self._live_busy = True
        self._live_again = False
        self._live_serial += 1
        serial = self._live_serial
        epoch = self._full_epoch
        # Stored edits only. The original file is not written.
        self.store.set_tone(photo_id, values)
        self._tone_pending = None

        def work() -> None:
            built = None
            try:
                base = cached
                if base is None:
                    base = preview_base_image(path, geometric, PREVIEW_LIVE_EDGE)
                    built = base
                image = render_tone_on(base, values)
                histogram = rgb_histogram(image)
                data = jpeg_bytes(image, quality=82)
                error = None
            except Exception as exc:
                traceback.print_exc()
                built = None
                data = None
                histogram = None
                error = str(exc)
            GLib.idle_add(
                self._live_ready,
                serial,
                epoch,
                photo_id,
                key,
                built,
                data,
                histogram,
                error,
            )

        threading.Thread(target=work, name="lumen-live", daemon=True).start()

    def _live_ready(self, serial, epoch, photo_id, key, new_base, data, histogram, error) -> bool:
        if new_base is not None:
            with self._tone_base_lock:
                self._tone_base = new_base
                self._tone_base_key = key
        self._live_busy = False
        current = epoch == self._full_epoch
        if (
            current
            and not error
            and data
            and serial >= self._live_applied
            and self.photo
            and int(self.photo["id"]) == photo_id
            and self.stack.get_visible_child_name() == "viewer"
        ):
            self._live_applied = serial
            self._show_preview_bytes(data, histogram, kind="live", reset_zoom=False)
        # A newer drag step arrived while this frame was rendering. Show that next.
        # A full-size pass clears this flag, so a stale frame does not restart itself.
        if self._live_again and self.photo and int(self.photo["id"]) == photo_id:
            self._tone_pending = (photo_id, self._tone_values())
            self._start_live_tone()
        return False

    def _flush_pending_tone(self, reload: bool) -> None:
        if self._tone_timer:
            GLib.source_remove(self._tone_timer)
            self._tone_timer = 0
        pending = self._tone_pending
        self._tone_pending = None
        self._live_again = False
        if not pending:
            return
        photo_id, values = pending
        self.store.set_tone(photo_id, values)
        if (
            reload
            and self.photo
            and int(self.photo["id"]) == photo_id
            and self.stack.get_visible_child_name() == "viewer"
        ):
            self._load_view()

    def _drop_pending_tone(self) -> None:
        if self._tone_timer:
            GLib.source_remove(self._tone_timer)
            self._tone_timer = 0
        self._tone_pending = None
        self._live_again = False

    def _sync_tone_sliders(self) -> None:
        if not self._tone_scales:
            return
        values = self.store.get_tone(self.photo["id"]) if self.photo else neutral_tone()
        self._tone_sync = True
        try:
            for key, scale in self._tone_scales.items():
                scale.set_value(values[key])
                self._tone_readouts[key].set_label(self._format_tone(key, values[key]))
        finally:
            self._tone_sync = False

    def _reset_tone(self) -> None:
        if not self.photo:
            return
        self._drop_pending_tone()
        self.store.set_tone(self.photo["id"], {})
        self._sync_tone_sliders()
        self._load_view()

    def _draw_histogram(self, _area, cr, width: float, height: float) -> None:
        cr.set_source_rgba(0.10, 0.10, 0.11, 1.0)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        channels = self._histogram
        if not channels or width < 2 or height < 2:
            return
        import math

        peak = 1
        for channel in channels:
            for count in channel:
                if count > peak:
                    peak = count
        log_peak = math.log1p(peak)
        colors = (
            (0.93, 0.33, 0.31, 0.70),
            (0.35, 0.78, 0.42, 0.60),
            (0.32, 0.52, 0.95, 0.60),
        )
        bottom = height - 1.0
        usable = max(1.0, height - 4.0)
        for channel, color in zip(channels, colors):
            cr.set_source_rgba(*color)
            cr.move_to(0, bottom)
            last_x = 0.0
            for index, count in enumerate(channel):
                x = (index + 0.5) / 256.0 * width
                y = bottom - (math.log1p(count) / log_peak) * usable
                cr.line_to(x, y)
                last_x = x
            cr.line_to(last_x, bottom)
            cr.close_path()
            cr.fill()

    def add_library(self, path: str) -> None:
        try:
            self.store.add_root(path)
        except NotADirectoryError:
            self._error("Not a folder", path)
            return
        self._rebuild_sidebar()

    def start_scan(self) -> None:
        self._scan_gen += 1
        gen = self._scan_gen
        roots = self.store.get_roots()
        self.title_widget.set_subtitle("Scanning…" if roots else "Add a folder to begin")

        def work() -> None:
            try:
                total = 0
                for root in roots:
                    total += self.store.scan_root(root)
                GLib.idle_add(self._scan_done, gen, total, None)
            except Exception as exc:
                traceback.print_exc()
                GLib.idle_add(self._scan_done, gen, 0, str(exc))

        threading.Thread(target=work, name="lumen-scan", daemon=True).start()

    def _scan_done(self, gen: int, total: int, error: str | None) -> bool:
        if gen != self._scan_gen:
            return False
        self._rebuild_sidebar()
        self.reload_grid()
        if error:
            self.title_widget.set_subtitle(error)
        if self.app.self_check and not self._did_self_check:
            self._did_self_check = True
            print(f"SCANNED {len(self._photos)}", flush=True)
            if self._photos:
                self._self_check_pending = True
                self.open_photo(self._photos[0]["id"])
                self._apply_rating(4)
            else:
                print("SELF_CHECK empty", flush=True)
                GLib.timeout_add(400, self._maybe_screenshot)
        elif os.environ.get("LUMEN_SCREENSHOT") and not self._screenshot_done:
            GLib.timeout_add(900, self._maybe_screenshot)
        return False

    def _rebuild_sidebar(self) -> None:
        child = self.side.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.side.remove(child)
            child = nxt
        roots = self.store.get_roots()
        folders = self.store.folders()
        root_counts = self.store.grouped_counts("root")
        folder_counts = self.store.grouped_counts("folder")
        self._side_section("Folders", "all", self.store.count_photos())
        if not roots:
            self._side_note("No folders yet")
        for root in roots:
            self._side_row(
                "root",
                Path(root).name or root,
                count=root_counts.get(root, 0),
                path=root,
                removable=True,
                indent=16,
            )
            for folder in folders:
                if folder == root:
                    continue
                try:
                    label = str(Path(folder).relative_to(root))
                except ValueError:
                    continue
                self._side_row(
                    "folder",
                    label,
                    count=folder_counts.get(folder, 0),
                    path=folder,
                    indent=28,
                )
        self._side_section("Albums", "albums", self.store.count_photos(in_any_album=True))
        self._side_row("new-album", "New album…", indent=16)
        for album in self.store.albums():
            self._side_row(
                "album",
                album["name"],
                count=int(album["n"]),
                album_id=int(album["id"]),
                removable=True,
                indent=16,
            )
        self._side_section("Filters", None, None)
        self._side_row("rating", "Starred", min_rating=1, indent=16)
        self._side_row("rating", "Rated 3+", min_rating=3, indent=16)
        self._side_row("rating", "Rated 5", min_rating=5, indent=16)
        self._select_current_row()

    def _side_section(self, text: str, kind: str | None, count: int | None) -> None:
        activatable = kind is not None
        row = Gtk.ListBoxRow(selectable=activatable, activatable=activatable)
        row.kind = kind
        row.payload = {}
        box = Gtk.Box(spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(8)
        box.set_margin_top(10)
        box.set_margin_bottom(4)
        label = Gtk.Label(label=text, xalign=0, hexpand=True)
        label.add_css_class("heading")
        if kind == "all":
            label.set_tooltip_text("Every photo in the library")
        elif kind == "albums":
            label.set_tooltip_text("Every photo that is in an album")
        box.append(label)
        if count is not None:
            count_label = Gtk.Label(label=str(count), xalign=1)
            count_label.add_css_class("dim-label")
            box.append(count_label)
        row.set_child(box)
        self.side.append(row)

    def _side_note(self, text: str) -> None:
        row = Gtk.ListBoxRow(selectable=False, activatable=False)
        row.kind = None
        row.payload = {}
        label = Gtk.Label(label=text, xalign=0, margin_start=16, margin_top=4, margin_bottom=4)
        label.add_css_class("dim-label")
        row.set_child(label)
        self.side.append(row)

    def _side_row(
        self,
        kind: str,
        text: str,
        removable: bool = False,
        count: int | None = None,
        indent: int = 16,
        **payload,
    ) -> None:
        row = Gtk.ListBoxRow()
        row.kind = kind
        row.payload = payload
        box = Gtk.Box(spacing=6)
        label = Gtk.Label(label=text, xalign=0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        label.set_margin_start(indent)
        if kind in ("root", "folder"):
            label.set_tooltip_text(payload.get("path", text))
        box.append(label)
        if count is not None:
            count_label = Gtk.Label(label=str(count), xalign=1)
            count_label.add_css_class("dim-label")
            count_label.set_margin_end(4)
            box.append(count_label)
        if removable and kind == "root":
            button = Gtk.Button(label="Remove")
            button.set_tooltip_text("Forget this folder. Files stay on disk.")
            button.connect("clicked", self._on_remove_root, payload["path"])
            box.append(button)
        elif removable and kind == "album":
            button = Gtk.Button(label="Delete")
            button.set_tooltip_text("Delete this album. Photos stay in the library.")
            button.connect("clicked", self._on_delete_album, payload["album_id"])
            box.append(button)
        row.set_child(box)
        self.side.append(row)

    def _row_matches(self, row) -> bool:
        kind = getattr(row, "kind", None)
        payload = getattr(row, "payload", {}) or {}
        filt = self._filter
        if kind == "all":
            return not filt
        if kind == "albums":
            return set(filt) == {"in_any_album"} and filt.get("in_any_album") is True
        if kind == "root":
            return set(filt) == {"root"} and filt.get("root") == payload.get("path")
        if kind == "folder":
            return set(filt) == {"folder"} and filt.get("folder") == payload.get("path")
        if kind == "album":
            return set(filt) == {"album_id"} and filt.get("album_id") == payload.get("album_id")
        if kind == "rating":
            return set(filt) == {"min_rating"} and filt.get("min_rating") == payload.get("min_rating")
        return False

    def _select_current_row(self) -> None:
        child = self.side.get_first_child()
        while child is not None:
            if self._row_matches(child):
                self.side.select_row(child)
                return
            child = child.get_next_sibling()

    def _on_side(self, _box, row) -> None:
        kind = getattr(row, "kind", None)
        payload = getattr(row, "payload", {}) or {}
        if kind == "new-album":
            self._prompt_text("New album", "Album name", self._create_album_from_sidebar)
            return
        if kind is None:
            return
        if not self._hold:
            self._clear_selection(hold_off=False)
        if kind == "all":
            self._filter = {}
        elif kind == "albums":
            self._filter = {"in_any_album": True}
        elif kind == "root":
            self._filter = {"root": payload["path"]}
        elif kind == "folder":
            self._filter = {"folder": payload["path"]}
        elif kind == "album":
            self._filter = {"album_id": payload["album_id"]}
        elif kind == "rating":
            self._filter = {"min_rating": payload["min_rating"]}
        else:
            return
        if self.stack.get_visible_child_name() != "grid":
            self._save_tags()
            self.stack.set_visible_child_name("grid")
        self.reload_grid()

    def _on_search(self, entry) -> None:
        self._search = entry.get_text()
        if self.stack.get_visible_child_name() == "viewer":
            return
        if self._search_src:
            GLib.source_remove(self._search_src)
        self._search_src = GLib.timeout_add(160, self._flush_search)

    def _flush_search(self) -> bool:
        self._search_src = 0
        if self.stack.get_visible_child_name() != "viewer":
            self.reload_grid()
        return False

    def _thumb_layout(self, size: int) -> tuple[int, int, int, int, int]:
        """Tile width, frame height, picture height, tile height, label chars.

        The image well is square. A short landscape frame was clipping the
        photo when the slider moved, instead of scaling it.
        """
        width = size
        frame_h = size
        pic_h = size
        tile_h = frame_h + 58
        chars = max(8, min(28, size // 10))
        return width, frame_h, pic_h, tile_h, chars

    def _thumb_texture(self, path: str, edge: int) -> Gdk.Texture:
        """Paintable whose longer side tracks the slider, not a fixed cache bitmap."""
        blob = scaled_thumb_bytes(path, edge)
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(blob))

    def _show_thumb(self, pic: Gtk.Picture, path: str, edge: int) -> None:
        pic.thumb_src = path
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_paintable(self._thumb_texture(path, edge))

    def _apply_thumb_size(self, size: int) -> None:
        """Resize tiles and the preview bitmaps already on the grid."""
        self._thumb_size = size
        width, frame_h, pic_h, tile_h, chars = self._thumb_layout(size)
        child = self.flow.get_first_child()
        while child is not None:
            tile = child.get_child()
            if tile is not None:
                tile.set_size_request(width, tile_h)
                frame = getattr(tile, "thumb_frame", None)
                pic = getattr(tile, "thumb_pic", None)
                name = getattr(tile, "thumb_name", None)
                if frame is not None:
                    frame.set_size_request(width, frame_h)
                if pic is not None:
                    pic.set_size_request(width, pic_h)
                    pic.set_content_fit(Gtk.ContentFit.CONTAIN)
                    src = getattr(pic, "thumb_src", None)
                    if src:
                        try:
                            pic.set_paintable(self._thumb_texture(src, size))
                        except (GLib.Error, OSError):
                            traceback.print_exc()
                if name is not None:
                    name.set_max_width_chars(chars)
                    name.set_width_chars(chars)
            child = child.get_next_sibling()
        if hasattr(self, "thumb_size_readout"):
            self.thumb_size_readout.set_text(str(size))
            self.thumb_scale.set_tooltip_text(f"Thumbnail size, {size} pixels")

    def _on_thumb_size(self, scale) -> None:
        size = int(round(scale.get_value()))
        if size == self._thumb_size:
            return
        self._apply_thumb_size(size)
        self.store.set_thumb_size(size)

    def _build_bottom_bar(self) -> Gtk.Widget:
        """Picasa-style bar: photo tray, size slider, then the actions Lumen already has."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("toolbar")
        bar.add_css_class("lumen-bottom")
        bar.set_margin_start(8)
        bar.set_margin_end(8)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)

        tray_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tray_wrap.set_hexpand(True)
        caption = Gtk.Box(spacing=8)
        title = Gtk.Label(label="Photo Tray", xalign=0)
        title.add_css_class("heading")
        self.tray_count = Gtk.Label(label="", xalign=0)
        self.tray_count.add_css_class("dim-label")
        caption.append(title)
        caption.append(self.tray_count)
        tray_wrap.append(caption)

        row = Gtk.Box(spacing=6)
        self.tray_box = Gtk.Box(spacing=4)
        self.tray_empty = Gtk.Label(label="Nothing selected", xalign=0)
        self.tray_empty.add_css_class("dim-label")
        self.tray_empty.set_margin_start(4)
        self.tray_empty.set_halign(Gtk.Align.START)
        self.tray_empty.set_valign(Gtk.Align.CENTER)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroll.set_child(self.tray_box)
        scroll.set_min_content_height(56)
        scroll.set_hexpand(True)
        self.tray_stack = Gtk.Stack()
        self.tray_stack.add_named(self.tray_empty, "empty")
        self.tray_stack.add_named(scroll, "photos")
        self.tray_stack.set_hexpand(True)
        self.tray_stack.set_visible_child_name("empty")
        row.append(self.tray_stack)

        self.hold_btn = Gtk.ToggleButton(label="Hold")
        self.hold_btn.set_tooltip_text(
            "Keep the tray while you select more. Off: the next click replaces the tray."
        )
        self.hold_btn.connect("toggled", self._on_hold_toggled)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.set_tooltip_text("Empty the photo tray")
        clear_btn.connect("clicked", lambda *_: self.select_none())
        self.add_to_btn = Gtk.MenuButton(label="Add to")
        self.add_to_btn.set_tooltip_text("Add the tray photos to an album")
        self.add_to_pop = Gtk.Popover()
        self.add_to_btn.set_popover(self.add_to_pop)
        self.add_to_pop.connect("show", self._rebuild_add_to_popover)
        actions = Gtk.Box(spacing=6)
        actions.set_valign(Gtk.Align.CENTER)
        actions.append(self.hold_btn)
        actions.append(clear_btn)
        actions.append(self.add_to_btn)
        row.append(actions)
        tray_wrap.append(row)
        bar.append(tray_wrap)

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        size_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        size_box.set_valign(Gtk.Align.CENTER)
        size_label = Gtk.Label(label="Size", xalign=0)
        size_label.add_css_class("dim-label")
        scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, THUMB_SIZE_MIN, THUMB_SIZE_MAX, 8
        )
        scale.set_digits(0)
        scale.set_draw_value(False)
        scale.set_hexpand(False)
        scale.set_size_request(160, -1)
        scale.set_value(self._thumb_size)
        scale.set_tooltip_text(f"Thumbnail size, {self._thumb_size} pixels")
        scale.connect("value-changed", self._on_thumb_size)
        readout = Gtk.Label(label=str(self._thumb_size))
        readout.add_css_class("dim-label")
        readout.set_width_chars(3)
        readout.set_xalign(1)
        size_row = Gtk.Box(spacing=6)
        size_row.append(scale)
        size_row.append(readout)
        size_box.append(size_label)
        size_box.append(size_row)
        bar.append(size_box)
        self.thumb_scale = scale
        self.thumb_size_readout = readout

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        tools = Gtk.Box(spacing=6)
        tools.set_valign(Gtk.Align.CENTER)
        print_btn = Gtk.Button(label="Print")
        print_btn.set_tooltip_text(
            "Print the photo tray. If the tray is empty, print every photo in this view."
        )
        print_btn.connect("clicked", lambda *_: self._print_current())
        book_btn = Gtk.Button(label="Photobook")
        book_btn.set_tooltip_text(
            "Photobook for the tray, or every photo in this view if the tray is empty."
        )
        book_btn.connect("clicked", lambda *_: self._open_photobook())
        export_btn = Gtk.Button(label="Export")
        export_btn.set_tooltip_text(
            "Write edited copies of the tray into a folder you pick. Originals stay untouched."
        )
        export_btn.connect("clicked", lambda *_: self._export_selection())
        tools.append(print_btn)
        tools.append(book_btn)
        tools.append(export_btn)
        tools.append(self._build_editor_menu())
        bar.append(tools)
        self._rebuild_tray()
        return bar

    def reload_grid(self) -> None:
        self._photos = self.store.query_photos(text=self._search, **self._filter)
        child = self.flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        self._pics = {}
        self._checks = {}
        self._thumb_gen += 1
        gen = self._thumb_gen
        width, frame_h, pic_h, tile_h, chars = self._thumb_layout(self._thumb_size)
        for photo in self._photos:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            tile.set_size_request(width, tile_h)
            tile.photo_id = photo["id"]
            pic = Gtk.Picture()
            pic.set_size_request(width, pic_h)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            pic.set_halign(Gtk.Align.FILL)
            pic.set_valign(Gtk.Align.FILL)
            pic.set_can_shrink(True)
            # CONTAIN scales the whole photo into the tile. COVER cropped it.
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.thumb_src = None
            frame = Gtk.Box()
            frame.add_css_class("card")
            frame.set_hexpand(True)
            frame.set_size_request(width, frame_h)
            frame.set_margin_start(4)
            frame.set_margin_end(4)
            frame.set_margin_top(4)
            overlay = Gtk.Overlay()
            overlay.set_child(pic)
            check = Gtk.CheckButton()
            check.set_halign(Gtk.Align.START)
            check.set_valign(Gtk.Align.START)
            check.set_margin_start(4)
            check.set_margin_top(4)
            check.set_tooltip_text(
                "Photo tray. If the tray is empty, Print, Photobook, and Export use this view."
            )
            check.set_active(photo["id"] in self._checked)
            check.connect("toggled", self._on_photo_checked, photo["id"])
            overlay.add_overlay(check)
            self._checks[photo["id"]] = check
            frame.append(overlay)
            name = Gtk.Label(label=photo["filename"], xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_max_width_chars(chars)
            name.set_width_chars(chars)
            name.set_margin_start(6)
            name.set_margin_end(6)
            name.set_tooltip_text(photo["path"])
            rating = Gtk.Label(label=_stars(photo["rating"]), xalign=0)
            rating.add_css_class("dim-label")
            rating.set_margin_start(6)
            rating.set_margin_bottom(4)
            tile.thumb_frame = frame
            tile.thumb_pic = pic
            tile.thumb_name = name
            tile.append(frame)
            tile.append(name)
            tile.append(rating)
            holder = Gtk.FlowBoxChild()
            holder.photo_id = photo["id"]
            holder.set_child(tile)
            self.flow.append(holder)
            self._pics[photo["id"]] = pic
        count = len(self._photos)
        noun = "photo" if count == 1 else "photos"
        if self.stack.get_visible_child_name() != "viewer":
            self.title_widget.set_subtitle(f"{count} {noun}")
        self.empty_label.set_visible(count == 0)
        if count == 0:
            if not self.store.get_roots():
                self.empty_label.set_text("No photos yet.\nUse Import or Add folder, or start with --library DIR.")
            elif self._search.strip():
                self.empty_label.set_text("No photos match this search.")
            else:
                self.empty_label.set_text("No photos in this view.")
        photos = list(self._photos)
        threading.Thread(
            target=self._thumb_worker, args=(gen, photos), name="lumen-thumbs", daemon=True
        ).start()

    def _thumb_worker(self, gen: int, photos: list[dict]) -> None:
        for photo in photos:
            if gen != self._thumb_gen:
                return
            if is_raw_path(photo["path"]):
                continue
            try:
                ops = self.store.get_edits(photo["id"])
                dest = ensure_thumb(self.store.paths.thumbs, photo["path"], photo["mtime_ns"], ops)
            except Exception:
                traceback.print_exc()
                continue
            GLib.idle_add(self._set_thumb, gen, photo["id"], str(dest))

    def _set_thumb(self, gen: int, photo_id: int, path: str) -> bool:
        if gen != self._thumb_gen:
            return False
        pic = self._pics.get(photo_id)
        if pic is None:
            return False
        try:
            self._show_thumb(pic, path, self._thumb_size)
        except (GLib.Error, OSError):
            traceback.print_exc()
        return False

    def _on_thumb_activated(self, _flow, child) -> None:
        photo_id = getattr(child, "photo_id", None)
        if photo_id is None and child.get_child() is not None:
            photo_id = getattr(child.get_child(), "photo_id", None)
        if photo_id is not None:
            self.open_photo(int(photo_id))

    def open_photo(self, photo_id: int) -> None:
        ids = [p["id"] for p in self._photos]
        if photo_id not in ids:
            fresh = self.store.get_photo(photo_id)
            if fresh is None:
                return
            self._photos = [fresh]
            ids = [photo_id]
        self._index = ids.index(photo_id)
        self._show_current()

    def _show_current(self) -> None:
        if not self._photos:
            return
        self._index = max(0, min(self._index, len(self._photos) - 1))
        photo = self.store.get_photo(self._photos[self._index]["id"])
        if photo is None:
            return
        self.photo = photo
        self._photos[self._index] = photo
        self.stack.set_visible_child_name("viewer")
        self._update_viewer_chrome()
        self._load_view()

    def _update_viewer_chrome(self) -> None:
        if not self.photo:
            return
        rating = int(self.photo["rating"])
        for index, button in enumerate(self.star_buttons, start=1):
            button.set_label("★" if index <= rating else "☆")
        self.tags_entry.set_text(", ".join(self.store.get_tags(self.photo["id"])))
        self.prev_btn.set_sensitive(self._index > 0)
        self.next_btn.set_sensitive(self._index < len(self._photos) - 1)
        self.title_widget.set_subtitle(f"{self.photo['filename']}  ·  {_stars(rating)}")
        self._sync_tone_sliders()

    def _load_view(self) -> None:
        if not self.photo:
            return
        # A newer full render invalidates a live frame still in flight.
        self._live_again = False
        self._full_epoch += 1
        epoch = self._full_epoch
        self._view_gen += 1
        gen = self._view_gen
        photo_id = self.photo["id"]
        path = self.photo["path"]
        if is_raw_path(path):
            self.title_widget.set_subtitle("Raw files stay in the library, but Lumen does not decode them.")
            self._finish_self_check()
            return

        def work() -> None:
            try:
                ops = self.store.get_edits(photo_id)
                sig = edit_sig(ops)
                image = flatten_rgb(render(path, ops))
                # Histogram matches this preview, not a second full-size pass.
                histogram = rgb_histogram(image)
                data = jpeg_bytes(image, quality=90)
                error = None
            except Exception as exc:
                traceback.print_exc()
                sig = None
                data = None
                histogram = None
                error = str(exc)
            GLib.idle_add(self._view_ready, gen, epoch, photo_id, sig, data, histogram, error)

        threading.Thread(target=work, name="lumen-view", daemon=True).start()

    def _show_preview_bytes(self, data, histogram, *, kind: str, reset_zoom: bool) -> None:
        self._histogram = histogram
        if getattr(self, "histogram", None) is not None:
            self.histogram.queue_draw()
        gbytes = GLib.Bytes.new(data)
        self._texture_bytes = gbytes
        self._texture = Gdk.Texture.new_from_bytes(gbytes)
        self._preview_kind = kind
        if reset_zoom:
            self._fit = True
            self._zoom = 1.0
        self._apply_picture_mode()

    def _view_ready(self, gen: int, epoch: int, photo_id: int, sig, data, histogram, error: str | None) -> bool:
        if gen != self._view_gen or epoch != self._full_epoch:
            return False
        # Sliders moved again while this full-size pass ran. Keep the live frame.
        if (
            sig is not None
            and self.photo
            and int(self.photo["id"]) == photo_id
            and edit_sig(self.store.get_edits(photo_id)) != sig
        ):
            self._live_applied = self._live_serial
            if not self._tone_timer and not self._live_busy:
                self._load_view()
            return False
        self._live_applied = self._live_serial
        if error or not data:
            self._histogram = histogram
            if getattr(self, "histogram", None) is not None:
                self.histogram.queue_draw()
            self.title_widget.set_subtitle(error or "Could not open this photo")
            self._finish_self_check()
            return False
        self._show_preview_bytes(data, histogram, kind="full", reset_zoom=True)
        self._finish_self_check()
        return False

    def _apply_picture_mode(self) -> None:
        tex = self._texture
        if tex is None:
            return
        self.picture.set_paintable(tex)
        if self._fit:
            self._place_picture(zoomed=False)
            self.picture.set_size_request(-1, -1)
            self.picture.set_hexpand(True)
            self.picture.set_vexpand(True)
            self.picture.set_halign(Gtk.Align.CENTER)
            self.picture.set_valign(Gtk.Align.CENTER)
            self.picture.set_can_shrink(True)
            self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            return
        self._place_picture(zoomed=True)
        width = max(1, int(tex.get_width() * self._zoom))
        height = max(1, int(tex.get_height() * self._zoom))
        self.picture.set_hexpand(False)
        self.picture.set_vexpand(False)
        self.picture.set_halign(Gtk.Align.CENTER)
        self.picture.set_valign(Gtk.Align.CENTER)
        self.picture.set_can_shrink(False)
        self.picture.set_content_fit(Gtk.ContentFit.FILL)
        self.picture.set_size_request(width, height)

    def _place_picture(self, zoomed: bool) -> None:
        parent = self.picture.get_parent()
        if zoomed:
            if parent is self.image_bin:
                self.image_bin.remove(self.picture)
            if self.scrolled.get_parent() is None:
                self.image_bin.append(self.scrolled)
            if self.scrolled.get_child() is not self.picture:
                self.scrolled.set_child(self.picture)
            return
        if parent is self.scrolled:
            self.scrolled.set_child(None)
        if self.scrolled.get_parent() is self.image_bin:
            self.image_bin.remove(self.scrolled)
        if self.picture.get_parent() is None:
            self.image_bin.append(self.picture)

    def _fit_scale(self) -> float:
        tex = self._texture
        if tex is None:
            return 1.0
        host = self.image_bin if self._fit else self.scrolled
        width = host.get_width() or self.image_bin.get_width() or 1
        height = host.get_height() or self.image_bin.get_height() or 1
        return min(width / max(tex.get_width(), 1), height / max(tex.get_height(), 1))

    def zoom_fit(self) -> None:
        self._fit = True
        self._zoom = 1.0
        self._apply_picture_mode()

    def zoom_in(self) -> None:
        if self._texture is None:
            return
        base = self._fit_scale() if self._fit else self._zoom
        self._fit = False
        self._zoom = min(max(base, 0.05) * 1.25, 8.0)
        self._apply_picture_mode()

    def zoom_out(self) -> None:
        if self._texture is None or self._fit:
            return
        self._zoom = self._zoom / 1.25
        if self._zoom <= self._fit_scale() * 1.05:
            self._fit = True
        self._apply_picture_mode()

    def _finish_self_check(self) -> None:
        if not self._self_check_pending:
            return
        self._self_check_pending = False
        photo = self.photo or {}
        print(
            f"SELF_CHECK id={photo.get('id')} rating={photo.get('rating')} file={photo.get('filename')}",
            flush=True,
        )
        if os.environ.get("LUMEN_SCREENSHOT"):
            if os.environ.get("LUMEN_SCREENSHOT_VIEWER"):
                GLib.timeout_add(700, self._maybe_screenshot)
            else:
                GLib.timeout_add(400, self._self_check_show_grid)

    def _self_check_show_grid(self) -> bool:
        self.show_grid()
        GLib.timeout_add(1200, self._maybe_screenshot)
        return False

    def show_prev(self) -> None:
        if self._index <= 0:
            return
        self._flush_pending_tone(reload=False)
        self._save_tags()
        self._index -= 1
        self._show_current()

    def show_next(self) -> None:
        if self._index >= len(self._photos) - 1:
            return
        self._flush_pending_tone(reload=False)
        self._save_tags()
        self._index += 1
        self._show_current()

    def show_grid(self) -> None:
        self._flush_pending_tone(reload=False)
        self._save_tags()
        self.stack.set_visible_child_name("grid")
        self.reload_grid()

    def _save_tags(self) -> None:
        if not self.photo:
            return
        if self.stack.get_visible_child_name() != "viewer" and not self.tags_entry.get_text():
            return
        try:
            self.store.set_tags(self.photo["id"], self.tags_entry.get_text())
        except Exception:
            traceback.print_exc()

    def _on_star(self, _button, value: int) -> None:
        self._apply_rating(value)

    def _apply_rating(self, value: int) -> None:
        if not self.photo:
            return
        self.store.set_rating(self.photo["id"], value)
        self.photo["rating"] = value
        if 0 <= self._index < len(self._photos):
            self._photos[self._index]["rating"] = value
        self._update_viewer_chrome()

    def _edit_rotate(self, direction: str) -> None:
        if not self.photo:
            return
        self.store.add_edit(self.photo["id"], "rotate", {"direction": direction})
        self._load_view()

    def _edit_enhance(self) -> None:
        if not self.photo:
            return
        self.store.add_edit(self.photo["id"], "enhance", {})
        self._load_view()

    def _edit_revert(self) -> None:
        if not self.photo:
            return
        self._drop_pending_tone()
        self.store.clear_edits(self.photo["id"])
        self._sync_tone_sliders()
        self._load_view()

    def _open_crop(self) -> None:
        if not self.photo:
            return
        try:
            size = current_size(self.photo["path"], self.store.get_edits(self.photo["id"]))
        except Exception as exc:
            self._error("Could not read this photo", str(exc))
            return
        CropDialog(self, size, self._apply_crop).present()

    def _apply_crop(self, x: int, y: int, w: int, h: int) -> None:
        if not self.photo:
            return
        self.store.add_edit(self.photo["id"], "crop", {"x": x, "y": y, "w": w, "h": h})
        self._load_view()

    def _export(self) -> None:
        if not self.photo:
            return
        stem = Path(self.photo["filename"]).stem
        dialog = Gtk.FileDialog(title="Export a copy")
        dialog.set_initial_name(f"{stem}-lumen.jpg")
        dialog.save(self, None, self._on_export_done)

    def _on_export_done(self, dialog, result) -> None:
        try:
            target = dialog.save_finish(result)
        except GLib.Error:
            return
        if target is None or not target.get_path():
            self._error("Export failed", "Pick a local file path.")
            return
        if not self.photo:
            return
        try:
            export_copy(self.photo["path"], self.store.get_edits(self.photo["id"]), Path(target.get_path()))
        except Exception as exc:
            self._error("Export failed", str(exc))
            return
        self.title_widget.set_subtitle(f"Exported a copy to {target.get_path()}")

    def _rebuild_album_popover(self, pop) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        if not self.photo:
            box.append(Gtk.Label(label="Open a photo first"))
            pop.set_child(box)
            return
        member = self.store.album_ids_for_photo(self.photo["id"])
        albums = self.store.albums()
        if not albums:
            hint = Gtk.Label(label="No albums yet", xalign=0)
            hint.add_css_class("dim-label")
            box.append(hint)
        for album in albums:
            check = Gtk.CheckButton(label=album["name"])
            check.set_active(int(album["id"]) in member)
            check.connect("toggled", self._on_album_toggled, int(album["id"]))
            box.append(check)
        entry = Gtk.Entry(placeholder_text="New album name")
        entry.connect("activate", self._on_album_entry)
        box.append(entry)
        pop.set_child(box)

    def _on_album_toggled(self, check, album_id: int) -> None:
        if not self.photo:
            return
        self.store.set_membership(self.photo["id"], album_id, check.get_active())
        self._rebuild_sidebar()

    def _on_album_entry(self, entry) -> None:
        name = entry.get_text().strip()
        if not name or not self.photo:
            return
        album_id = self.store.create_album(name)
        self.store.set_membership(self.photo["id"], album_id, True)
        self._rebuild_sidebar()
        self._rebuild_album_popover(self.album_pop)

    def _create_album_from_sidebar(self, name: str) -> None:
        self.store.create_album(name)
        self._rebuild_sidebar()

    def _prompt_text(self, title: str, placeholder: str, callback) -> None:
        win = Gtk.Window(transient_for=self, modal=True, title=title)
        win.set_destroy_with_parent(True)
        win.set_default_size(360, 120)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        entry = Gtk.Entry(placeholder_text=placeholder)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: win.close())
        ok = Gtk.Button(label="Create")
        ok.add_css_class("suggested-action")

        def submit(*_args) -> None:
            text = entry.get_text().strip()
            win.close()
            if text:
                callback(text)

        ok.connect("clicked", submit)
        entry.connect("activate", submit)
        buttons.append(cancel)
        buttons.append(ok)
        box.append(entry)
        box.append(buttons)
        win.set_child(box)
        win.present()
        entry.grab_focus()


    def _build_theme_menu(self, header) -> None:
        button = Gtk.MenuButton(label="Theme")
        button.set_tooltip_text("Light, dark, or follow the system color scheme")
        self._theme_button = button
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        self._theme_checks: dict[str, Gtk.CheckButton] = {}
        self._theme_sync = False
        group = None
        for key, label in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            check = Gtk.CheckButton(label=label)
            if group is None:
                group = check
            else:
                check.set_group(group)
            check.connect("toggled", self._on_theme_toggled, key)
            self._theme_checks[key] = check
            box.append(check)
        pop.set_child(box)
        pop.connect("show", lambda *_: self._sync_theme_checks())
        button.set_popover(pop)
        header.pack_start(button)
        self._sync_theme_checks()

    def _sync_theme_checks(self) -> None:
        theme = self.store.get_theme()
        self._theme_sync = True
        try:
            for key, check in self._theme_checks.items():
                check.set_active(key == theme)
        finally:
            self._theme_sync = False
        tips = {
            "system": "Theme: follow system",
            "light": "Theme: light",
            "dark": "Theme: dark",
        }
        self._theme_button.set_tooltip_text(tips.get(theme, "Theme"))

    def _on_theme_toggled(self, check, key: str) -> None:
        if self._theme_sync or not check.get_active():
            return
        self.store.set_theme(key)
        apply_color_scheme(key)
        self._sync_theme_checks()

    def _build_editor_menu(self) -> Gtk.Widget:
        button = Gtk.MenuButton(label="Edit in")
        button.set_tooltip_text("Open the tray in Darktable or GIMP. If the tray is empty, use this view. Originals stay untouched.")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        self._editor_buttons: dict[str, Gtk.Button] = {}
        for command, title, tip in EDITORS:
            item = Gtk.Button(label=f"Open in {title}")
            item.set_tooltip_text(tip)
            item.connect(
                "clicked",
                lambda *_, command=command, title=title: (pop.popdown(), self._open_in_editor(command, title)),
            )
            box.append(item)
            self._editor_buttons[command] = item
        self._editor_note = Gtk.Label(xalign=0)
        self._editor_note.set_wrap(True)
        self._editor_note.set_max_width_chars(32)
        self._editor_note.add_css_class("dim-label")
        self._editor_note.set_visible(False)
        box.append(self._editor_note)
        pop.set_child(box)
        pop.connect("show", lambda *_: self._refresh_editors())
        button.set_popover(pop)
        self._refresh_editors()
        return button

    def _refresh_editors(self) -> None:
        missing: list[str] = []
        for command, title, _tip in EDITORS:
            found = resolve_editor(command) is not None
            self._editor_buttons[command].set_sensitive(found)
            if not found:
                missing.append(title)
        if not missing:
            self._editor_note.set_visible(False)
            return
        if len(missing) == 1:
            text = f"{missing[0]} was not found."
        else:
            text = f"{missing[0]} and {missing[1]} were not found."
        self._editor_note.set_text(text)
        self._editor_note.set_visible(True)

    def _photos_for_editor(self) -> list[dict]:
        tray = self._tray_photos()
        if tray:
            return tray
        if self.stack.get_visible_child_name() == "viewer" and self.photo:
            return [self.photo]
        return list(self._photos)

    def _open_in_editor(self, command: str, title: str) -> None:
        executable = resolve_editor(command)
        if executable is None:
            self._error(f"{title} was not found", f"The {command} program was not found.")
            self._refresh_editors()
            return
        photos = self._photos_for_editor()
        if not photos:
            self._error(
                "Nothing selected",
                "The photo tray is empty, and this view has no photos.",
            )
            return
        export_dir = self.store.paths.edit_export
        snapshot = [(int(photo["id"]), photo["path"]) for photo in photos]

        def work() -> None:
            try:
                paths = []
                for photo_id, path in snapshot:
                    ops = self.store.get_edits(photo_id)
                    paths.append(str(prepare_editor_file(path, ops, export_dir)))
                GLib.idle_add(self._spawn_editor, executable, title, paths, None)
            except Exception as exc:
                traceback.print_exc()
                GLib.idle_add(self._spawn_editor, executable, title, [], str(exc))

        threading.Thread(target=work, name="lumen-edit-export", daemon=True).start()

    def _spawn_editor(self, executable: str, title: str, paths: list[str], error: str | None) -> bool:
        if error:
            self._error(f"Could not prepare files for {title}", error)
            return False
        if not paths:
            self._error(
                "Nothing selected",
                "The photo tray is empty, and this view has no photos.",
            )
            return False
        try:
            # Absolute path so a desktop session with a short PATH still starts
            # the program (native binary or Flatpak export wrapper).
            GLib.spawn_async([executable, *paths], flags=GLib.SpawnFlags.SEARCH_PATH)
        except GLib.Error as exc:
            self._error(f"Could not start {title}", str(exc))
            return False
        noun = "photo" if len(paths) == 1 else "photos"
        self.title_widget.set_subtitle(f"Opened {len(paths)} {noun} in {title}")
        return False

    def _sync_checks(self) -> None:
        self._updating_checks = True
        try:
            for photo_id, check in self._checks.items():
                check.set_active(photo_id in self._checked)
        finally:
            self._updating_checks = False

    def select_all_visible(self) -> None:
        ids = [int(photo["id"]) for photo in self._photos]
        if self._hold:
            self._add_selection(ids)
            if self._anchor is None and ids:
                self._anchor = ids[0]
        else:
            self._replace_selection(ids)
            if ids:
                self._anchor = ids[0]

    def select_none(self) -> None:
        self._clear_selection(hold_off=True)

    def _on_hold_toggled(self, button) -> None:
        if self._tray_sync:
            return
        self._hold = bool(button.get_active())

    def _clear_selection(self, *, hold_off: bool) -> None:
        self._checked.clear()
        self._tray_order.clear()
        self._anchor = None
        if hold_off:
            self._hold = False
            if getattr(self, "hold_btn", None) is not None:
                self._tray_sync = True
                self.hold_btn.set_active(False)
                self._tray_sync = False
        self._sync_checks()
        self._rebuild_tray()

    def _remember(self, photo_id: int) -> None:
        if photo_id not in self._checked:
            self._checked.add(photo_id)
            self._tray_order.append(photo_id)

    def _forget(self, photo_id: int) -> None:
        self._checked.discard(photo_id)
        self._tray_order = [pid for pid in self._tray_order if pid != photo_id]

    def _replace_selection(self, ids: list[int]) -> None:
        ordered: list[int] = []
        seen: set[int] = set()
        for photo_id in ids:
            if photo_id in seen:
                continue
            seen.add(photo_id)
            ordered.append(photo_id)
        self._checked = set(ordered)
        self._tray_order = ordered
        self._sync_checks()
        self._rebuild_tray()

    def _add_selection(self, ids: list[int]) -> None:
        for photo_id in ids:
            self._remember(photo_id)
        self._sync_checks()
        self._rebuild_tray()

    def _apply_click(self, photo_id: int, *, shift: bool, ctrl: bool) -> None:
        """Hold off: a click replaces the tray. Hold on: a click adds to it."""
        ids = [int(photo["id"]) for photo in self._photos]
        if ctrl and not shift:
            if photo_id in self._checked:
                self._forget(photo_id)
            else:
                self._remember(photo_id)
            self._anchor = photo_id
            self._sync_checks()
            self._rebuild_tray()
            return
        if shift and self._anchor is not None and self._anchor in ids and photo_id in ids:
            lo = ids.index(self._anchor)
            hi = ids.index(photo_id)
            if lo > hi:
                lo, hi = hi, lo
            rng = ids[lo : hi + 1]
            if self._hold:
                self._add_selection(rng)
            else:
                self._replace_selection(rng)
            return
        if self._hold:
            self._remember(photo_id)
            self._anchor = photo_id
            self._sync_checks()
            self._rebuild_tray()
            return
        self._replace_selection([photo_id])
        self._anchor = photo_id

    def _grid_focused(self) -> bool:
        if self.stack.get_visible_child_name() != "grid":
            return False
        widget = self.get_focus()
        while widget is not None:
            if widget is self.flow:
                return True
            widget = widget.get_parent()
        return False

    def _handle_grid_key(self, keyval, state) -> bool:
        if not self._grid_focused():
            return False
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and keyval in (Gdk.KEY_a, Gdk.KEY_A):
            if shift:
                self.select_none()
            else:
                self.select_all_visible()
            return True
        if keyval == Gdk.KEY_Escape and not ctrl and not shift:
            self.select_none()
            return True
        return False

    def _widget_is_check(self, widget) -> bool:
        while widget is not None and widget is not self.flow:
            if isinstance(widget, Gtk.CheckButton):
                return True
            widget = widget.get_parent()
        return False

    def _photo_under(self, x: float, y: float) -> int | None:
        widget = self.flow.pick(x, y, Gtk.PickFlags.DEFAULT)
        saw_check = False
        photo_id = None
        while widget is not None and widget is not self.flow:
            if isinstance(widget, Gtk.CheckButton):
                saw_check = True
            pid = getattr(widget, "photo_id", None)
            if pid is not None and photo_id is None:
                photo_id = int(pid)
            widget = widget.get_parent()
        if saw_check:
            return None
        return photo_id

    def _norm_rect(self, x0: float, y0: float, x1: float, y1: float):
        return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)

    def _bounds_hit(self, rect, x: float, y: float, w: float, h: float) -> bool:
        rx, ry = rect.get_x(), rect.get_y()
        rw, rh = rect.get_width(), rect.get_height()
        return rx < x + w and rx + rw > x and ry < y + h and ry + rh > y

    def _ids_in_rect(self, x: float, y: float, w: float, h: float) -> list[int]:
        found: list[int] = []
        if w < 1 and h < 1:
            return found
        child = self.flow.get_first_child()
        while child is not None:
            ok, rect = child.compute_bounds(self.flow)
            pid = getattr(child, "photo_id", None)
            if ok and pid is not None and self._bounds_hit(rect, x, y, w, h):
                found.append(int(pid))
            child = child.get_next_sibling()
        return found

    def _apply_rubber(self, x: float, y: float, w: float, h: float) -> None:
        hits = self._ids_in_rect(x, y, w, h)
        merged: list[int] = []
        seen: set[int] = set()
        for photo_id in list(self._rubber_base) + hits:
            if photo_id in seen:
                continue
            seen.add(photo_id)
            merged.append(photo_id)
        self._checked = set(merged)
        self._tray_order = merged
        self._sync_checks()

    def _draw_band(self, _area, cr, width, height) -> None:
        cr.set_source_rgba(0.21, 0.52, 0.89, 0.28)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        cr.set_source_rgba(0.11, 0.40, 0.78, 0.95)
        cr.set_line_width(1)
        cr.rectangle(0.5, 0.5, max(width - 1, 0), max(height - 1, 0))
        cr.stroke()

    def _place_band(self, x: float, y: float, w: float, h: float) -> None:
        start = Graphene.Point().init(x, y)
        end = Graphene.Point().init(x + w, y + h)
        ok1, p1 = self.flow.compute_point(self.grid_overlay, start)
        ok2, p2 = self.flow.compute_point(self.grid_overlay, end)
        if not ok1 or not ok2:
            self.band.set_visible(False)
            return
        left, top = int(p1.x), int(p1.y)
        right, bottom = int(p2.x), int(p2.y)
        host_w = self.grid_overlay.get_width()
        host_h = self.grid_overlay.get_height()
        clip_l, clip_t = max(0, left), max(0, top)
        clip_r = min(host_w, right) if host_w else right
        clip_b = min(host_h, bottom) if host_h else bottom
        if clip_r - clip_l < 1 or clip_b - clip_t < 1:
            self.band.set_visible(False)
            return
        self.band.set_margin_start(clip_l)
        self.band.set_margin_top(clip_t)
        self.band.set_size_request(clip_r - clip_l, clip_b - clip_t)
        self.band.set_visible(True)
        self.band.queue_draw()

    def _modifier_state(self, gesture) -> int:
        event = gesture.get_current_event()
        if event is None:
            return 0
        return int(event.get_modifier_state())

    def _on_rubber_begin(self, gesture, x, y) -> None:
        self._press_xy = (x, y)
        self._press_hadj, self._press_vadj = self._scroll_adjustments()
        self._rubber_on = False
        self._ignore_drag = False
        self._set_scroll_focus(False)
        picked = self.flow.pick(x, y, Gtk.PickFlags.DEFAULT)
        if self._widget_is_check(picked):
            self._ignore_drag = True
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            GLib.idle_add(self._restore_scroll_focus)
            return
        # Focusing the FlowBox itself focuses its first child and scrolls the
        # lightbox there. A one-pixel jitter is then a drag the full height of
        # that scroll, which selects the whole column. Focus the photo under
        # the pointer instead, and never the first child.
        child = self._flow_child_at(x, y)
        if child is not None:
            child.grab_focus()

    def _scroll_adjustments(self):
        parent = self.flow.get_parent()
        if parent is None:
            return 0.0, 0.0
        return parent.get_hadjustment().get_value(), parent.get_vadjustment().get_value()

    def _set_scroll_focus(self, enabled: bool) -> None:
        """Keep a click from scrolling the lightbox to whichever child takes focus."""
        parent = self.flow.get_parent()
        if parent is None or not hasattr(parent, "set_scroll_to_focus"):
            return
        parent.set_scroll_to_focus(enabled)
        self._suspend_scroll_focus = not enabled

    def _restore_scroll_focus(self) -> bool:
        if self._suspend_scroll_focus:
            self._set_scroll_focus(True)
        return False

    def _pointer_delta(self, offset_x: float, offset_y: float) -> tuple[float, float]:
        """Gesture offsets are in flow coordinates, so a focus scroll looks like a drag.

        Subtract the adjustment change since press. What remains is how far the
        pointer actually moved. A click that only scrolls must not become a selection.
        """
        hadj, vadj = self._scroll_adjustments()
        return offset_x - (hadj - self._press_hadj), offset_y - (vadj - self._press_vadj)

    def _flow_child_at(self, x: float, y: float):
        widget = self.flow.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self.flow:
            if isinstance(widget, Gtk.FlowBoxChild):
                return widget
            widget = widget.get_parent()
        return None

    def _on_rubber_update(self, gesture, offset_x, offset_y) -> None:
        if self._ignore_drag:
            return
        pointer_dx, pointer_dy = self._pointer_delta(offset_x, offset_y)
        if not self._rubber_on and (pointer_dx * pointer_dx + pointer_dy * pointer_dy) < 64:
            return
        if not self._rubber_on:
            self._rubber_on = True
            self._rubber_base = list(self._tray_order) if self._hold else []
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        x0, y0 = self._press_xy
        x, y, w, h = self._norm_rect(x0, y0, x0 + pointer_dx, y0 + pointer_dy)
        self._place_band(x, y, w, h)
        self._apply_rubber(x, y, w, h)

    def _on_rubber_end(self, gesture, _offset_x, _offset_y) -> None:
        # FlowBox focuses the clicked child on release. Restore scroll-to-focus
        # on the next idle so that focus cannot move the lightbox during this click.
        GLib.idle_add(self._restore_scroll_focus)
        if self._ignore_drag:
            self._ignore_drag = False
            self.band.set_visible(False)
            return
        if self._rubber_on:
            self._rubber_on = False
            self.band.set_visible(False)
            self._rebuild_tray()
            return
        photo_id = self._photo_under(*self._press_xy)
        if photo_id is None:
            return
        state = self._modifier_state(gesture)
        shift = bool(state & int(Gdk.ModifierType.SHIFT_MASK))
        ctrl = bool(state & int(Gdk.ModifierType.CONTROL_MASK))
        self._apply_click(photo_id, shift=shift, ctrl=ctrl)

    def _on_photo_checked(self, check, photo_id: int) -> None:
        if self._updating_checks:
            return
        if check.get_active():
            self._remember(photo_id)
        else:
            self._forget(photo_id)
        self._anchor = photo_id
        self._rebuild_tray()

    def _tray_photos(self) -> list[dict]:
        photos: list[dict] = []
        seen: set[int] = set()
        for photo_id in list(self._tray_order):
            if photo_id in seen:
                continue
            row = self.store.get_photo(int(photo_id))
            if row is None:
                continue
            seen.add(int(photo_id))
            photos.append(row)
        for photo_id in self._checked:
            if photo_id in seen:
                continue
            row = self.store.get_photo(int(photo_id))
            if row is not None:
                photos.append(row)
        return photos

    def _book_photos(self) -> tuple[list[dict], bool]:
        tray = self._tray_photos()
        if tray:
            return tray, True
        return list(self._photos), False

    def _empty_print_message(self) -> str:
        if self._checked:
            return "The photo tray has no photos that can be opened."
        return "The photo tray is empty, and this view has no photos."

    def _rebuild_tray(self) -> None:
        if not hasattr(self, "tray_box"):
            return
        child = self.tray_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.tray_box.remove(child)
            child = nxt
        photos = self._tray_photos()
        noun = "photo" if len(photos) == 1 else "photos"
        self.tray_count.set_text(f"{len(photos)} {noun}" if photos else "")
        self.tray_stack.set_visible_child_name("photos" if photos else "empty")
        self._tray_gen += 1
        gen = self._tray_gen
        self._tray_pics = {}
        if not photos:
            return
        for photo in photos:
            pic = Gtk.Picture()
            pic.set_size_request(52, 52)
            pic.set_can_shrink(True)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            frame = Gtk.Box()
            frame.add_css_class("card")
            frame.set_size_request(56, 56)
            frame.set_tooltip_text(photo["filename"])
            frame.append(pic)
            self.tray_box.append(frame)
            self._tray_pics[int(photo["id"])] = pic
        threading.Thread(
            target=self._tray_worker, args=(gen, list(photos)), name="lumen-tray", daemon=True
        ).start()

    def _tray_worker(self, gen: int, photos: list[dict]) -> None:
        for photo in photos:
            if gen != self._tray_gen:
                return
            try:
                ops = self.store.get_edits(photo["id"])
                dest = ensure_thumb(self.store.paths.thumbs, photo["path"], photo["mtime_ns"], ops)
            except Exception:
                traceback.print_exc()
                continue
            GLib.idle_add(self._set_tray_thumb, gen, int(photo["id"]), str(dest))

    def _set_tray_thumb(self, gen: int, photo_id: int, path: str) -> bool:
        if gen != self._tray_gen:
            return False
        pic = self._tray_pics.get(photo_id)
        if pic is None:
            return False
        try:
            self._show_thumb(pic, path, 52)
        except (GLib.Error, OSError):
            traceback.print_exc()
        return False

    def _rebuild_add_to_popover(self, pop) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        photos = self._tray_photos()
        if not photos:
            note = Gtk.Label(label="Select photos first. The tray is empty.", xalign=0)
            note.set_wrap(True)
            box.append(note)
            pop.set_child(box)
            return
        albums = self.store.albums()
        if not albums:
            hint = Gtk.Label(label="No albums yet", xalign=0)
            hint.add_css_class("dim-label")
            box.append(hint)
        for album in albums:
            button = Gtk.Button(label=album["name"])
            button.connect(
                "clicked",
                lambda *_, album_id=int(album["id"]), name=album["name"]: self._add_tray_to_album(album_id, name),
            )
            box.append(button)
        entry = Gtk.Entry(placeholder_text="New album name")
        entry.connect("activate", self._on_add_to_new_album)
        box.append(entry)
        pop.set_child(box)

    def _add_tray_to_album(self, album_id: int, name: str) -> None:
        self.add_to_pop.popdown()
        photos = self._tray_photos()
        for photo in photos:
            self.store.set_membership(int(photo["id"]), album_id, True)
        self._rebuild_sidebar()
        noun = "photo" if len(photos) == 1 else "photos"
        self.title_widget.set_subtitle(f"Added {len(photos)} {noun} to {name}")

    def _on_add_to_new_album(self, entry) -> None:
        name = entry.get_text().strip()
        if not name:
            return
        album_id = self.store.create_album(name)
        self._add_tray_to_album(album_id, name)

    def _export_selection(self) -> None:
        photos, _selected = self._book_photos()
        if not photos:
            self._error("Nothing to export", self._empty_print_message())
            return
        self._pending_export = list(photos)
        dialog = Gtk.FileDialog(title="Export copies")
        dialog.select_folder(self, None, self._on_export_folder)

    def _on_export_folder(self, dialog, result) -> None:
        try:
            target = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if target is None or not target.get_path():
            self._error("Export failed", "Pick a local folder.")
            return
        folder = Path(target.get_path())
        photos = list(self._pending_export)
        if not photos:
            return

        def work() -> None:
            try:
                written = self._write_export_copies(folder, photos)
                GLib.idle_add(self._export_batch_done, written, None)
            except Exception as exc:
                traceback.print_exc()
                GLib.idle_add(self._export_batch_done, [], str(exc))

        threading.Thread(target=work, name="lumen-export", daemon=True).start()

    def _unique_export_path(self, folder: Path, src: str) -> Path:
        source = Path(src)
        ext = source.suffix if source.suffix.lower() in EXPORT_FORMATS else ".jpg"
        try:
            original = source.resolve()
        except OSError:
            original = source
        for index in range(1000):
            extra = "" if index == 0 else f"-{index + 1}"
            candidate = folder / f"{source.stem}-lumen{extra}{ext}"
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved == original or candidate.exists():
                continue
            return candidate
        raise RuntimeError(f"No free name for {source.name}")

    def _write_export_copies(self, folder: Path, photos: list[dict]) -> list[str]:
        written: list[str] = []
        for photo in photos:
            dest = self._unique_export_path(folder, photo["path"])
            export_copy(photo["path"], self.store.get_edits(int(photo["id"])), dest)
            written.append(str(dest))
        return written

    def _export_batch_done(self, written: list[str], error: str | None) -> bool:
        if error:
            self._error("Export failed", error)
            return False
        noun = "copy" if len(written) == 1 else "copies"
        self.title_widget.set_subtitle(f"Exported {len(written)} {noun}. Originals were not changed.")
        return False

    def _viewer_photo(self) -> dict | None:
        if self.stack.get_visible_child_name() == "viewer" and self.photo:
            return self.photo
        return None

    def _prepare_pages(self, photos: list[dict], single: bool):
        if not photos:
            self._error("Nothing to print", self._empty_print_message())
            return None
        items = [(p["path"], self.store.get_edits(p["id"])) for p in photos]
        try:
            pages = load_pages(items, single=single)
        except Exception as exc:
            self._error("Could not prepare photos", str(exc))
            return None
        if not pages:
            self._error("Nothing to print", self._empty_print_message())
            return None
        return pages

    def _guard_pdf_path(self, path: str, photos: list[dict]) -> str | None:
        dest = Path(path)
        if dest.suffix.lower() != ".pdf":
            dest = dest.with_name(dest.name + ".pdf")
        try:
            resolved = dest.resolve()
        except OSError:
            resolved = dest
        originals = set()
        for photo in photos:
            try:
                originals.add(Path(photo["path"]).resolve())
            except OSError:
                originals.add(Path(photo["path"]))
        if resolved in originals:
            self._error("Export failed", "Refusing to overwrite an original photo.")
            return None
        return str(dest)

    def _open_photobook(self) -> None:
        photos, selected = self._book_photos()
        win = Gtk.Window(transient_for=self, modal=True, title="Photobook")
        win.set_destroy_with_parent(True)
        win.set_default_size(440, 230)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        summary = Gtk.Label(label=describe_book(len(photos), selected=selected), wrap=True, xalign=0)
        box.append(summary)
        note = Gtk.Label(
            label="A4 portrait, 12 mm margins, white page. Photos keep their shape and are centered. An 8 mm gap separates the two photos on even pages.",
            wrap=True,
            xalign=0,
        )
        note.add_css_class("dim-label")
        box.append(note)
        if self._viewer_photo() is not None:
            hint = Gtk.Label(
                label="This booklet uses the photo tray, or the grid if the tray is empty. With a photo open and the tray empty, Print uses that photo as one page.",
                wrap=True,
                xalign=0,
            )
            hint.add_css_class("dim-label")
            box.append(hint)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: win.close())
        export = Gtk.Button(label="Export PDF")
        export.add_css_class("suggested-action")
        export.set_sensitive(bool(photos))
        export.connect("clicked", lambda *_: self._export_pdf_photos(photos, single=False))
        print_btn = Gtk.Button(label="Print")
        print_btn.set_sensitive(bool(photos))
        print_btn.connect("clicked", lambda *_: self._print_photos(photos, single=False))
        buttons.append(close)
        buttons.append(print_btn)
        buttons.append(export)
        box.append(buttons)
        win.set_child(box)
        win.present()

    def _export_pdf(self) -> None:
        if not self._tray_photos():
            photo = self._viewer_photo()
            if photo is not None:
                self._export_pdf_photos([photo], single=True)
                return
        photos, _selected = self._book_photos()
        self._export_pdf_photos(photos, single=False)

    def _export_pdf_photos(self, photos: list[dict], single: bool) -> None:
        if not photos:
            self._error("Nothing to export", self._empty_print_message())
            return
        dialog = Gtk.FileDialog(title="Export PDF")
        dialog.set_initial_name("lumen-photobook.pdf")
        filt = Gtk.FileFilter()
        filt.set_name("PDF")
        filt.add_suffix("pdf")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog.set_filters(filters)
        dialog.save(self, None, lambda d, res, p=list(photos), s=single: self._on_pdf_chosen(d, res, p, s))

    def _on_pdf_chosen(self, dialog, result, photos: list[dict], single: bool) -> None:
        try:
            target = dialog.save_finish(result)
        except GLib.Error:
            return
        if target is None or not target.get_path():
            self._error("Export failed", "Pick a local file path.")
            return
        path = self._guard_pdf_path(target.get_path(), photos)
        if path is None:
            return
        pages = self._prepare_pages(photos, single)
        if pages is None:
            return
        try:
            outcome = run_print(self, pages, path)
        except Exception as exc:
            self._error("Export failed", str(exc))
            return
        if outcome == Gtk.PrintOperationResult.APPLY:
            self.title_widget.set_subtitle(f"Exported PDF to {path}")

    def _print_current(self) -> None:
        if not self._tray_photos():
            photo = self._viewer_photo()
            if photo is not None:
                self._print_photos([photo], single=True)
                return
        photos, _selected = self._book_photos()
        self._print_photos(photos, single=False)

    def _print_photos(self, photos: list[dict], single: bool) -> None:
        pages = self._prepare_pages(photos, single)
        if pages is None:
            return
        try:
            run_print(self, pages, None)
        except Exception as exc:
            self._error("Print failed", str(exc))

    def _on_backup(self, *_args) -> None:
        from lumen.backup_dialog import BackupDialog

        dialog = BackupDialog(self, self.store)
        dialog.present()

    def _on_import(self, *_args) -> None:
        from lumen.import_dialog import ImportDialog

        dialog = ImportDialog(self, self.store, self._on_import_finished)
        dialog.present()

    def _on_import_finished(self, result, summary: str) -> None:
        if result.root:
            self._filter = {"root": result.root}
        self._rebuild_sidebar()
        if self.stack.get_visible_child_name() == "viewer":
            self.stack.set_visible_child_name("grid")
        self.reload_grid()
        self.title_widget.set_subtitle(summary)

    def _on_add_folder(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Add photo folders")
        dialog.select_multiple_folders(self, None, self._on_folders_chosen)

    def _on_folders_chosen(self, dialog, result) -> None:
        try:
            files = dialog.select_multiple_folders_finish(result)
        except GLib.Error:
            return
        if files is None:
            return
        for index in range(files.get_n_items()):
            item = files.get_item(index)
            path = item.get_path() if item is not None else None
            if path:
                self.add_library(path)
        self.start_scan()

    def _on_remove_root(self, _button, path: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message("Remove this folder from Lumen?")
        dialog.set_detail("Photos stay on disk. Lumen only forgets the folder and its library entries.")
        dialog.set_buttons(["Cancel", "Remove"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, lambda d, res, p=path: self._remove_root_done(d, res, p))

    def _remove_root_done(self, dialog, result, path: str) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return
        self.store.remove_root(path)
        root = str(Path(path).resolve())
        folder = self._filter.get("folder") or ""
        if self._filter.get("root") == root or folder == root or folder.startswith(root + os.sep):
            self._filter = {}
        self._rebuild_sidebar()
        if self.stack.get_visible_child_name() == "viewer":
            self.stack.set_visible_child_name("grid")
        self.reload_grid()

    def _on_delete_album(self, _button, album_id: int) -> None:
        self.store.delete_album(album_id)
        if self._filter.get("album_id") == album_id:
            self._filter = {}
        self._rebuild_sidebar()
        if self.stack.get_visible_child_name() != "viewer":
            self.reload_grid()

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        if self._handle_grid_key(keyval, state):
            return True
        if self.stack.get_visible_child_name() != "viewer":
            return False
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Entry, Gtk.SearchEntry, Gtk.SpinButton, Gtk.Text, Gtk.Range)):
            return False
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self.show_prev()
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self.show_next()
            return True
        if keyval == Gdk.KEY_Escape:
            self.show_grid()
            return True
        if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.zoom_in()
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.zoom_out()
            return True
        if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self.zoom_fit()
            return True
        rating_keys = {
            Gdk.KEY_1: 1,
            Gdk.KEY_2: 2,
            Gdk.KEY_3: 3,
            Gdk.KEY_4: 4,
            Gdk.KEY_5: 5,
        }
        if keyval in rating_keys:
            self._apply_rating(rating_keys[keyval])
            return True
        return False

    def _maybe_screenshot(self) -> bool:
        path = os.environ.get("LUMEN_SCREENSHOT", "").strip()
        if not path or self._screenshot_done:
            return False
        self._shot_tries += 1
        if self.get_width() < 50 or self.get_height() < 50:
            if self._shot_tries > 15:
                print("SCREENSHOT_FAIL window not mapped", flush=True)
                return False
            return True
        try:
            self._save_png(path)
        except Exception:
            traceback.print_exc()
            if self._shot_tries > 15:
                print("SCREENSHOT_FAIL", flush=True)
                return False
            return True
        self._screenshot_done = True
        print(f"SCREENSHOT {path}", flush=True)
        if os.environ.get("LUMEN_SCREENSHOT_QUIT"):
            GLib.timeout_add(150, self._quit_after_shot)
        return False

    def _quit_after_shot(self) -> bool:
        app = self.get_application()
        if app is not None:
            app.quit()
        return False

    def _save_png(self, path: str) -> None:
        width, height = self.get_width(), self.get_height()
        paintable = Gtk.WidgetPaintable.new(self)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, width, height)
        node = snap.to_node()
        if node is None:
            raise RuntimeError("empty window snapshot")
        renderer = self.get_native().get_renderer()
        if renderer is None:
            raise RuntimeError("no GSK renderer")
        texture = renderer.render_texture(node, None)
        parent = Path(path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        if not texture.save_to_png(path):
            raise RuntimeError("save_to_png returned false")

    def _error(self, message: str, detail: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message(message)
        dialog.set_detail(detail)
        dialog.set_buttons(["OK"])
        dialog.choose(self, None, _finish_alert)


def _finish_alert(dialog, result) -> None:
    try:
        dialog.choose_finish(result)
    except GLib.Error:
        pass


class LumenApp(Adw.Application):
    def __init__(self, libraries: list[str], self_check: bool) -> None:
        super().__init__(application_id="org.lumen.Lumen", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.libraries = list(libraries)
        self.self_check = self_check
        self.window: MainWindow | None = None
        self.connect("activate", self._activate)

    def _activate(self, _app) -> None:
        if self.window is None:
            self.window = MainWindow(self)
            for folder in self.libraries:
                self.window.add_library(folder)
            self.window.present()
            self.window.start_scan()
        else:
            self.window.present()
