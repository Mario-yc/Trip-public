from __future__ import annotations

import math
from typing import Iterable, Sequence


Point = tuple[float, float]


class SpatialGeometryService:
    """Small, dependency-free geometry primitives for grounded boundaries.

    The service never closes gaps or constructs hulls.  A polygon is accepted
    only when the Provider geometry already contains a real closed cycle.
    """

    EARTH_RADIUS_METERS = 6_371_008.8

    @staticmethod
    def point_key(point: Point) -> tuple[float, float]:
        return (round(float(point[0]), 7), round(float(point[1]), 7))

    @classmethod
    def closed_components(cls, ways: Iterable[Sequence[Point]]) -> list[list[Point]]:
        sequences = [cls._clean_way(way) for way in ways]
        sequences = [way for way in sequences if len(way) >= 2]
        if not sequences:
            return []

        endpoint_to_ways: dict[tuple[float, float], list[int]] = {}
        for index, way in enumerate(sequences):
            endpoint_to_ways.setdefault(cls.point_key(way[0]), []).append(index)
            endpoint_to_ways.setdefault(cls.point_key(way[-1]), []).append(index)

        unseen = set(range(len(sequences)))
        accepted: list[list[Point]] = []
        while unseen:
            seed = unseen.pop()
            component = {seed}
            stack = [seed]
            while stack:
                current = stack.pop()
                way = sequences[current]
                for endpoint in (cls.point_key(way[0]), cls.point_key(way[-1])):
                    for neighbour in endpoint_to_ways.get(endpoint, []):
                        if neighbour in unseen:
                            unseen.remove(neighbour)
                            component.add(neighbour)
                            stack.append(neighbour)
            ring = cls._assemble_cycle([sequences[index] for index in sorted(component)])
            if ring is not None and cls.valid_closed_polygon(ring):
                accepted.append(ring)
        return accepted

    @classmethod
    def _assemble_cycle(cls, ways: list[list[Point]]) -> list[Point] | None:
        degrees: dict[tuple[float, float], int] = {}
        for way in ways:
            start, end = cls.point_key(way[0]), cls.point_key(way[-1])
            if start == end and len(ways) == 1:
                return way
            degrees[start] = degrees.get(start, 0) + 1
            degrees[end] = degrees.get(end, 0) + 1
        if not degrees or any(value != 2 for value in degrees.values()):
            return None

        remaining = list(ways)
        ring = remaining.pop(0)
        while remaining:
            endpoint = cls.point_key(ring[-1])
            match_index = next(
                (
                    index
                    for index, way in enumerate(remaining)
                    if endpoint in {cls.point_key(way[0]), cls.point_key(way[-1])}
                ),
                None,
            )
            if match_index is None:
                return None
            match = remaining.pop(match_index)
            if cls.point_key(match[-1]) == endpoint:
                match = list(reversed(match))
            ring.extend(match[1:])
        return ring if cls.point_key(ring[0]) == cls.point_key(ring[-1]) else None

    @classmethod
    def valid_closed_polygon(cls, polygon: Sequence[Point]) -> bool:
        if len(polygon) < 4 or cls.point_key(polygon[0]) != cls.point_key(polygon[-1]):
            return False
        if any(not math.isfinite(value) for point in polygon for value in point):
            return False
        if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in polygon):
            return False
        if abs(cls._signed_area(polygon)) < 1e-12:
            return False
        return not cls._has_self_intersection(polygon)

    @classmethod
    def simplify_closed_polygon(
        cls,
        polygon: Sequence[Point],
        *,
        max_vertices: int,
        max_deviation_meters: float,
    ) -> tuple[list[Point], float] | None:
        if not cls.valid_closed_polygon(polygon) or max_vertices < 4 or max_deviation_meters <= 0:
            return None
        source = list(polygon[:-1])
        if len(source) + 1 <= max_vertices:
            return [*source, source[0]], 0.0

        low, high = 0.0, float(max_deviation_meters)
        best: list[Point] | None = None
        for _ in range(28):
            tolerance = (low + high) / 2
            candidate = cls._simplify_ring(source, tolerance)
            closed = [*candidate, candidate[0]] if candidate else []
            if len(closed) <= max_vertices and cls.valid_closed_polygon(closed):
                best, high = closed, tolerance
            else:
                low = tolerance
        if best is None:
            candidate = cls._simplify_ring(source, float(max_deviation_meters))
            closed = [*candidate, candidate[0]] if candidate else []
            if len(closed) <= max_vertices and cls.valid_closed_polygon(closed):
                best = closed
        if best is None:
            return None
        measured_deviation = cls.max_simplification_deviation_meters(polygon, best)
        if measured_deviation > max_deviation_meters:
            return None
        return best, measured_deviation

    @classmethod
    def max_simplification_deviation_meters(
        cls,
        source_polygon: Sequence[Point],
        simplified_polygon: Sequence[Point],
    ) -> float:
        """Measure source-to-simplified boundary error instead of reporting DP tolerance."""

        if not cls.valid_closed_polygon(source_polygon) or not cls.valid_closed_polygon(
            simplified_polygon
        ):
            return math.inf
        simplified_segments = [
            (simplified_polygon[index], simplified_polygon[index + 1])
            for index in range(len(simplified_polygon) - 1)
        ]
        return max(
            (
                min(
                    cls._point_segment_distance_meters(point, start, end)
                    for start, end in simplified_segments
                )
                for point in source_polygon[:-1]
            ),
            default=0.0,
        )

    @classmethod
    def _simplify_ring(cls, points: list[Point], tolerance_meters: float) -> list[Point]:
        if len(points) <= 3:
            return points
        first = min(range(len(points)), key=lambda index: (points[index][0], points[index][1]))
        rotated = points[first:] + points[:first]
        split = max(
            range(1, len(rotated)),
            key=lambda index: cls.distance_meters(rotated[0], rotated[index]),
        )
        left = cls._douglas_peucker(rotated[: split + 1], tolerance_meters)
        right = cls._douglas_peucker(rotated[split:] + [rotated[0]], tolerance_meters)
        merged = left[:-1] + right[:-1]
        return merged if len(merged) >= 3 else rotated[:3]

    @classmethod
    def _douglas_peucker(cls, points: list[Point], tolerance_meters: float) -> list[Point]:
        if len(points) <= 2:
            return points
        start, end = points[0], points[-1]
        distances = [cls._point_segment_distance_meters(point, start, end) for point in points[1:-1]]
        maximum = max(distances, default=0.0)
        if maximum <= tolerance_meters:
            return [start, end]
        index = distances.index(maximum) + 1
        return cls._douglas_peucker(points[: index + 1], tolerance_meters)[:-1] + cls._douglas_peucker(
            points[index:], tolerance_meters
        )

    @classmethod
    def point_in_polygon(cls, point: Point, polygon: Sequence[Point]) -> bool:
        if not cls.valid_closed_polygon(polygon):
            return False
        x, y = point
        inside = False
        for index in range(len(polygon) - 1):
            x1, y1 = polygon[index]
            x2, y2 = polygon[index + 1]
            if cls._point_on_segment(point, (x1, y1), (x2, y2)):
                return True
            if (y1 > y) != (y2 > y):
                intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
                if x < intersection_x:
                    inside = not inside
        return inside

    @classmethod
    def distance_meters(cls, left: Point, right: Point) -> float:
        lon1, lat1 = map(math.radians, left)
        lon2, lat2 = map(math.radians, right)
        dlon, dlat = lon2 - lon1, lat2 - lat1
        value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return cls.EARTH_RADIUS_METERS * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))

    @staticmethod
    def _clean_way(way: Sequence[Point]) -> list[Point]:
        result: list[Point] = []
        for raw in way:
            point = (float(raw[0]), float(raw[1]))
            if not result or point != result[-1]:
                result.append(point)
        return result

    @staticmethod
    def _signed_area(polygon: Sequence[Point]) -> float:
        return sum(
            polygon[index][0] * polygon[index + 1][1] - polygon[index + 1][0] * polygon[index][1]
            for index in range(len(polygon) - 1)
        ) / 2

    @classmethod
    def _has_self_intersection(cls, polygon: Sequence[Point]) -> bool:
        segments = [(polygon[index], polygon[index + 1]) for index in range(len(polygon) - 1)]
        for left_index, left in enumerate(segments):
            for right_index in range(left_index + 1, len(segments)):
                if abs(left_index - right_index) <= 1 or {left_index, right_index} == {0, len(segments) - 1}:
                    continue
                if cls._segments_intersect(left[0], left[1], segments[right_index][0], segments[right_index][1]):
                    return True
        return False

    @classmethod
    def _segments_intersect(cls, a: Point, b: Point, c: Point, d: Point) -> bool:
        def orientation(p: Point, q: Point, r: Point) -> float:
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

        o1, o2, o3, o4 = orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)
        epsilon = 1e-12
        if (o1 > epsilon) != (o2 > epsilon) and (o3 > epsilon) != (o4 > epsilon):
            return True
        return (
            (abs(o1) <= epsilon and cls._point_on_segment(c, a, b))
            or (abs(o2) <= epsilon and cls._point_on_segment(d, a, b))
            or (abs(o3) <= epsilon and cls._point_on_segment(a, c, d))
            or (abs(o4) <= epsilon and cls._point_on_segment(b, c, d))
        )

    @staticmethod
    def _point_on_segment(point: Point, start: Point, end: Point) -> bool:
        cross = (point[0] - start[0]) * (end[1] - start[1]) - (point[1] - start[1]) * (
            end[0] - start[0]
        )
        return abs(cross) <= 1e-10 and min(start[0], end[0]) <= point[0] <= max(
            start[0], end[0]
        ) and min(start[1], end[1]) <= point[1] <= max(start[1], end[1])

    @classmethod
    def _point_segment_distance_meters(cls, point: Point, start: Point, end: Point) -> float:
        mean_latitude = math.radians((point[1] + start[1] + end[1]) / 3)
        scale_x = math.cos(mean_latitude) * 111_320.0
        scale_y = 110_540.0
        px, py = point[0] * scale_x, point[1] * scale_y
        ax, ay = start[0] * scale_x, start[1] * scale_y
        bx, by = end[0] * scale_x, end[1] * scale_y
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        ratio = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + ratio * dx), py - (ay + ratio * dy))
