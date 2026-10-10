"""A4 photobook layout, shared by Print and Export PDF.

Pages alternate: one photo, then two stacked photos. A leftover photo on a
two-up page is still drawn (top slot) instead of being dropped. Original
image files are never written.
"""

from __future__ import annotations

import traceback

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk
from PIL import Image

from lumen.imaging import flatten_rgb, render

# A4 portrait. 12 mm margins leave a 186 mm × 273 mm content box.
A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
MARGIN_MM = 12.0
GAP_MM = 8.0
# Pixel size used when shrinking a photo into a slot. Cairo scales it onto the page.
PRINT_DPI = 200


def plan_pages(count: int) -> list[tuple[str, int]]:
    """Return (layout, photo_count) per page.

    layout is "one" or "two". The last two-up page may hold a single photo.
    """
    pages: list[tuple[str, int]] = []
    index = 0
    one_up = True
    while index < count:
        if one_up:
            pages.append(("one", 1))
            index += 1
        else:
            take = 1 if index + 1 == count else 2
            pages.append(("two", take))
            index += take
        one_up = not one_up
    return pages


def describe_book(count: int, *, selected: bool) -> str:
    if count <= 0:
        if selected:
            return "The photo tray is empty."
        return "No photos to include. Select thumbnails, or show some photos in the grid."
    pages = plan_pages(count)
    n_pages = len(pages)
    source = "Photo tray" if selected else "This view (photo tray is empty)"
    photo_word = "photo" if count == 1 else "photos"
    page_word = "page" if n_pages == 1 else "pages"
    text = (
        f"{source}: {count} {photo_word}, {n_pages} A4 {page_word}. "
        "Odd pages are one photo. Even pages are two photos, one above the other."
    )
    if pages[-1] == ("two", 1):
        text += " The last page has the leftover photo in the top half."
    return text


def content_slots(layout: str, width: float, height: float) -> list[tuple[float, float, float, float]]:
    """Slot rectangles (x, y, w, h) inside the content box, in millimetres."""
    if layout != "two":
        return [(0.0, 0.0, width, height)]
    gap = GAP_MM if height > GAP_MM else 0.0
    slot_h = (height - gap) / 2.0
    return [
        (0.0, 0.0, width, slot_h),
        (0.0, slot_h + gap, width, slot_h),
    ]


def place(src_w: float, src_h: float, slot: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Fit src inside slot, keep aspect ratio, center it. Returns x, y, w, h."""
    sx, sy, sw, sh = slot
    if src_w <= 0 or src_h <= 0 or sw <= 0 or sh <= 0:
        return sx, sy, 0.0, 0.0
    scale = min(sw / src_w, sh / src_h)
    dw = src_w * scale
    dh = src_h * scale
    return sx + (sw - dw) / 2.0, sy + (sh - dh) / 2.0, dw, dh


def load_pages(items: list[tuple[str, list]], *, single: bool) -> list[tuple[str, list]]:
    """items are (path, edit ops). single forces one one-up page (the open photo)."""
    images: list[Image.Image] = []
    for path, ops in items:
        try:
            images.append(flatten_rgb(render(path, ops)))
        except Exception:
            traceback.print_exc()
            images.append(Image.new("RGB", (32, 24), (220, 220, 220)))
    if single:
        return [("one", images[:1])] if images else []
    plan = plan_pages(len(images))
    pages: list[tuple[str, list]] = []
    index = 0
    for layout, count in plan:
        pages.append((layout, images[index : index + count]))
        index += count
    return pages


def _surface_for(im: Image.Image, slot_w: float, slot_h: float):
    """ARGB32 cairo surface, sized for the slot. The buffer must stay referenced."""
    max_w = max(1, int(round(slot_w / 25.4 * PRINT_DPI)))
    max_h = max(1, int(round(slot_h / 25.4 * PRINT_DPI)))
    if im.width > max_w or im.height > max_h:
        im = im.copy()
        im.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    rgba = im.convert("RGBA")
    raw = bytearray(rgba.tobytes("raw", "BGRA"))
    surface = cairo.ImageSurface.create_for_data(
        raw, cairo.FORMAT_ARGB32, rgba.width, rgba.height, rgba.width * 4
    )
    return surface, raw


def draw_page(ctx, layout: str, images: list, content_w: float, content_h: float) -> None:
    """Draw one page. ctx is in millimetres, origin at the content box (inside margins)."""
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    slots = content_slots(layout, content_w, content_h)
    # Keep pixel buffers alive until cairo has painted them.
    held = []
    for image, slot in zip(images, slots):
        surface, raw = _surface_for(image, slot[2], slot[3])
        held.append((surface, raw))
        x, y, dw, dh = place(surface.get_width(), surface.get_height(), slot)
        if dw <= 0 or dh <= 0:
            continue
        ctx.save()
        ctx.translate(x, y)
        ctx.scale(dw / surface.get_width(), dh / surface.get_height())
        ctx.set_source_surface(surface, 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_BEST)
        ctx.paint()
        ctx.restore()


def a4_page_setup() -> Gtk.PageSetup:
    setup = Gtk.PageSetup()
    setup.set_orientation(Gtk.PageOrientation.PORTRAIT)
    setup.set_paper_size(Gtk.PaperSize.new(Gtk.PAPER_NAME_A4))
    setup.set_top_margin(MARGIN_MM, Gtk.Unit.MM)
    setup.set_bottom_margin(MARGIN_MM, Gtk.Unit.MM)
    setup.set_left_margin(MARGIN_MM, Gtk.Unit.MM)
    setup.set_right_margin(MARGIN_MM, Gtk.Unit.MM)
    return setup


def run_print(parent, pages: list, export_path: str | None = None) -> Gtk.PrintOperationResult:
    """Print or export. pages is (layout, images) from load_pages. Raises on error."""
    if not pages:
        raise RuntimeError("Nothing to print.")

    def on_begin(op, _context) -> None:
        op.set_n_pages(len(pages))

    def on_draw(op, context, page_nr) -> None:
        if page_nr < 0 or page_nr >= len(pages):
            return
        layout, images = pages[page_nr]
        draw_page(
            context.get_cairo_context(),
            layout,
            images,
            context.get_width(),
            context.get_height(),
        )

    op = Gtk.PrintOperation()
    op.set_job_name("Lumen photobook")
    op.set_n_pages(len(pages))
    op.set_unit(Gtk.Unit.MM)
    op.set_use_full_page(False)
    op.set_embed_page_setup(True)
    op.set_default_page_setup(a4_page_setup())
    op.set_show_progress(export_path is None)
    op.connect("begin-print", on_begin)
    op.connect("draw-page", on_draw)
    if export_path:
        op.set_export_filename(export_path)
        settings = Gtk.PrintSettings()
        settings.set(Gtk.PRINT_SETTINGS_OUTPUT_FILE_FORMAT, "pdf")
        settings.set(Gtk.PRINT_SETTINGS_OUTPUT_URI, GLib.filename_to_uri(export_path))
        settings.set(Gtk.PRINT_SETTINGS_PAPER_FORMAT, Gtk.PAPER_NAME_A4)
        settings.set_orientation(Gtk.PageOrientation.PORTRAIT)
        op.set_print_settings(settings)
        action = Gtk.PrintOperationAction.EXPORT
    else:
        action = Gtk.PrintOperationAction.PRINT_DIALOG
    try:
        result = op.run(action, parent)
    except GLib.Error as exc:
        raise RuntimeError(exc.message) from exc
    if result == Gtk.PrintOperationResult.ERROR:
        err = op.get_error()
        message = err.message if err is not None else "Print failed"
        raise RuntimeError(message)
    return result
