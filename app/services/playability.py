"""在像素空间诊断关卡可玩性，不修改关卡几何。"""

import math
from collections import deque
from typing import Dict, List, Optional, Tuple

from app.level_contracts import Level, Platform, PlayabilityAnalysis, PlayabilityProfile, Warning


def _slope_degrees(platform: Platform) -> float:
    """平台相对水平线的倾角（度，y 轴向下为正）。端点存反时归一化为从左到右。"""
    dx = platform.end.x - platform.start.x
    dy = platform.end.y - platform.start.y
    if dx < 0:
        dx, dy = -dx, -dy
    if dx == 0:
        return 90.0 if dy > 0 else -90.0
    return math.degrees(math.atan2(dy, dx))


def _span(platform: Platform) -> Tuple[int, int]:
    return platform.start.x, platform.end.x


def _y_at(platform: Platform, x: int) -> float:
    width = platform.end.x - platform.start.x
    if width == 0:
        return float(platform.start.y)
    ratio = (x - platform.start.x) / width
    return platform.start.y + ratio * (platform.end.y - platform.start.y)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _horizontal_gap(a: Platform, b: Platform) -> int:
    if a.end.x < b.start.x:
        return b.start.x - a.end.x
    if b.end.x < a.start.x:
        return a.start.x - b.end.x
    return 0


def _landing_points(source: Platform, target: Platform) -> Tuple[int, int]:
    if source.end.x < target.start.x:
        return source.end.x, target.start.x
    if target.end.x < source.start.x:
        return source.start.x, target.end.x
    x = max(source.start.x, target.start.x)
    return x, x


def _warning(code: str, message: str, ids: List[str], required: Optional[int] = None,
             available: Optional[int] = None) -> Warning:
    return Warning(code=code, message=message, relatedPlatformIds=ids,
                   requiredValuePixels=required, availableValuePixels=available)


def _start_platform(level: Level, tolerance: int) -> Tuple[Optional[Platform], int, Platform]:
    candidates = []
    for platform in level.platforms:
        x = _clamp(level.playerStart.x, platform.start.x, platform.end.x)
        horizontal = abs(level.playerStart.x - x)
        vertical = abs(level.playerStart.y - _y_at(platform, x))
        candidates.append((max(horizontal, vertical), vertical, platform.id, platform))
    supported = [item for item in candidates if item[0] <= tolerance]
    chosen = min(supported or candidates, key=lambda item: (item[0], item[1], item[2]))
    return (chosen[3] if supported else None), int(round(chosen[0])), chosen[3]


def _goal_platform(level: Level, tolerance: int) -> Tuple[Optional[Platform], int, Platform]:
    left = level.goalRegion.x
    right = left + level.goalRegion.width
    bottom = level.goalRegion.y + level.goalRegion.height
    candidates = []
    for platform in level.platforms:
        overlap_left = max(left, platform.start.x)
        overlap_right = min(right, platform.end.x)
        if overlap_left <= overlap_right:
            endpoint_gaps = [bottom - _y_at(platform, x) for x in (overlap_left, overlap_right)]
            gap = 0.0 if endpoint_gaps[0] * endpoint_gaps[1] <= 0 else min(map(abs, endpoint_gaps))
            candidates.append((gap, platform.id, platform))
    if candidates:
        chosen = min(candidates, key=lambda item: (item[0], item[1]))
        return (chosen[2] if chosen[0] <= tolerance else None), int(round(chosen[0])), chosen[2]
    nearest = min(level.platforms, key=lambda platform: (
        min(abs(platform.start.x - right), abs(platform.end.x - left)), platform.id))
    gap = min(abs(nearest.start.x - right), abs(nearest.end.x - left))
    return None, gap, nearest


