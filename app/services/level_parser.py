"""编排关卡识别、显式起终点校验与权威产物发布，不判断可玩性。"""
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from pydantic import ValidationError

from app.level_contracts import (ALGORITHM_VERSION, DEFAULT_PLAYABILITY_PROFILE,
                                 Level, PlayabilityProfile, PlayabilityAnalysis, SCHEMA_VERSION,
                                 LEVEL_ERROR_MESSAGES)
from app.services import level_detect, level_rectify, level_semantic

logger = logging.getLogger(__name__)

# 合成集和黄金样本的候选置信度均高于 0.65；接入真实拍照集后需重标定。
MIN_PUBLISH_CONFIDENCE = .65
# 合成集/黄金样本的中心线像素覆盖均高于 0.55；笔迹材质或线宽变化时需重标定。
MIN_INK_COVERAGE = .55
# 80% 水平重叠且中心高度 4px 内视为重复检测；真实手绘平行近线加入后需重标定。
DUPLICATE_OVERLAP = .80
DUPLICATE_Y_DISTANCE = 4
Progress = Optional[Callable[[str], None]]

REVIEW_MESSAGES = {
    'PAPER_NOT_FOUND': '未检测到完整纸张，请重拍。',
    'PAPER_AMBIGUOUS': '检测到多个纸张候选，请只拍一张纸。',
    'PAPER_OCCLUDED': '纸张边缘被遮挡或不完整，请重拍。',
    'ORIENTATION_AMBIGUOUS': '纸张方向证据不足，请横向重拍。',
    'NO_PLATFORM_DETECTED': '未检测到可靠平台。',
    'PLATFORM_GEOMETRY_AMBIGUOUS': '平台几何存在重复或缺少墨迹证据。',
    'GOAL_NOT_FOUND': '未检测到终点旗帜。',
    'AMBIGUOUS_GOAL': '检测到多个接近的终点候选。',
    'START_PLATFORM_NOT_FOUND': '找不到可安全承载角色的出生平台。',
    'LOW_CONFIDENCE': '候选置信度不足，不能安全发布。',
}
# request.json 缺失或时间戳不可用时的占位值：保证终态契约的 str 字段拿到的是字符串
EPOCH_CREATED = '1970-01-01T00:00:00Z'


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def _atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(content)
    temporary.replace(path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, _canonical_bytes(value))


def _stage(progress: Progress, name: str) -> None:
    if progress is not None:
        progress(name)


def _artifact_url(job_id: str, name: str) -> str:
    return '/artifacts/{}/{}'.format(job_id, name)


def _request(job_dir: Path) -> Tuple[PlayabilityProfile, str, str]:
    request_path = job_dir / 'request.json'
    request = json.loads(request_path.read_text(encoding='utf-8')) if request_path.exists() else {}
    profile = PlayabilityProfile.model_validate(
        request.get('playabilityProfile', DEFAULT_PLAYABILITY_PROFILE.model_dump()))
    created = request.get('createdAt') or EPOCH_CREATED
    # 用 or 而非 .get(key, default)：历史上写入过显式 null 的 request.json 仍在磁盘上，
    # 把 None 透到终态 payload 会直接撞在 str 校验上，让整条任务被归为 PROCESSING_CRASHED。
    return profile, created, (request.get('updatedAt') or created)


def _candidate_region(candidate: Dict[str, Any], width: int, height: int) -> Dict[str, Any]:
    raw = candidate.get('region', candidate)
    x = max(0, min(int(raw.get('x', 0)), width - 1))
    y = max(0, min(int(raw.get('y', 0)), height - 1))
    return {'x': x, 'y': y,
            'width': max(1, min(int(raw.get('width', 1)), width - x)),
            'height': max(1, min(int(raw.get('height', 1)), height - y)),
            'confidence': float(candidate.get('confidence', raw.get('confidence', 0)))}


