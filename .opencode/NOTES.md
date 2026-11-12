# sai-lineart

## Run / test

- `python3 -m sai` — UI (PySide6 is installed system-wide, no venv needed).
- `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v` — tests.
- `ruff check sai tests && ruff format sai tests` — lint/format.
- `basedpyright sai tests` — types (0 errors expected).

## Architecture

- `sai/model.py` — `Point(x, y, width, smooth)`, `Stroke`, `Layer` (plain, raster or group),
  master format: `{"format": "sai-lineart", "version": 2, ...}` (version 1 loads and migrates).
  `Stroke.closed` means last→first is drawn too; the key is written only when true, so old
  files stay untouched and open.
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
  When both tangents belong to one circle (`_arc_samples`) the segment is sampled on the exact
  circle (≤10° per step), so long arcs and semicircles stay round; the circle is rejected if
  the end tangent disagrees, otherwise the join would kink. The radius is interpolated from
  start to end inside the segment: with nearly collinear points the constructed radii differ,
  and sampling at a constant radius leaves the end point off by a fraction of a pixel — that
  shows up as a notch in thick strokes (user bug report, 2026-11-11).
  A closed stroke wraps neighbours in `_circle_tangents`/`_segment_tangents` and
  `_segment_range` adds the last→first segment.
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

## Layers, groups and clipping
- `Layer` is a node: `kind == "group"` uses `children` (list of `Layer`), ordinary layers use
  `strokes`/`image_base64`. `Document.walk()` yields `(layer, depth)` in draw order and
  **`active_layer` is an index into `walk()`** (groups included), so the UI/undo stay
  index-based. Tree helpers: `locate()`, `insert_below()`/`insert_above()`, `remove_layer()`,
  `swap_with_sibling()`, `all_layers()` (no groups).
- Multi-layer edits use set operations instead of index math: `sort_layers()` (walk order,
  dedupe), `move_layers(layers, target_children, index)` (order kept, indices adjusted for
  removals in the same list, cycles refused via `group_owning()`/`is_ancestor()`),
  `group_layers()` (inserts the group at the topmost selected position, then moves the
  selection inside), `move_selection()` (block-safe direction: reversed when moving up),
  `node_count()` for the "keep at least one layer" guard.
- Selection: `_on_layer_selected()` must NOT rebuild the tree — `tree.clear()` drops Qt's
  shift anchor (bug: ranges always started at the topmost item). It only updates the active
  layer and the opacity/blend/clip rows via `_refresh_layer_properties()`. Shift-range is
  ours: `LayerTree.anchor` = layer of the last plain/Ctrl click, `select_range()` selects the
  DFS slice between anchor and clicked item, current item = clicked end (drives
  `active_layer`). Items are created in `Document.walk()` order, so the slice matches draw
  order.
- The layer panel is a `LayerTree(QTreeWidget)` with `ExtendedSelection` and `InternalMove`.
  `dropEvent` does **not** move items: it asks `drop_target()` for `(siblings, index)` and
  calls `MainWindow._drop_layers()`, then the tree is rebuilt from the model (single source of
  truth). Drop on a group item = append into it, `AboveItem`/`BelowItem` = insert at that
  sibling position, empty viewport = top level. Selection is preserved by layer identity
  across refreshes and is dropped after undo/redo (new document objects).
- Actions that may be no-ops (`_move_layer`, `_drop_layers`) take a `Document.copy()`
  snapshot first and call `Canvas.push_undo_snapshot()` only when something changed.
- Rendering is recursive (`render._draw_stack`): a group draws its children into the current
  painter, and `_apply_composition()` **multiplies** `painter.opacity()` by the node's
  opacity — otherwise a group opacity would be overwritten by its children's 1.0.
- Clip is an alpha operation: `render._clip_mask()` renders the base content into a
  cropped mask (bbox from `silhouette.node_bounds`, +1 px AA), at
  `_mask_scale()` = `max(1, target)` capped by `MAX_MASK_PIXELS = 8M`; `draw_document(scale=)`
  is fed `zoom × devicePixelRatio` from `Canvas.paintEvent` and `scale` from `export_png`, so
  clipped layers are not upscaled from document resolution. Masks are cached in
  `_MASK_CACHE` keyed by `(id(document), uid, _node_signature, scale, bounds, document size)`;
  `skip_stroke` (live preview) bypasses the cache. The base itself is drawn directly, only
  clipped siblings go through `_render_masked()`; the mask is composed in device pixels
  (`resetTransform`), and `inside` needs a buffer-sized mask copy so pixels outside the mask
  rect are cleared too.