def analyze(level: Level, profile: PlayabilityProfile) -> PlayabilityAnalysis:
    """返回稳定的可玩性诊断；输入 Level 始终保持原样。"""
    platforms = sorted(level.platforms, key=lambda platform: platform.id)
    tolerance = profile.landingTolerancePixels
    warnings = []  # type: List[Warning]

    start, start_gap, nearest_start = _start_platform(level, tolerance)
    if start is None:
        warnings.append(_warning('START_NOT_SUPPORTED', '出生点未被平台承载',
                                 [nearest_start.id], start_gap, tolerance))

    # 超过可攀爬坡度的平台仍然是合法几何（角色可以落在上面并沿坡滑下），
    # 所以只提示、不从可达图里剔除，避免把「滑梯」误判成不可玩。
    for platform in platforms:
        slope = abs(_slope_degrees(platform))
        if slope > profile.maxClimbableSlopeDegrees:
            warnings.append(_warning(
                'SLOPE_TOO_STEEP',
                '{0} 倾角 {1:.0f}° 超过可攀爬上限 {2:.0f}°，角色会沿坡滑下'.format(
                    platform.id, slope, profile.maxClimbableSlopeDegrees),
                [platform.id]))

    goal, goal_gap, nearest_goal = _goal_platform(level, tolerance)
    if goal is None:
        if any(platform.start.x <= level.goalRegion.x + level.goalRegion.width and
               platform.end.x >= level.goalRegion.x for platform in platforms):
            warnings.append(_warning('GOAL_NOT_SUPPORTED', '终点区域底边未被平台承载',
                                     [nearest_goal.id], goal_gap, tolerance))
        else:
            warnings.append(_warning('GOAL_NOT_SUPPORTED', '终点区域与平台无水平投影重叠',
                                     [nearest_goal.id]))

    # ponytail: 这里只使用轴向能力包络，不模拟抛物线与障碍碰撞；若真实物理误判，升级为共享 Unity 轨迹采样器，仍不得调整平台。
    graph = {platform.id: [] for platform in platforms}  # type: Dict[str, List[str]]
    for source in platforms:
        for target in platforms:
            if source.id == target.id:
                continue
            ids = [source.id, target.id]
            gap = _horizontal_gap(source, target)
            available_distance = profile.maxJumpDistancePixels + tolerance
            if gap > available_distance:
                warnings.append(_warning('JUMP_GAP_TOO_WIDE', '平台间水平距离超过角色能力', ids,
                                         gap, available_distance))
                continue
            reachable_left = max(target.start.x, source.start.x - available_distance)
            reachable_right = min(target.end.x, source.end.x + available_distance)
            landing_length = max(0, reachable_right - reachable_left + 1)
            if landing_length < profile.characterWidthPixels:
                warning_ids = ids if landing_length < target.end.x - target.start.x + 1 else [target.id]
                warnings.append(_warning('LANDING_AREA_TOO_SHORT', '目标平台可达着陆区域过短', warning_ids,
                                         profile.characterWidthPixels, landing_length))
                continue
            source_x, target_x = _landing_points(source, target)
            rise = int(round(_y_at(source, source_x) - _y_at(target, target_x)))
            if rise > profile.maxJumpRisePixels:
                warnings.append(_warning('JUMP_GAP_TOO_HIGH', '平台间上升高度超过角色能力', ids,
                                         rise, profile.maxJumpRisePixels))
                continue
            graph[source.id].append(target.id)

    path = []  # type: List[str]
    if start is not None and goal is not None:
        queue = deque([(start.id, [start.id])])
        visited = {start.id}
        while queue:
            current, current_path = queue.popleft()
            if current == goal.id:
                path = current_path
                break
            for neighbor in sorted(graph[current]):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, current_path + [neighbor]))
        if not path:
            warnings.append(_warning('NO_PATH_TO_GOAL', '出生平台到终点平台不存在可达路径',
                                     [start.id, goal.id]))

    unique = {}
    for warning in warnings:
        key = (warning.code, tuple(warning.relatedPlatformIds), warning.requiredValuePixels,
               warning.availableValuePixels)
        unique[key] = warning
    stable_warnings = [unique[key] for key in sorted(unique)]
    return PlayabilityAnalysis(playability='playable' if path else 'unreachable', profile=profile,
                               startPlatformId=start.id if start else None,
                               goalPlatformId=goal.id if goal else None,
                               path=path, warnings=stable_warnings)