def _review_payload(job_dir: Path, reason: str, candidates: Sequence[Any],
                    width: int, height: int, created: str, updated: str) -> Dict[str, Any]:
    job_id = job_dir.name
    # 复核是业务终态而非故障，但“为何没解析出来”全靠这条日志定位
    logger.info('关卡任务 %s 判定为需人工复核：%s（%s），候选 %d 个，画布 %dx%d',
                job_id, reason, REVIEW_MESSAGES[reason], len(candidates), width, height)
    items = []
    for index, candidate in enumerate(candidates):
        raw = candidate if isinstance(candidate, dict) else {}
        region_source = getattr(candidate, 'region', raw)
        if hasattr(region_source, '__dict__'):
            region_source = vars(region_source)
        confidence = getattr(candidate, 'confidence', raw.get('confidence', 0))
        region = _candidate_region({**dict(region_source), 'confidence': confidence}, width, height)
        items.append({'id': getattr(candidate, 'id', raw.get('id', '{}_{:03d}'.format(reason.lower(), index + 1))),
                      'type': 'goal' if reason in ('GOAL_NOT_FOUND', 'AMBIGUOUS_GOAL') else 'geometry',
                      'region': region, 'confidence': confidence})
    if not items:
        items = [{'id': reason.lower(), 'type': 'geometry',
                  'region': {'x': 0, 'y': 0, 'width': max(width, 1),
                             'height': max(height, 1), 'confidence': 0},
                  'confidence': 0}]
    artifacts = {'rectifiedImageUrl': _artifact_url(job_id, 'rectified.png'),
                 'overlayImageUrl': _artifact_url(job_id, 'overlay.png')}
    return {'jobId': job_id, 'schemaVersion': SCHEMA_VERSION,
            'algorithmVersion': ALGORITHM_VERSION, 'createdAt': created, 'updatedAt': updated,
            'status': 'needs_review',
            'review': {'reason': reason, 'message': REVIEW_MESSAGES[reason],
                       'candidates': items, 'suggestions': ['请依据叠加图重拍或人工复核。']},
            'artifacts': artifacts}


def _coverage(mask: np.ndarray, platform: Any) -> float:
    points = max(abs(platform.end.x - platform.start.x),
                 abs(platform.end.y - platform.start.y)) + 1
    xs = np.rint(np.linspace(platform.start.x, platform.end.x, points)).astype(int)
    ys = np.rint(np.linspace(platform.start.y, platform.end.y, points)).astype(int)
    valid = (xs >= 0) & (xs < mask.shape[1]) & (ys >= 0) & (ys < mask.shape[0])
    if not valid.any():
        return 0.0
    return float(np.count_nonzero(mask[ys[valid], xs[valid]])) / int(valid.sum())


def _duplicate(platforms: Sequence[Any]) -> bool:
    for index, left in enumerate(platforms):
        for right in platforms[index + 1:]:
            overlap = min(left.end.x, right.end.x) - max(left.start.x, right.start.x) + 1
            shortest = min(left.end.x - left.start.x + 1, right.end.x - right.start.x + 1)
            left_y = (left.start.y + left.end.y) / 2
            right_y = (right.start.y + right.end.y) / 2
            if overlap > 0 and overlap / shortest >= DUPLICATE_OVERLAP and abs(left_y - right_y) <= DUPLICATE_Y_DISTANCE:
                return True
    return False


def _platforms(candidates: Sequence[Any]) -> List[Dict[str, Any]]:
    ordered = sorted(candidates, key=lambda p: (
        min(p.start.x, p.end.x), min(p.start.y, p.end.y),
        max(p.start.x, p.end.x), max(p.start.y, p.end.y), p.id))
    result = []
    for index, candidate in enumerate(ordered, 1):
        start, end = candidate.start, candidate.end
        if start.x > end.x:
            start, end = end, start
        result.append({'id': 'platform_{:03d}'.format(index),
                       'start': {'x': start.x, 'y': start.y},
                       'end': {'x': end.x, 'y': end.y},
                       'confidence': candidate.confidence})
    return result


def _goal_region(goal: Any) -> Dict[str, Any]:
    region = goal.region
    return {'x': region.x, 'y': region.y, 'width': region.width,
            'height': region.height, 'confidence': goal.confidence}


