from __future__ import annotations

import math

from .model import Point, Stroke

Sample = tuple[float, float, float]  # x, y, width
Vector = tuple[float, float]

EPSILON = 1e-9


def _unit(vx: float, vy: float) -> Vector | None:
    length = math.hypot(vx, vy)
    if length < EPSILON:
        return None
    return (vx / length, vy / length)


def _circumcenter(a: Point, b: Point, c: Point) -> Vector | None:
    d = 2.0 * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y))
    if abs(d) < EPSILON:
        return None
    a2 = a.x * a.x + a.y * a.y
    b2 = b.x * b.x + b.y * b.y
    c2 = c.x * c.x + c.y * c.y
    return (
        (a2 * (b.y - c.y) + b2 * (c.y - a.y) + c2 * (a.y - b.y)) / d,
        (a2 * (c.x - b.x) + b2 * (a.x - c.x) + c2 * (b.x - a.x)) / d,
    )


def _circle_tangents(points: list[Point], closed: bool = False) -> list[Vector | None]:
    """Circle tangent at every point.

    The tangent comes from the circle through the point and its two neighbours,
    so the whole stroke follows arcs. A closed stroke wraps its neighbours, so
    the seam point is smooth too. ``None`` only when the neighbours are collinear.
    """
    count = len(points)
    tangents: list[Vector | None] = [None] * count
    if count < 3:
        return tangents
    for index in range(count):
        if closed:
            before = points[(index - 1) % count]
            after = points[(index + 1) % count]
            triple = (before, points[index], after)
            travel = (after.x - before.x, after.y - before.y)
        elif index == 0:
            triple = (points[0], points[1], points[2])
            travel = (points[1].x - points[0].x, points[1].y - points[0].y)
        elif index == count - 1:
            triple = (points[count - 3], points[count - 2], points[count - 1])
            travel = (
                points[count - 1].x - points[count - 2].x,
                points[count - 1].y - points[count - 2].y,
            )
        else:
            triple = (points[index - 1], points[index], points[index + 1])
            travel = (
                points[index + 1].x - points[index - 1].x,
                points[index + 1].y - points[index - 1].y,
            )
        center = _circumcenter(*triple)
        if center is None:
            continue
        radius_x = points[index].x - center[0]
        radius_y = points[index].y - center[1]
        tangent = (-radius_y, radius_x)
        if tangent[0] * travel[0] + tangent[1] * travel[1] < 0:
            tangent = (-tangent[0], -tangent[1])
        tangents[index] = _unit(*tangent)
    return tangents


def _segment_tangents(
    points: list[Point],
    circle_tangents: list[Vector | None],
    index: int,
    chord_unit: Vector,
    closed: bool = False,
) -> tuple[Vector, Vector]:
    """Tangents (start, end) for the segment ``index -> index+1``.

    A tangent is shared by the two segments meeting at a smooth point, so the
    curve never kinks there. When the incoming segment was a straight run from a
    sharp point, the shared tangent is that run's direction: the line passes
    through the point and only then starts to bend.
    """
    count = len(points)
    start = points[index]
    end = points[(index + 1) % count]
    if closed:
        previous = points[(index - 1) % count]
    elif index > 0:
        previous = points[index - 1]
    else:
        previous = None
    if previous is not None and not previous.smooth:
        t1 = _unit(start.x - previous.x, start.y - previous.y) or chord_unit
    else:
        t1 = circle_tangents[index] or chord_unit
    if end.smooth:
        t2 = circle_tangents[(index + 1) % count] or chord_unit
    else:
        # Sharp end: the arc arrives along its chord, so the corner stays sharp.
        t2 = chord_unit
    return t1, t2


def _hermite(p1: Point, p2: Point, m1: Vector, m2: Vector, t: float) -> tuple[float, float]:
    t2 = t * t
    t3 = t2 * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    x = h00 * p1.x + h10 * m1[0] + h01 * p2.x + h11 * m2[0]
    y = h00 * p1.y + h10 * m1[1] + h01 * p2.y + h11 * m2[1]
    return x, y


def _straight_samples(
    start: Point,
    end: Point,
    length: float,
    min_step: float,
    max_steps: int,
    dense: bool = False,
) -> list[tuple[float, float, float, float]]:
    uniform = abs(end.width - start.width) < 0.01
    steps = max(2, min(max_steps, int(length / min_step))) if dense or not uniform else 1
    return [
        (
            step / steps,
            start.x + (end.x - start.x) * step / steps,
            start.y + (end.y - start.y) * step / steps,
            start.width + (end.width - start.width) * step / steps,
        )
        for step in range(1, steps + 1)
    ]


