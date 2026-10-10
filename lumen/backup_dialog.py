"""Backup window: pick a folder, then copy the library into it."""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from lumen.backup import BackupResult, backup_library, format_progress
from lumen.store import Store

HELP = (
    "Copies every indexed photo and the library database, so albums, ratings, "
    "tags, and edits come along. A file already at the destination with the "
    "same size is skipped. A newer file already there is left alone. "
    "Originals are never changed or deleted.\n\n"
    "Pick any folder you can open. A mounted NAS works. So does a folder that "
    "Dropbox, Google Drive, or Nextcloud already syncs. Lumen does not sign "
    "in to a cloud service."
)


class BackupDialog(Adw.Window):
    def __init__(self, parent, store: Store) -> None:
        super().__init__(title="Back up library")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(560, 360)
        self.store = store
        self.dest: Path | None = None
        self._running = False
        self._closed = False
        self._scheduled = False
        self._pending: tuple[int, int, int, int, int] | None = None
        saved = store.get_backup_dir().strip()
        if saved:
            self.dest = Path(saved)
        self._build()
        self.connect("close-request", self._on_close)

    def _build(self) -> None:
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle.new("Back up library", "Copy. Nothing is deleted.")
        header.set_title_widget(self.title)
        cancel = Gtk.Button(label="Close")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.sync_btn = Gtk.Button(label="Sync now")
        self.sync_btn.add_css_class("suggested-action")
        self.sync_btn.set_tooltip_text("Copy photos that are missing or older. Same-size files are skipped.")
        self.sync_btn.connect("clicked", self._on_sync)
        header.pack_end(self.sync_btn)

        help_label = Gtk.Label(label=HELP, xalign=0, wrap=True)
        help_label.set_margin_start(16)
        help_label.set_margin_end(16)
        help_label.set_margin_top(12)
        help_label.add_css_class("dim-label")
        help_label.set_selectable(True)

        row = Gtk.Box(spacing=8)
        row.set_margin_start(16)
        row.set_margin_end(16)
        row.set_margin_top(16)
        dest_label = Gtk.Label(label="Folder", xalign=0)
        self.dest_value = Gtk.Label(label="", xalign=0, hexpand=True)
        self.dest_value.set_ellipsize(Pango.EllipsizeMode.START)
        choose = Gtk.Button(label="Choose folder")
        choose.set_tooltip_text("Mounted NAS, cloud sync folder, or any other folder")
        choose.connect("clicked", self._on_choose)
        self.choose_btn = choose
        row.append(dest_label)
        row.append(self.dest_value)
        row.append(choose)

        self.progress = Gtk.Label(label="", xalign=0)
        self.progress.set_margin_start(16)
        self.progress.set_margin_end(16)
        self.progress.set_margin_top(12)
        self.result = Gtk.Label(label="", xalign=0, wrap=True)
        self.result.set_margin_start(16)
        self.result.set_margin_end(16)
        self.result.set_margin_top(8)
        self.result.set_margin_bottom(16)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(help_label)
        body.append(row)
        body.append(self.progress)
        body.append(self.result)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(body)
        self.set_content(toolbar)
        self._show_dest()

    def _pretty(self, path: Path) -> str:
        try:
            rel = path.expanduser().relative_to(Path.home())
            return str(Path("~") / rel)
        except ValueError:
            return str(path)

    def _show_dest(self) -> None:
        if self.dest is None:
            self.dest_value.set_text("Choose a folder")
            self.dest_value.set_tooltip_text("")
            self.sync_btn.set_sensitive(False)
            return
        self.dest_value.set_text(self._pretty(self.dest))
        self.dest_value.set_tooltip_text(str(self.dest))
        if self.dest.is_dir():
            self.sync_btn.set_sensitive(not self._running)
            self.title.set_subtitle(str(self.dest))
        else:
            self.sync_btn.set_sensitive(False)
            self.result.set_text("That folder is not available. Choose it again when the disk is mounted.")

    def _on_choose(self, *_args) -> None:
        if self._running:
            return
        dialog = Gtk.FileDialog(title="Backup folder")
        if self.dest is not None and self.dest.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.dest)))
        dialog.select_folder(self, None, self._on_folder)

    def _on_folder(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or not folder.get_path():
            return
        path = Path(folder.get_path())
        try:
            remembered = self.store.set_backup_dir(str(path))
        except OSError as exc:
            self.result.set_text(str(exc))
            return
        self.dest = Path(remembered)
        self.result.set_text("")
        self.progress.set_text("")
        self._show_dest()

    def _on_sync(self, *_args) -> None:
        if self._running or self.dest is None or not self.dest.is_dir():
            return
        try:
            self.store.set_backup_dir(str(self.dest))
        except OSError as exc:
            self.result.set_text(str(exc))
            return
        self._running = True
        self.sync_btn.set_sensitive(False)
        self.choose_btn.set_sensitive(False)
        self.title.set_subtitle("Copying…")
        self.progress.set_text(format_progress(0, 0, 0, 0, 0))
        self.result.set_text("")
        dest = self.dest

        def report(done: int, total: int, copied: int, skipped: int, failed: int) -> None:
            self._pending = (done, total, copied, skipped, failed)
            if not self._scheduled:
                self._scheduled = True
                GLib.idle_add(self._flush_progress)

        def work() -> None:
            try:
                result = backup_library(self.store, dest, progress=report)
            except Exception as exc:
                result = BackupResult(destination=str(dest), errors=[str(exc)])
            GLib.idle_add(self._finished, result)

        threading.Thread(target=work, name="lumen-backup", daemon=True).start()

    def _flush_progress(self) -> bool:
        self._scheduled = False
        if self._closed or self._pending is None:
            return False
        done, total, copied, skipped, failed = self._pending
        self.progress.set_text(format_progress(done, total, copied, skipped, failed))
        return False

    def _finished(self, result: BackupResult) -> bool:
        if self._closed:
            return False
        self._running = False
        self.choose_btn.set_sensitive(True)
        self.progress.set_text(
            format_progress(result.total, result.total, result.copied, result.skipped, result.failed)
        )
        detail = result.summary()
        if result.errors:
            detail = detail + "\n" + "\n".join(result.errors)
        self.result.set_text(detail)
        if result.failed or not result.database_ok:
            self.title.set_subtitle("Finished with problems")
        else:
            self.title.set_subtitle(result.summary())
        self._show_dest()
        return False

    def _on_close(self, *_args) -> bool:
        if self._running:
            return True
        self._closed = True
        return False