def _overlay(path: Path, level: Level, analysis: Any) -> None:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('无法读取拉正图：{}'.format(path))
    for platform in level.platforms:
        color = (0, 180, 0) if platform.id in analysis.path else (255, 120, 0)
        cv2.line(image, (platform.start.x, platform.start.y),
                 (platform.end.x, platform.end.y), color, 1, cv2.LINE_AA)
        cv2.putText(image, platform.id, (platform.start.x, max(10, platform.start.y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, .3, color, 1, cv2.LINE_AA)
    cv2.circle(image, (level.playerStart.x, level.playerStart.y), 3, (255, 0, 255), 1)
    goal = level.goalRegion
    cv2.rectangle(image, (goal.x, goal.y),
                  (goal.x + goal.width - 1, goal.y + goal.height - 1), (0, 0, 255), 1)
    temporary = path.parent / 'overlay.png.tmp.png'
    if not cv2.imwrite(str(temporary), image):
        raise OSError('无法写入识别叠加图：{}'.format(temporary))
    temporary.replace(path.parent / 'overlay.png')


def _publish_review_artifacts(job_dir: Path, reason: str, candidates: Sequence[Any],
                              rectified_path: Path, progress: Progress) -> None:
    level_path = job_dir / 'level.json'
    if level_path.exists():
        level_path.unlink()
    image = cv2.imread(str(rectified_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('无法读取拉正图：{}'.format(rectified_path))
    for candidate in candidates:
        if hasattr(candidate, 'region'):
            region = candidate.region
            cv2.rectangle(image, (region.x, region.y),
                          (region.x + region.width - 1, region.y + region.height - 1),
                          (0, 0, 255), 1)
            cv2.putText(image, candidate.id, (region.x, max(10, region.y - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, .3, (0, 0, 255), 1)
        elif hasattr(candidate, 'start'):
            cv2.line(image, (candidate.start.x, candidate.start.y),
                     (candidate.end.x, candidate.end.y), (255, 120, 0), 1)
    temporary = job_dir / 'overlay.png.tmp.png'
    if not cv2.imwrite(str(temporary), image):
        raise OSError('无法写入复核叠加图：{}'.format(temporary))
    temporary.replace(job_dir / 'overlay.png')
    analysis = {'algorithmVersion': ALGORITHM_VERSION, 'reviewReason': reason}
    _stage(progress, 'publishing_artifacts')
    _atomic_json(job_dir / 'analysis.json', analysis)


def _pre_review(semantic: Any) -> Optional[str]:
    if semantic.review_reasons:
        return 'AMBIGUOUS_GOAL' if 'AMBIGUOUS_GOAL' in semantic.review_reasons else 'LOW_CONFIDENCE'
    if not semantic.platforms:
        return 'NO_PLATFORM_DETECTED'
    if not semantic.goals:
        return 'GOAL_NOT_FOUND'
    if len(semantic.goals) > 1:
        scores = sorted((goal.confidence for goal in semantic.goals), reverse=True)
        if scores[0] - scores[1] <= .05:
            return 'AMBIGUOUS_GOAL'
    if min([p.confidence for p in semantic.platforms] + [g.confidence for g in semantic.goals]) < MIN_PUBLISH_CONFIDENCE:
        return 'LOW_CONFIDENCE'
    return None


def parse(job_dir: Path, progress: Progress = None,
          semantic_client: Optional[level_semantic.SemanticClient] = None) -> Dict[str, Any]:
    """顺序执行共享模块；歧义只发布复核载荷，可靠几何才发布 level.json。"""
    job_dir = Path(job_dir).resolve()
    job_id = job_dir.name
    profile, created, updated = _request(job_dir)
    logger.info('关卡任务 %s 开始解析：能力配置 %s，输入 %s', job_id,
                profile.profileVersion, job_dir / 'input.png')
    _stage(progress, 'validating_upload')
    input_path = job_dir / 'input.png'
    if not input_path.exists():
        raise FileNotFoundError('关卡任务 {} 缺少上传原图：{}'.format(job_id, input_path))
    _stage(progress, 'rectifying_paper')
    try:
        rectified = level_rectify.rectify(input_path, job_dir)
    except level_rectify.RectifyIssue as issue:
        logger.warning('关卡任务 %s 纸张拉正失败：%s（%s）', job_id, issue.reason, issue)
        review_image = job_dir / 'rectified.png'
        if not review_image.exists():
            _atomic_bytes(review_image, input_path.read_bytes())
        _publish_review_artifacts(job_dir, issue.reason, (), review_image, progress)
        image = cv2.imread(str(review_image), cv2.IMREAD_COLOR)
        height, width = image.shape[:2]
        return _review_payload(job_dir, issue.reason, issue.candidates,
                               width, height, created, updated)
    logger.info('关卡任务 %s 纸张拉正完成：画布 %dx%d', job_id, rectified.width, rectified.height)
    _stage(progress, 'detecting_platforms')
    detection = level_detect.detect(rectified.rectified_path, job_dir)
    _stage(progress, 'detecting_goal')
    _stage(progress, 'semantic_review')
    semantic = level_semantic.review(rectified.rectified_path, detection,
                                     client=semantic_client)
    logger.info('关卡任务 %s 语义复核完成：来源 %s，保留平台 %d 条、终点 %d 个，复核标记 %s',
                job_id, semantic.source, len(semantic.platforms), len(semantic.goals),
                '、'.join(semantic.review_reasons) or '无')
    starts = detection.start_candidates
    marker_error = None
    if not starts and not semantic.goals:
        marker_error = 'START_AND_GOAL_NOT_FOUND'
    elif not starts:
        marker_error = 'START_NOT_FOUND'
    elif not semantic.goals:
        marker_error = 'GOAL_NOT_FOUND'
    elif len(starts) != 1:
        marker_error = 'AMBIGUOUS_START'
    elif len(semantic.goals) != 1 or 'AMBIGUOUS_GOAL' in semantic.review_reasons:
        marker_error = 'AMBIGUOUS_GOAL'
    if marker_error:
        logger.info('关卡任务 %s 生成失败：%s，起点 %d 个，终点 %d 个',
                    job_id, marker_error, len(starts), len(semantic.goals))
        _publish_review_artifacts(job_dir, marker_error, semantic.goals,
                                  rectified.rectified_path, progress)
        return {'jobId': job_id, 'status': 'failed', 'createdAt': created, 'updatedAt': updated,
                'error': {'code': marker_error, 'message': LEVEL_ERROR_MESSAGES[marker_error],
                          'retryable': False, 'requestId': 'req_' + job_id}}
    reason = _pre_review(semantic)
    if reason:
        candidates = semantic.goals if reason in ('GOAL_NOT_FOUND', 'AMBIGUOUS_GOAL') else semantic.platforms
        _publish_review_artifacts(job_dir, reason, candidates, rectified.rectified_path, progress)
        return _review_payload(job_dir, reason, candidates, rectified.width, rectified.height, created, updated)
    _stage(progress, 'validating_geometry')
    ink = cv2.imread(str(detection.ink_mask_path), cv2.IMREAD_GRAYSCALE)
    # 拆开算一遍才能把“到底卡在哪个门禁”写进日志；短路顺序与原判定一致
    weak = [platform.id for platform in semantic.platforms
            if ink is None or _coverage(ink, platform) < MIN_INK_COVERAGE]
    duplicated = _duplicate(semantic.platforms)
    if ink is None or weak or duplicated:
        logger.warning('关卡任务 %s 几何门禁未通过：墨迹证据不足的平台 %s，存在重复平台 %s，墨迹遮罩可读 %s',
                       job_id, '、'.join(weak) or '无', duplicated, ink is not None)
        _publish_review_artifacts(job_dir, 'PLATFORM_GEOMETRY_AMBIGUOUS', semantic.platforms,
                                  rectified.rectified_path, progress)
        return _review_payload(job_dir, 'PLATFORM_GEOMETRY_AMBIGUOUS', semantic.platforms,
                               rectified.width, rectified.height, created, updated)
    start = {'x': starts[0].x, 'y': starts[0].y, 'source': 'detected',
             'confidence': starts[0].confidence}

    background_url = _artifact_url(job_id, 'rectified.png')
    level_data = {'schemaVersion': SCHEMA_VERSION,
                  'coordinateSystem': {'origin': 'top_left', 'xAxis': 'right',
                                       'yAxis': 'down', 'unit': 'pixel'},
                  'canvas': {'width': rectified.width, 'height': rectified.height},
                  'background': {'imageUrl': background_url, 'contentType': 'image/png',
                                 'width': rectified.width, 'height': rectified.height,
                                 'sha256': hashlib.sha256(rectified.rectified_path.read_bytes()).hexdigest()},
                  'playerStart': start, 'platforms': _platforms(semantic.platforms),
                  'goalRegion': _goal_region(max(semantic.goals, key=lambda goal: (goal.confidence, goal.id)))}
    try:
        level = Level.model_validate(level_data)
    except ValidationError as exc:
        # 契约校验失败原本被静默吞掉，只能看到“转复核”，排查时无从下手
        logger.warning('关卡任务 %s 几何不符合契约，转人工复核：%s', job_id, exc)
        _publish_review_artifacts(job_dir, 'PLATFORM_GEOMETRY_AMBIGUOUS', semantic.platforms,
                                  rectified.rectified_path, progress)
        return _review_payload(job_dir, 'PLATFORM_GEOMETRY_AMBIGUOUS', semantic.platforms,
                               rectified.width, rectified.height, created, updated)
    analysis = PlayabilityAnalysis(playability='not_checked', profile=profile)
    _stage(progress, 'publishing_artifacts')
    _overlay(rectified.rectified_path, level, analysis)
    _atomic_json(job_dir / 'level.json', level.model_dump())
    _atomic_json(job_dir / 'analysis.json', analysis.model_dump())
    artifacts = {'inputUrl': _artifact_url(job_id, 'input.png'),
                 'rectifiedImageUrl': background_url,
                 'overlayImageUrl': _artifact_url(job_id, 'overlay.png'),
                 'levelJsonUrl': _artifact_url(job_id, 'level.json'),
                 'analysisJsonUrl': _artifact_url(job_id, 'analysis.json')}
    status = 'ready'
    logger.info('关卡任务 %s 解析完成：%s，%d 块平台，起点和终点已识别，跳过可玩性判断',
                job_id, status, len(level.platforms))
    return {'jobId': job_id, 'schemaVersion': SCHEMA_VERSION,
            'algorithmVersion': ALGORITHM_VERSION, 'createdAt': created, 'updatedAt': updated,
            'status': status,
            'result': {'level': level.model_dump(), 'analysis': analysis.model_dump(),
                       'artifacts': artifacts}}