def _arc_samples(
    start: Point,
    end: Point,
    t1: Vector,
    t2: Vector,
    min_step: float,
    max_steps: int,
    dense: bool,
) -> list[tuple[float, float, float, float]] | None:
    """Exact circular-arc samples, or ``None`` when the tangents disagree.

    Both end tangents must belong to one circle; the centre is where the
    perpendiculars meet. Sampling the real circle (instead of one cubic) keeps
    long arcs, including semicircles, round.
    """
    n1 = (-t1[1], t1[0])
    n2 = (-t2[1], t2[0])
    denominator = n1[0] * n2[1] - n1[1] * n2[0]
    if abs(denominator) < EPSILON:
        # Parallel tangents: only a semicircle over the chord can satisfy them.
        center_x = (start.x + end.x) / 2.0
        center_y = (start.y + end.y) / 2.0
        radius = math.hypot(start.x - center_x, start.y - center_y)
        end_length = radius
    else:
        dx = end.x - start.x
        dy = end.y - start.y
        along = (dx * n2[1] - dy * n2[0]) / denominator
        center_x = start.x + along * n1[0]
        center_y = start.y + along * n1[1]
        radius = math.hypot(start.x - center_x, start.y - center_y)
        end_length = math.hypot(end.x - center_x, end.y - center_y)
        if abs(radius - end_length) > max(0.05, radius * 5e-3):
            return None
    if radius < EPSILON:
        return None
    start_radius = (start.x - center_x, start.y - center_y)
    end_radius = (end.x - center_x, end.y - center_y)
    if abs(t1[0] * start_radius[0] + t1[1] * start_radius[1]) > radius * 1e-3:
        return None

    start_angle = math.atan2(start_radius[1], start_radius[0])
    end_angle = math.atan2(end_radius[1], end_radius[0])
    direction = 1.0 if -math.sin(start_angle) * t1[0] + math.cos(start_angle) * t1[1] >= 0 else -1.0
    sweep = end_angle - start_angle
    while sweep > math.pi:
        sweep -= 2.0 * math.pi
    while sweep <= -math.pi:
        sweep += 2.0 * math.pi
    if sweep * direction < 0:
        sweep += 2.0 * math.pi * direction

    # The circle must also leave the end point along the requested tangent,
    # otherwise the join with the next segment would kink.
    end_tangent = (
        -direction * math.sin(end_angle),
        direction * math.cos(end_angle),
    )
    if end_tangent[0] * t2[0] + end_tangent[1] * t2[1] < 0.999:
        return None

    arc_length = (radius + end_length) * 0.5 * abs(sweep)
    steps = max(2, math.ceil(abs(sweep) / math.radians(10.0)))
    step_limit = 0.5 if dense else min_step
    steps = max(steps, min(max_steps, int(arc_length / step_limit)))
    steps = min(max_steps, steps)
    result: list[tuple[float, float, float, float]] = []
    for step in range(1, steps + 1):
        t = step / steps
        angle = start_angle + sweep * t
        # Interpolating the radius keeps both endpoints exact even when the
        # tangents are only approximately circular.
        current = radius + (end_length - radius) * t
        x = center_x + current * math.cos(angle)
        y = center_y + current * math.sin(angle)
        width = start.width + (end.width - start.width) * t
        result.append((t, x, y, width))
    return result