- `visible=False` skips painting a node but **keeps it in the clip logic**: a hidden base is
  still rendered to its stencil buffer and masks its clipped siblings, and a hidden group
  base still clips. That is how a mask layer is hidden while staying a mask; only the
  painted output disappears. Tests: `test_hidden_base_still_clips_inside/outside`,
  `test_hidden_group_base_still_clips`.
- `clip` refers to the **previous sibling in the same list**, not the global layer below:
  `inside` = `DestinationIn` with the base render, `outside` = `DestinationOut`. A group can
  be the base for a clipped layer and can itself be clipped.
- Masks were removed (v3): no `Document.masks`, `MaskTerm`, `Layer.mask_id`. A stencil is now
  a PNG layer (File > Import PNG as Layer) used with `clip`, or a group. Legacy `"masks"`/
  `"mask_id"` keys in old JSON are ignored; `"clip": true` still loads as `"inside"`.
- SVG export recurses groups into nested `<g>` (opacity/blend on the group tag) and expresses
  clip: `<clipPath clipPathUnits="userSpaceOnUse">` with the flattened silhouette
  (`silhouette.outline_path`, `outside` = page path `.subtracted(path.simplified())`) when the
  base is opaque, otherwise `<mask>` with `mask-type="alpha"` (`"luminance"` + white rect +
  black mono content for `outside`). A hidden base is emitted inside the definitions only.
  Raster layers export as `<image>` with a PNG data URI.

## Vector fills and scope switch
- `Stroke.uid` / `Layer.uid` (uuid hex) are stable references; they survive copy and are
  written to JSON (`from_dict` regenerates when absent).
- `Stroke.fill` + `fill_alpha` = a filled closed path (render draws `stroke_path()` first).
- A vector fill **duplicates the owner layer** (`Layer.duplicate()` — fresh uid for the
  layer and every stroke, so stroke references never collide) and inserts the copy
  below it as `Fill N`. In the copy, `Canvas._fill_vector_at()` sets `bridge=True` + `fill` +
  `fill_alpha` on the figure stroke, or replaces a multi-stroke figure with one merged
  stroke. The source layer is never touched (user decision 2026-11-11: "duplicate the layer,
  then fill it").
- `Stroke.bridge` closes last -> first with a **straight** segment: `sample_stroke()` appends
  one sample of the first point, so the drawn line and the fill path close identically.
  Never use `Stroke.closed` for that: it samples the closing segment from the neighbour
  tangents, so the link curves away from the silhouette (bug: 5-point bean, +62 px lobe).
- `render.stroke_path()` always closes (>=3 samples), because it only serves fills; the
  fill boundary is the centreline, so the stroke itself covers it.
- Multi-stroke figures: `fill.figure_at()` seeds with the stroke whose **bounding box** is
  nearest (not `nearest_curve_point` — that cost 60 ms per move with 400 strokes) and chains
  strokes whose ends are within `JOIN_TOLERANCE = 10 px` (both directions, reversing as
  needed). If the chain is longer than the seed, the duplicate gets one merged `width=0`
  stroke (same points, joint duplicates dropped) at the position of the first figure stroke,
  with `closed=True` when the chain returns to its start, else `bridge=True`. Nested/adjacent
  figures are still decided by `QPainterPath.contains()`.
- Preview is geometry-only (`Canvas.request_fill_preview()` -> `_fill_preview_path`): the
  tint is `fillPath` of the same closed path, 0.8 ms per move with 400 strokes at 2000²,
  no timers or rasters.
- Raster layers keep the old `fill.region_mask()` + `bucket_fill()` path (walls above,
  rasters below are background); vector fills no longer touch it.
- `convert_to_fill()` copies the active layer, closes its strokes, zeroes width and sets
  `fill` (static conversion of an existing line art layer).
- `Canvas.scope` = `"layer"` (default) | `"image"`; `editable_layers()` feeds every picker
  (`_find_point`, `_insert_point`, `_erase_at`, `_stroke_at`, handles, box selection), so the
  switch is global. Fill walls use the same scope (`fill._is_wall`).
- `Layer.clip` is a string: `"off" | "inside" | "outside"`, referring to the **previous
  sibling in the same list**. `_draw_stack()` collects the run of siblings after the base with
  `clip != "off"`, renders each to its own buffer and masks it with the base image:
  `DestinationIn` for `"inside"`, `DestinationOut` for `"outside"`. Legacy `"clip": true` in
  JSON loads as `"inside"` (`model._clip_mode`). The clipped layer is composited after the
  base, so a pixel inside the base still shows the base colour — check colours, not just
  alpha, in tests.
