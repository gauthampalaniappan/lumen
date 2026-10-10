"""Picasa-style import window: pick a card, camera, or folder, then copy."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from lumen.importing import (
    CAMERA_NOTE,
    ImportResult,
    ImportSource,
    ListedPhoto,
    camera_download_note,
    default_import_dir,
    detect_cameras,
    discover_volumes,
    import_camera_files,
    import_local_files,
    list_camera_photos,
    list_folder_photos,
    mount_device,
)
from lumen.store import Store

_THUMB_BYTES = 12_000_000


class ImportDialog(Adw.Window):
    def __init__(self, parent, store: Store, on_done) -> None:
        super().__init__(title="Import photos")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(920, 600)
        self.store = store
        self.on_done = on_done
        self._sources: list[ImportSource] = []
        self._source_keys: list[str] = []
        self._selected_key: str | None = None
        self._chosen: list[ImportSource] = []
        self._cameras: list[ImportSource] = []
        self._checks: list[tuple[Gtk.CheckButton, ListedPhoto]] = []
        self._list_gen = 0
        self._importing = False
        self._closed = False
        self._refresh_scheduled = False
        self._camera_busy = False
        self._camera_ticks = 0
        self._poll = 0
        self._mount_ids: list[int] = []
        self._file_monitors = []
        self.dest = default_import_dir()
        self._build()
        self._reload_sources()
        self._watch_volumes()
        self._detect_cameras_async()
        self.connect("close-request", self._on_close)

    def _build(self) -> None:
        header = Adw.HeaderBar()
        title = Adw.WindowTitle.new("Import photos", "Copy into the library. Nothing is moved or overwritten.")
        self.title = title
        header.set_title_widget(title)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.import_btn = Gtk.Button(label="Import")
        self.import_btn.add_css_class("suggested-action")
        self.import_btn.set_sensitive(False)
        self.import_btn.connect("clicked", self._on_import)
        header.pack_end(self.import_btn)

        choose = Gtk.Button(label="Choose a folder")
        choose.set_tooltip_text("Browse a folder of photos when no card is connected")
        choose.connect("clicked", self._on_choose_folder)
        choose.set_margin_start(8)
        choose.set_margin_end(8)
        choose.set_margin_top(8)

        self.source_list = Gtk.ListBox()
        self.source_list.add_css_class("navigation-sidebar")
        self.source_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.source_list.connect("row-selected", self._on_source_selected)
        source_scroll = Gtk.ScrolledWindow()
        source_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        source_scroll.set_child(self.source_list)
        source_scroll.set_vexpand(True)
        source_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        source_wrap.set_size_request(240, -1)
        source_wrap.append(choose)
        hint = Gtk.Label(label="Cards and cameras", xalign=0)
        hint.add_css_class("heading")
        hint.set_margin_start(12)
        hint.set_margin_top(10)
        hint.set_margin_bottom(4)
        source_wrap.append(hint)
        source_wrap.append(source_scroll)

        tools = Gtk.Box(spacing=6)
        tools.set_margin_start(12)
        tools.set_margin_end(12)
        tools.set_margin_top(8)
        all_btn = Gtk.Button(label="Select all")
        none_btn = Gtk.Button(label="Select none")
        all_btn.connect("clicked", lambda *_: self._set_all(True))
        none_btn.connect("clicked", lambda *_: self._set_all(False))
        self.count_label = Gtk.Label(label="Choose a source", xalign=0, hexpand=True)
        tools.append(all_btn)
        tools.append(none_btn)
        tools.append(self.count_label)

        self.photo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.photo_box.set_margin_start(8)
        self.photo_box.set_margin_end(8)
        self.photo_box.set_margin_bottom(8)
        photo_scroll = Gtk.ScrolledWindow()
        photo_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        photo_scroll.set_child(self.photo_box)
        photo_scroll.set_vexpand(True)

        photos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        photos.append(tools)
        photos.append(photo_scroll)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.append(source_wrap)
        body.append(photos)

        dest_row = Gtk.Box(spacing=8)
        dest_row.set_margin_start(12)
        dest_row.set_margin_end(12)
        dest_row.set_margin_top(8)
        dest_label = Gtk.Label(label="Folder", xalign=0)
        self.dest_value = Gtk.Label(label=self._pretty(self.dest), xalign=0, hexpand=True)
        self.dest_value.set_ellipsize(Pango.EllipsizeMode.START)
        self.dest_value.set_tooltip_text(str(self.dest))
        change = Gtk.Button(label="Change")
        change.set_tooltip_text("Folder inside the library. Created if needed.")
        change.connect("clicked", self._on_change_dest)
        dest_row.append(dest_label)
        dest_row.append(self.dest_value)
        dest_row.append(change)

        album_row = Gtk.Box(spacing=8)
        album_row.set_margin_start(12)
        album_row.set_margin_end(12)
        album_row.set_margin_bottom(4)
        album_label = Gtk.Label(label="Album", xalign=0)
        self._album_ids: list[int | None] = [None]
        names = ["Library only"]
        for album in self.store.albums():
            names.append(str(album["name"]))
            self._album_ids.append(int(album["id"]))
        self.album_drop = Gtk.DropDown.new_from_strings(names)
        self.album_drop.set_hexpand(True)
        album_row.append(album_label)
        album_row.append(self.album_drop)

        self.note = Gtk.Label(label="", xalign=0)
        self.note.set_margin_start(12)
        self.note.set_margin_end(12)
        self.note.set_margin_bottom(8)
        self.note.add_css_class("dim-label")
        self.note.set_wrap(True)
        note = camera_download_note()
        if note:
            self.note.set_text(note + " Cards and folders still work.")
        else:
            self.note.set_text("Copies files. Existing names are skipped, never overwritten.")

        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bottom.append(dest_row)
        bottom.append(album_row)
        bottom.append(self.note)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(body)
        toolbar.add_bottom_bar(bottom)
        self.set_content(toolbar)

    def _pretty(self, path: Path) -> str:
        try:
            rel = path.expanduser().relative_to(Path.home())
            return str(Path("~") / rel)
        except ValueError:
            return str(path)

    def _watch_volumes(self) -> None:
        self._poll = GLib.timeout_add_seconds(2, self._poll_sources)
        try:
            self._monitor = Gio.VolumeMonitor.get()
        except Exception:
            self._monitor = None
        else:
            for signal in ("mount-added", "mount-removed", "mount-changed", "volume-added", "volume-removed"):
                try:
                    self._mount_ids.append(self._monitor.connect(signal, self._on_volume_event))
                except Exception:
                    pass
        user = os.environ.get("USER", "").strip()
        candidates = [Path("/run/media"), Path("/media")]
        if user:
            candidates.extend([Path("/run/media") / user, Path("/media") / user])
        for path in candidates:
            if not path.is_dir():
                continue
            try:
                mon = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOUNTS, None)
            except Exception:
                try:
                    mon = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
                except Exception:
                    continue
            mon.connect("changed", self._on_volume_event)
            self._file_monitors.append(mon)

    def _on_volume_event(self, *_args) -> None:
        if self._closed or self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        GLib.timeout_add(250, self._debounced_refresh)

    def _debounced_refresh(self) -> bool:
        self._refresh_scheduled = False
        if not self._closed:
            self._reload_sources()
        return False

    def _poll_sources(self) -> bool:
        if self._closed:
            return False
        self._reload_sources()
        self._camera_ticks += 1
        if self._camera_ticks >= 4:
            self._camera_ticks = 0
            self._detect_cameras_async()
        return True

    def _detect_cameras_async(self) -> None:
        if self._closed or self._camera_busy:
            return
        if camera_download_note():
            return
        self._camera_busy = True

        def work() -> None:
            try:
                cameras, error = detect_cameras()
            except Exception as exc:
                cameras, error = [], str(exc)
            GLib.idle_add(self._cameras_ready, cameras, error)

        threading.Thread(target=work, name="lumen-cameras", daemon=True).start()

    def _cameras_ready(self, cameras, error) -> bool:
        self._camera_busy = False
        if self._closed:
            return False
        self._cameras = list(cameras)
        if error and error != CAMERA_NOTE:
            self.note.set_text(error)
        self._reload_sources()
        return False

    def _reload_sources(self) -> None:
        if self._closed:
            return
        try:
            volumes = discover_volumes()
        except Exception:
            volumes = []
        sources = list(volumes) + list(self._cameras) + list(self._chosen)
        keys = [source.key for source in sources]
        if keys == self._source_keys:
            return
        self._sources = sources
        self._source_keys = keys
        self.source_list.unselect_all()
        child = self.source_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.source_list.remove(child)
            child = nxt
        if not sources:
            row = Gtk.ListBoxRow(selectable=False, activatable=False)
            label = Gtk.Label(
                label="No card or camera yet.\nChoose a folder, or connect one.",
                xalign=0,
            )
            label.set_wrap(True)
            label.set_margin_start(12)
            label.set_margin_end(12)
            label.set_margin_top(8)
            label.set_margin_bottom(8)
            label.add_css_class("dim-label")
            row.set_child(label)
            self.source_list.append(row)
            self._clear_photos("No card or camera yet.")
            return
        selected_row = None
        for source in sources:
            row = Gtk.ListBoxRow()
            row.source = source
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_start(10)
            box.set_margin_end(10)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            title = Gtk.Label(label=source.title, xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(title)
            if source.kind == "camera":
                detail = source.port or "Camera"
            elif source.path:
                detail = source.path
            elif source.device:
                detail = "Not mounted yet"
            else:
                detail = ""
            sub = Gtk.Label(label=detail, xalign=0)
            sub.add_css_class("dim-label")
            sub.set_ellipsize(Pango.EllipsizeMode.START)
            box.append(sub)
            row.set_child(box)
            self.source_list.append(row)
            if source.key == self._selected_key:
                selected_row = row
        if selected_row is not None:
            self.source_list.select_row(selected_row)
        elif self._selected_key is not None:
            self._selected_key = None
            self._clear_photos("That source is no longer connected.")

    def _on_source_selected(self, _box, row) -> None:
        if row is None or self._importing:
            return
        source = getattr(row, "source", None)
        if source is None:
            return
        if source.key == self._selected_key and self._checks:
            return
        self._selected_key = source.key
        self._list_gen += 1
        gen = self._list_gen
        self._clear_photos("Looking for photos…")
        if source.kind == "camera":
            self._list_camera(source, gen)
        elif source.path:
            self._list_path(source.path, gen)
        elif source.device:
            self._mount_then_list(source, gen)
        else:
            self._clear_photos("This source has no folder to browse.")

    def _mount_then_list(self, source: ImportSource, gen: int) -> None:
        device = source.device or ""

        def work() -> None:
            try:
                path = mount_device(device)
                error = None
            except Exception as exc:
                path = None
                error = str(exc)
            GLib.idle_add(self._mount_ready, gen, source.key, path, error)

        threading.Thread(target=work, name="lumen-mount", daemon=True).start()

    def _mount_ready(self, gen: int, key: str, path: str | None, error: str | None) -> bool:
        if self._closed or gen != self._list_gen:
            return False
        if error or not path:
            self._clear_photos(error or "Could not mount this card.")
            return False
        for source in self._sources:
            if source.key == key:
                source.path = path
                source.mounted = True
                source.key = "vol:" + path
                break
        self._selected_key = "vol:" + path
        self._list_path(path, gen)
        self._source_keys = []
        self._reload_sources()
        return False

    def _list_path(self, path: str, gen: int) -> None:
        def work() -> None:
            try:
                photos = list_folder_photos(Path(path))
                error = None
            except Exception as exc:
                photos = []
                error = str(exc)
            GLib.idle_add(self._photos_ready, gen, photos, error)

        threading.Thread(target=work, name="lumen-import-list", daemon=True).start()

    def _list_camera(self, source: ImportSource, gen: int) -> None:
        port = source.port or ""

        def work() -> None:
            try:
                photos = list_camera_photos(port)
                error = None
            except Exception as exc:
                photos = []
                error = str(exc)
            GLib.idle_add(self._photos_ready, gen, photos, error)

        threading.Thread(target=work, name="lumen-import-camera", daemon=True).start()

    def _clear_photos(self, message: str) -> None:
        self._checks = []
        child = self.photo_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.photo_box.remove(child)
            child = nxt
        self.count_label.set_text(message)
        self.import_btn.set_sensitive(False)

    def _photos_ready(self, gen: int, photos: list[ListedPhoto], error: str | None) -> bool:
        if self._closed or gen != self._list_gen:
            return False
        self._clear_photos("")
        if error:
            self.count_label.set_text(error)
            return False
        if not photos:
            self.count_label.set_text("No photos in this source.")
            return False
        for photo in photos:
            row = Gtk.Box(spacing=8)
            row.set_margin_top(2)
            row.set_margin_bottom(2)
            check = Gtk.CheckButton()
            check.set_active(True)
            check.connect("toggled", lambda *_: self._update_count())
            pic = Gtk.Picture()
            pic.set_size_request(64, 48)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(True)
            if (
                photo.path
                and not photo.raw
                and photo.size is not None
                and photo.size <= _THUMB_BYTES
            ):
                try:
                    pic.set_filename(photo.path)
                except Exception:
                    pic.set_visible(False)
            else:
                pic.set_visible(False)
            text = photo.label
            if photo.raw:
                text += "  ·  raw"
            label = Gtk.Label(label=text, xalign=0, hexpand=True)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            label.set_tooltip_text(photo.path or photo.label)
            row.append(check)
            row.append(pic)
            row.append(label)
            self.photo_box.append(row)
            self._checks.append((check, photo))
        self._update_count()
        return False

    def _set_all(self, active: bool) -> None:
        for check, _photo in self._checks:
            check.set_active(active)
        self._update_count()

    def _update_count(self) -> None:
        total = len(self._checks)
        selected = sum(1 for check, _photo in self._checks if check.get_active())
        noun = "photo" if total == 1 else "photos"
        self.count_label.set_text(f"{selected} of {total} {noun} selected")
        self.import_btn.set_sensitive(selected > 0 and not self._importing)

    def _on_choose_folder(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Choose a folder to import")
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or not folder.get_path():
            return
        path = str(Path(folder.get_path()).resolve())
        source = ImportSource(
            key="dir:" + path,
            title=Path(path).name or path,
            kind="folder",
            path=path,
        )
        self._chosen = [item for item in self._chosen if item.key != source.key]
        self._chosen.append(source)
        self._selected_key = source.key
        self._source_keys = []
        self._reload_sources()
        self._select_key(source.key)

    def _select_key(self, key: str) -> None:
        child = self.source_list.get_first_child()
        while child is not None:
            source = getattr(child, "source", None)
            if source is not None and source.key == key:
                self.source_list.select_row(child)
                return
            child = child.get_next_sibling()

    def _on_change_dest(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Library folder for imported photos")
        current = self.dest.expanduser()
        parent = current if current.is_dir() else current.parent
        if parent.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(parent)))
        dialog.select_folder(self, None, self._on_dest_chosen)

    def _on_dest_chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or not folder.get_path():
            return
        self.dest = Path(folder.get_path())
        self.dest_value.set_text(self._pretty(self.dest))
        self.dest_value.set_tooltip_text(str(self.dest))

    def _selected_source(self) -> ImportSource | None:
        for source in self._sources:
            if source.key == self._selected_key:
                return source
        return None

    def _album_id(self) -> int | None:
        try:
            index = int(self.album_drop.get_selected())
        except Exception:
            index = 0
        if index < 0 or index >= len(self._album_ids):
            return None
        return self._album_ids[index]

    def _on_import(self, *_args) -> None:
        if self._importing:
            return
        chosen = [photo for check, photo in self._checks if check.get_active()]
        if not chosen:
            self.count_label.set_text("Select at least one photo.")
            return
        source = self._selected_source()
        if source is None:
            self.count_label.set_text("Choose a card, camera, or folder.")
            return
        album_id = self._album_id()
        dest = self.dest
        self._importing = True
        self.import_btn.set_sensitive(False)
        self.title.set_subtitle("Importing…")
        self._list_gen += 1

        def work() -> None:
            try:
                if source.kind == "camera":
                    result = import_camera_files(
                        self.store, chosen, dest, source.port or "", album_id
                    )
                else:
                    paths = [Path(photo.path) for photo in chosen if photo.path]
                    roots = [Path(source.path)] if source.path else []
                    result = import_local_files(
                        self.store, paths, dest, album_id, source_roots=roots
                    )
            except Exception as exc:
                result = ImportResult(dest=str(dest), errors=[str(exc)])
            GLib.idle_add(self._import_ready, result)

        threading.Thread(target=work, name="lumen-import", daemon=True).start()

    def _import_ready(self, result: ImportResult) -> bool:
        if self._closed:
            return False
        self._importing = False
        copied = len(result.copied)
        skipped = len(result.skipped)
        if result.errors and not copied:
            self.title.set_subtitle("Import failed")
            self.note.set_text(result.errors[0])
            self._update_count()
            return False
        parts = [f"Imported {copied}"]
        if skipped:
            parts.append(f"skipped {skipped} existing")
        if result.errors:
            parts.append(result.errors[0])
        summary = ", ".join(parts)
        if result.root:
            try:
                self.on_done(result, summary)
            except Exception:
                pass
            self.close()
            return False
        self.title.set_subtitle(summary)
        self.note.set_text(summary)
        self._update_count()
        return False

    def _on_close(self, *_args) -> bool:
        self._closed = True
        if self._poll:
            GLib.source_remove(self._poll)
            self._poll = 0
        monitor = getattr(self, "_monitor", None)
        if monitor is not None:
            for sig in self._mount_ids:
                try:
                    monitor.disconnect(sig)
                except Exception:
                    pass
        self._mount_ids = []
        self._file_monitors = []
        return False