def _segment_curve(
    points: list[Point],
    circle_tangents: list[Vector | None],
    index: int,
    min_step: float,
    max_steps: int,
    dense: bool = False,
    closed: bool = False,
) -> list[tuple[float, float, float, float]]:
    """Samples ``(t, x, y, width)`` for one segment, excluding its start."""
    start = points[index]
    end = points[(index + 1) % len(points)]
    chord = (end.x - start.x, end.y - start.y)
    chord_length = math.hypot(*chord)
    chord_unit = _unit(*chord) or (0.0, 0.0)

    # A corner starts a straight run: from a sharp point the line goes straight
    # to the next point, rounding resumes only after it.
    if not start.smooth or chord_length < EPSILON:
        return _straight_samples(start, end, chord_length, min_step, max_steps, dense)

    t1, t2 = _segment_tangents(points, circle_tangents, index, chord_unit, closed)
    arc = _arc_samples(start, end, t1, t2, min_step, max_steps, dense)
    if arc is not None:
        return arc

    dot = max(-1.0, min(1.0, t1[0] * t2[0] + t1[1] * t2[1]))
    cross = t1[0] * t2[1] - t1[1] * t2[0]
    theta = abs(math.atan2(cross, dot))
    if math.sin(theta / 2.0) < 1e-6:
        # Parallel tangents: a circular arc would degenerate, but a plain chord
        # would kink against the neighbouring segment. Keep a Hermite that
        # leaves and arrives along the tangents.
        handle = chord_length / 3.0
    else:
        # Cubic Bezier approximation of a circular arc: handle = 4*tan(theta/4)*R.
        radius = chord_length / (2.0 * math.sin(theta / 2.0))
        handle = 4.0 * math.tan(theta / 4.0) * radius
    m1 = (t1[0] * handle, t1[1] * handle)
    m2 = (t2[0] * handle, t2[1] * handle)

    steps = max(2, min(max_steps, int(chord_length / min_step)))
    result: list[tuple[float, float, float, float]] = []
    for step in range(1, steps + 1):
        t = step / steps
        x, y = _hermite(start, end, m1, m2, t)
        width = start.width + (end.width - start.width) * t
        result.append((t, x, y, width))
    return result


def sample_stroke(
    stroke: Stroke,
    min_step: float = 1.5,
    max_steps: int = 64,
    dense: bool = False,
) -> list[Sample]:
    """Turn a stroke into a dense polyline of (x, y, width).

    A segment leaving a sharp point is a straight chord: the corner ends the
    straight run and rounding resumes after the next point. Every other segment
    is a circular arc; its tangent comes from the circle through the point and
    its two neighbours (endpoints included), falling back to the chord when the
    point is sharp. No user-facing roundness parameter.
    """
    points = stroke.points
    if not points:
        return []
    if len(points) == 1:
        point = points[0]
        return [(point.x, point.y, point.width)]

    closed = stroke.closed and len(points) >= 3
    circle_tangents = _circle_tangents(points, closed)
    samples: list[Sample] = [(points[0].x, points[0].y, points[0].width)]
    for index in _segment_range(len(points), closed):
        for _, x, y, width in _segment_curve(
            points, circle_tangents, index, min_step, max_steps, dense, closed
        ):
            samples.append((x, y, width))
    return samples


def _segment_range(count: int, closed: bool) -> range:
    return range(count) if closed else range(count - 1)


def nearest_curve_point(
    stroke: Stroke, x: float, y: float, steps: int = 24
) -> tuple[int, float, float, float, float]:
    """Nearest point on the drawn curve: ``(segment_index, t, distance, x, y)``."""
    points = stroke.points
    if len(points) < 2:
        return -1, 0.0, float("inf"), 0.0, 0.0
    closed = stroke.closed and len(points) >= 3
    circle_tangents = _circle_tangents(points, closed)
    best_index = -1
    best_t = 0.0
    best_distance = float("inf")
    best_x = 0.0
    best_y = 0.0
    for index in _segment_range(len(points), closed):
        for t, px, py, _ in _segment_curve(
            points, circle_tangents, index, 0.5, max(64, steps * 8), dense=True, closed=closed
        ):
            distance = math.hypot(px - x, py - y)
            if distance < best_distance:
                best_index = index
                best_t = t
                best_distance = distance
                best_x = px
                best_y = py
    return best_index, best_t, best_distance, best_x, best_y


def nearest_point(stroke: Stroke, x: float, y: float) -> tuple[int, float]:
    best_index = -1
    best_distance = float("inf")
    for index, point in enumerate(stroke.points):
        distance = math.hypot(point.x - x, point.y - y)
        if distance < best_distance:
            best_index = index
            best_distance = distance
    return best_index, best_distance


def nearest_segment(stroke: Stroke, x: float, y: float) -> tuple[int, float]:
    """Return the index of the segment's first point and the distance to it."""
    count = len(stroke.points)
    closed = stroke.closed and count >= 3
    best_index = -1
    best_distance = float("inf")
    for index in _segment_range(count, closed):
        a = stroke.points[index]
        b = stroke.points[(index + 1) % count]
        distance = _point_segment_distance(x, y, a.x, a.y, b.x, b.y)
        if distance < best_distance:
            best_index = index
            best_distance = distance
    return best_index, best_distance


def _point_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    dx = x2 - x1
    dy = y2 - y1
    length_squared = dx * dx + dy * dy
    if length_squared < 1e-9:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_squared))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
