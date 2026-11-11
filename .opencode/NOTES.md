# sai-lineart

## Run / test

- `python3 -m sai` — UI (PySide6 is installed system-wide, no venv needed).
- `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v` — tests.
- `ruff check sai tests && ruff format sai tests` — lint/format.
- `basedpyright sai tests` — types (0 errors expected).

## Architecture

- `sai/model.py` — `Point(x, y, width, smooth)`, `Stroke`, `Layer`, `Mask`, `Document`. JSON is the
  master format: `{"format": "sai-lineart", "version": 2, ...}` (version 1 loads and migrates).
- `sai/geometry.py` — `sample_stroke()` turns points into `(x, y, width)` samples.
  A segment that starts at a sharp point is a straight chord: the corner ends the straight
  run, rounding resumes only after the next point (rule "B", user choice 2026-11-11).
  Every other segment is a cubic circular-arc approximation (`handle = 4*tan(theta/4)*R`)
  whose tangent comes from the circle through the point and its two neighbours (endpoints
  included); a sharp point falls back to the chord on its own side of that arc.
  The tangent at a smooth point is shared by both segments (`_segment_tangents`), and when
  the incoming segment was a straight run the shared tangent is that run's direction — so
  the line passes through a smooth point and only then bends, never kinking there.
  Parallel end tangents keep the Hermite (`handle = chord/3`) instead of collapsing to the
  chord, otherwise the join would kink.
  Three smooth points form a semicircle. No roundness parameter.
- `sai/render.py` — QPainter: one round-capped line per sample pair, width averaged per pair.
  No polygon offsetting, so no self-intersection artifacts.
- `sai/io.py` — JSON save/load, PNG export (transparent, scale), SVG export.
- `sai/canvas.py` — input, tools, zoom/pan, undo snapshots (full `Document.copy()`).
- `sai/app.py` — toolbar, layers dock, menus. Layer list items are editable (`ItemIsEditable`);
  `_on_layer_item_changed` handles both the visibility checkbox and renames (compare `item.text()`
  with `layer.name`). Raster layers are marked with a generated icon instead of a `[image]` suffix,
  so the editable text is exactly the layer name.

## Quirks

- SVG has no per-point stroke width. Export emits one `<line>` per sample with
  `stroke-linecap="round"`, so width variation survives but the file is verbose.
- Undo is snapshot-based; acceptable up to a few thousand strokes.
- PySide6 6.12 + basedpyright: use scoped enums (`Qt.MouseButton.LeftButton`,
  `QPainter.RenderHint.Antialiasing`). Unscoped aliases fail type checking.
- `sai/canvas.py` keeps `self.doc`; undo/redo replace the object, so always read
  `canvas.doc` instead of caching a reference.
- Wheel width in Edit (`_adjust_point_width`) pushes one undo per wheel burst on one point
  (`_width_edit_key = (id(stroke), index)`); any mouse move/press or undo/redo/`set_document`
  clears the key so the next burst starts a new entry.
- File dialogs go through `xdg-desktop-portal-termfilechooser` + yazi here
  (`QT_QPA_PLATFORMTHEME=xdgdesktopportal`). When the app passes no suggested file name the
  portal creates `$HOME/termfilechooser.tmp` and yazi's Enter returns that path, so saves land
  as `*.tmp`. Hence `io.ensure_suffix()` plus a suggested name in every `getSaveFileName` call —
  keep passing `_suggested_save_path()` when adding new dialogs.

## Raster layers / PNG import
- Raster layers keep a base64 PNG in `Layer.image_base64`; `render.layer_image()` decodes and caches it.
- `QImage.save(QBuffer, "PNG")` fails PySide6 6.12 stubs/runtime typing; encode via a temp file instead.
- Embedded base64 grows the JSON master file; move to external assets if documents get heavy.

