"""用可选 LLM 严格筛选 OpenCV 候选，失败时保留原始检测结果。"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Protocol, Union

import cv2
import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.level_detect import DetectionResult, GoalCandidate, PlatformCandidate

PROMPT_VERSION = 'level-semantic-16.3-v1'
PROMPT = """你是手绘横版平台关卡的候选分类器，不是关卡设计师。

输入是一张已经完成透视拉正的纸张图片。图片上标出了 OpenCV 产生的候选编号。
你的任务仅限于：
1. 判断每个 line candidate 是平台墨迹、纸张边缘、阴影、文字或其他内容；
2. 判断每个 goal candidate 是否为终点旗帜；
3. 指出是否存在多张纸、严重遮挡、方向不明确或无法判断的情况。

禁止行为：
- 不得创建候选列表中不存在的平台；
- 不得输出、移动、延长或修正任何平台坐标；
- 不得为了让关卡可玩而修改判断；
- 不确定时必须返回 needs_review，不得猜测。

只返回符合提供的 JSON Schema 的 JSON，不要输出解释性文字。"""


class SemanticClient(Protocol):
    def classify(self, image_data_uri: str, candidates: Dict[str, Any]) -> Any:
        ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class _LineClassification(_StrictModel):
    candidateId: str = Field(min_length=1)
    label: Literal['platform', 'paper_edge', 'shadow', 'text', 'other']
    confidence: float = Field(ge=0, le=1)


class _GoalClassification(_StrictModel):
    candidateId: str = Field(min_length=1)
    label: Literal['goal_flag', 'other']
    confidence: float = Field(ge=0, le=1)


class _SemanticResponse(_StrictModel):
    lineClassifications: List[_LineClassification]
    goalClassifications: List[_GoalClassification]
    sceneIssues: List[str]
    decision: Literal['accepted', 'needs_review']


@dataclass(frozen=True)
class SemanticResult:
    source: Literal['opencv', 'llm']
    platforms: List[PlatformCandidate]
    goals: List[GoalCandidate]
    degraded_reason: Optional[str] = None
    review_reasons: List[str] = field(default_factory=list)


class InvalidSemanticResponse(ValueError):
    """OpenAI 兼容响应不是可校验的结构化 JSON。"""


class RequestsSemanticClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        if not base_url.lower().startswith('https://'):
            raise ValueError('LEVEL_LLM_BASE_URL must use HTTPS')
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model

    def classify(self, image_data_uri: str, candidates: Dict[str, Any]) -> Any:
        response = requests.post(
            self.base_url + '/chat/completions',
            headers={'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json'},
            json={
                'model': self.model,
                'temperature': .1,
                'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': PROMPT + '\n\n候选 JSON：' + json.dumps(candidates, ensure_ascii=False)},
                    {'type': 'image_url', 'image_url': {'url': image_data_uri}},
                ]}],
                'response_format': {
                    'type': 'json_schema',
                    'json_schema': {'name': 'level_semantic_review', 'strict': True,
                                    'schema': _SemanticResponse.model_json_schema()},
                },
            },
            timeout=(5, 30),
        )
        response.raise_for_status()
        try:
            payload = response.json()
            return json.loads(payload['choices'][0]['message']['content'])
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
            raise InvalidSemanticResponse('invalid structured LLM response') from exc


def _candidate_payload(detection: DetectionResult) -> Dict[str, Any]:
    return {
        'lines': [
            {'candidateId': p.id, 'start': {'x': p.start.x, 'y': p.start.y},
             'end': {'x': p.end.x, 'y': p.end.y}, 'confidence': p.confidence}
            for p in detection.platform_candidates
        ],
        'goals': [
            {'candidateId': g.id,
             'region': {'x': g.region.x, 'y': g.region.y, 'width': g.region.width,
                        'height': g.region.height}, 'confidence': g.confidence}
            for g in detection.goal_candidates
        ],
    }


def _image_data_uri(path: Path, detection: DetectionResult) -> str:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return ''
    for platform in detection.platform_candidates:
        cv2.line(image, (platform.start.x, platform.start.y),
                 (platform.end.x, platform.end.y), (255, 0, 255), 2)
        cv2.putText(image, platform.id, (platform.start.x, max(12, platform.start.y - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 0, 255), 1, cv2.LINE_AA)
    for goal in detection.goal_candidates:
        region = goal.region
        cv2.rectangle(image, (region.x, region.y),
                      (region.x + region.width, region.y + region.height), (255, 128, 0), 2)
        cv2.putText(image, goal.id, (region.x, max(12, region.y - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 128, 0), 1, cv2.LINE_AA)
    height, width = image.shape[:2]
    scale = min(1.0, 1280.0 / max(height, width))
    if scale < 1:
        image = cv2.resize(image, (round(width * scale), round(height * scale)),
                           interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return ''
    return 'data:image/jpeg;base64,' + base64.b64encode(encoded.tobytes()).decode('ascii')


def _configured_client() -> Optional[RequestsSemanticClient]:
    values = [os.getenv(name) for name in
              ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL')]
    if not all(values):
        return None
    return RequestsSemanticClient(values[0], values[1], values[2])


def _write_audit(path: Path, client: SemanticClient, candidate_ids: List[str],
                 response: Any, degraded_reason: Optional[str]) -> None:
    try:
        model = getattr(client, 'model', client.__class__.__name__)
        payload = {'model': model, 'promptVersion': PROMPT_VERSION,
                   'requestCandidateIds': candidate_ids, 'response': response,
                   'degradedReason': degraded_reason}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        temporary.replace(path)
    except Exception:
        # 审计是旁路能力，序列化或磁盘故障不能改变关卡解析结果。
        return


def _opencv_result(detection: DetectionResult, reason: Optional[str] = None) -> SemanticResult:
    return SemanticResult('opencv', list(detection.platform_candidates),
                          list(detection.goal_candidates), reason)


def review(rectified_path: Path, detection: DetectionResult,
           client: Optional[SemanticClient] = None) -> SemanticResult:
    """复核候选 ID；模型的坐标字段和不存在的 ID 永远不会进入结果。"""
    if client is None:
        try:
            client = _configured_client()
        except Exception:
            return _opencv_result(detection, 'LLM_CONFIGURATION_INVALID')
    if client is None:
        return _opencv_result(detection)

    candidates = _candidate_payload(detection)
    candidate_ids = [p.id for p in detection.platform_candidates]
    candidate_ids += [g.id for g in detection.goal_candidates]
    audit_path = Path(rectified_path).parent / 'llm-audit.json'
    response: Any = None
    image_data_uri = _image_data_uri(Path(rectified_path), detection)
    if not image_data_uri:
        _write_audit(audit_path, client, candidate_ids, response, 'LLM_IMAGE_UNAVAILABLE')
        return _opencv_result(detection, 'LLM_IMAGE_UNAVAILABLE')
    try:
        response = client.classify(image_data_uri, candidates)
    except InvalidSemanticResponse:
        _write_audit(audit_path, client, candidate_ids, response, 'INVALID_LLM_RESPONSE')
        return _opencv_result(detection, 'INVALID_LLM_RESPONSE')
    except Exception:
        _write_audit(audit_path, client, candidate_ids, response, 'LLM_REQUEST_FAILED')
        return _opencv_result(detection, 'LLM_REQUEST_FAILED')

    try:
        parsed = _SemanticResponse.model_validate(response)
        line_ids = [item.candidateId for item in parsed.lineClassifications]
        goal_ids = [item.candidateId for item in parsed.goalClassifications]
        valid_line_ids = {p.id for p in detection.platform_candidates}
        valid_goal_ids = {g.id for g in detection.goal_candidates}
        if (set(line_ids) != valid_line_ids or len(line_ids) != len(set(line_ids)) or
                set(goal_ids) != valid_goal_ids or len(goal_ids) != len(set(goal_ids))):
            raise ValueError('response must classify every candidate exactly once')
    except (ValidationError, ValueError, TypeError):
        _write_audit(audit_path, client, candidate_ids, response, 'INVALID_LLM_RESPONSE')
        return _opencv_result(detection, 'INVALID_LLM_RESPONSE')

    selected_lines = {item.candidateId for item in parsed.lineClassifications
                      if item.label == 'platform'}
    selected_goals = {item.candidateId for item in parsed.goalClassifications
                      if item.label == 'goal_flag'}
    review_reasons = []
    if parsed.decision == 'needs_review':
        review_reasons.append('LLM_NEEDS_REVIEW')
    if parsed.sceneIssues:
        review_reasons.append('LLM_SCENE_ISSUE')
    goal_scores = sorted((item.confidence for item in parsed.goalClassifications
                          if item.label == 'goal_flag'), reverse=True)
    # 0.05 是协议计划用于识别双旗接近的差值；真实照片加入后需重新标定。
    if len(goal_scores) > 1 and goal_scores[0] - goal_scores[1] <= .05:
        review_reasons.append('AMBIGUOUS_GOAL')
    result = SemanticResult(
        'llm', [p for p in detection.platform_candidates if p.id in selected_lines],
        [g for g in detection.goal_candidates if g.id in selected_goals],
        review_reasons=review_reasons,
    )
    _write_audit(audit_path, client, candidate_ids, response, None)
    return result