## Clipping / masks
- `Layer.clip_to_below` + shared masks: `Document.masks: dict[str, Mask]` (alpha PNG) and
  `Layer.mask_id`. One `Mask` can serve any number of layers; `Mask._cached_image` holds the
  decoded QImage. Version-1 files keep the payload in `Layer` JSON key `"mask"`; `Document.from_dict`
  migrates it into `masks` as `mask-N`.
- Rendering groups a base layer with following clipped layers: each is rendered to an offscreen
  QImage, masked with `CompositionMode_DestinationIn` (own mask, then base alpha), then composited.
- Plain layers still draw directly; offscreen buffers are only allocated when a mask/clip exists.

## Recolor tool
- `Canvas.TOOL_RECOLOR` (`6`): `_stroke_at()` picks the closest stroke **of the active visible
  vector layer** (`active_vector_layer()`); the hit limit is `max(hit_radius, width/2)` with the
  width interpolated at the nearest curve parameter, so thick lines are hit near their edge.
- The gesture (`_recolor_gesture`) pushes one undo snapshot per drag via `_recolor_dirty`,
  set false on press/release; a miss or a stroke that already has the colour writes no undo.
- `_hovered_stroke` is drawn by `_draw_stroke_hover` in Edit/Width/Erase (hover outline) and by
  `_draw_recolor_overlay` (outline + colour preview). `clear_hover()` is called when the active
  layer changes or its visibility flips.
- `_draw_point_handles()` is shared: point marks are drawn in Edit/Width/Erase and in Recolor
  (user request: same anchors everywhere), for `active_vector_layer()` only.
- Single-point strokes ("dots") need their own hit test: `nearest_curve_point()` returns index -1
  for `len(points) < 2`, so `_stroke_at()` measures the distance to the point itself with limit
  `max(hit_radius, width/2)`. The same blind spot exists in any curve-based picker.

## Layer scoping of editing tools
- Edit (points, insertion, deletion, wheel width), Width, Erase and Recolor only touch
  `Canvas.active_vector_layer()` — the active layer if it is visible and vector. Point handles in
  the overlay are drawn for that layer only. Don't switch these loops back to `doc.layers`
  without a reason: it makes editing ambiguous (user request, 2026-11-11).

## View rotation
- `Canvas.rotation` is a view-only angle (degrees, clockwise). Mapping is
  `screen = pan + zoom * R(rotation) * scene`; `to_scene` inverts it, `scene_to_screen` forwards it.
- `paintEvent` does `translate(pan) -> rotate(rotation) -> scale(zoom)`; `_zoom_at` re-derives `pan`
  through `scene_to_screen`, so the point under the cursor stays put while rotated.
- `set_rotation` keeps the widget centre anchored (the canvas does not fly off screen).
- The document and exports never see `rotation`; `set_document` resets it to 0.
- `mouseReleaseEvent` must reset `_panning` before the `transform_mode` early return; otherwise a
  middle-click pan inside transform mode never ends and the view keeps following the cursor.
  Any new early return in the release path has the same trap.


## Transform mode
- `Canvas.begin_transform()` snapshots the active layer + doc, shows an overlay box from
  `transform.content_bounds()`; edits are applied live from the snapshot via
  `transform.apply_transform()` (rotate/scale/translate around a pivot).
- Edge drag = axis scale, inside = move, outside = rotate; Enter commits (one undo), Esc restores.
- Raster layers are re-encoded from the snapshot on every live update, so no cumulative resampling.

## Bucket fill
- `sai/fill.py`: renders the document without the target layer to get walls from alpha,
  scanline flood fill (numpy), dilates the mask by 1 px so the fill tucks under antialiased lines.
- Target must be a raster layer; the Fill tool creates one below the vector layer if needed.
- `_alpha_array()` MUST `.copy()` the numpy view of `QImage.constBits()` — a view outlives the
  temporary `Format_Alpha8` QImage and segfaults on large canvases.
- 2000x2000 fill takes ~1.1 s.
- Fill walls: vector layers plus raster layers ABOVE the target; raster layers below are
  background only, so an open region is bounded by the canvas. This matters when an opaque
  underlay or a previous fill sits below the fill layer.
