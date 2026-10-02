"""根据已计算的标题证据，确定性地选择媒体实体。"""


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def candidate_features(candidate_meta, tmdb_info, evidence, provider_reliability=0.5):
    """根据解析结果和 TMDB 元数据生成默认中性的比较特征。"""
    release_date = tmdb_info.get("release_date") or tmdb_info.get("first_air_date") or ""
    tmdb_year = str(release_date)[:4] if release_date else ""
    tmdb_type = getattr(tmdb_info.get("media_type"), "value", tmdb_info.get("media_type"))
    candidate_type = getattr(candidate_meta, "type", None)
    media_type = getattr(candidate_type, "value", candidate_type)
    parsed_year = str(getattr(candidate_meta, "year", "") or "")
    season = _optional_int(getattr(candidate_meta, "begin_season", None))
    season_count = _optional_int(tmdb_info.get("number_of_seasons"))
    try:
        reliability = min(1.0, max(0.0, float(provider_reliability)))
    except (TypeError, ValueError):
        reliability = 0.5

    if evidence.get("level") == "strong":
        title_match = 1.0
    elif evidence.get("level") == "fuzzy":
        try:
            title_match = float(evidence.get("fuzzy_score", 0.0))
        except (TypeError, ValueError):
            title_match = 0.0
    elif evidence.get("level") == "weak":
        title_match = 0.4
    else:
        title_match = 0.0

    return {
        "title_match": title_match,
        "year_match": (0.5 if not parsed_year or not tmdb_year else
                       1.0 if parsed_year == tmdb_year else 0.0),
        "type_match": (0.5 if not media_type or not tmdb_type else
                       1.0 if media_type == tmdb_type or
                       (media_type == "anime" and tmdb_type == "tv") else 0.0),
        "season_episode_match": (0.5 if season is None or season_count is None else
                                 1.0 if 0 <= season <= season_count else 0.0),
        "input_evidence": 1.0 if evidence.get("level") in ("strong", "fuzzy") else 0.5,
        "provider_reliability": reliability,
    }


def weighted_score(features, weights):
    weighted = {key: max(float(weights.get(key, 0) or 0), 0.0) for key in features}
    total = sum(weighted.values())
    if not total:
        return None
    return round(sum(features[key] * weighted[key] for key in features) / total, 4)


def _entity_key(tmdb_info):
    media_type = tmdb_info.get("media_type")
    media_type = getattr(media_type, "value", media_type)
    tmdb_id = tmdb_info.get("id")
    if media_type is None or tmdb_id is None or str(tmdb_id).strip() == "":
        return None
    return str(media_type), str(tmdb_id)


def select_title_evidence(candidates, agreement_bonus=0.0):
    """选择唯一的强匹配实体；若不存在强匹配，则选择唯一的模糊匹配实体。

    ``candidates`` 包含 ``((provider_id, parsed, tmdb_info), evidence)`` 形式的配对。
    来自多个识别器的同一 TMDB 实体证据会先去重；识别器顺序和诊断分数都不会用于打破平局。
    """
    strong = {}
    fuzzy = {}
    invalid_entities = 0
    for candidate, evidence in candidates:
        level = evidence.get("level")
        if level not in ("strong", "fuzzy"):
            continue
        key = _entity_key(candidate[2])
        if key is None:
            invalid_entities += 1
            continue
        target = strong if level == "strong" else fuzzy
        target.setdefault(key, []).append((candidate, evidence))

    entities = strong or fuzzy
    if len(entities) > 1:
        return {"status": "failed", "reason": "ambiguous_tmdb",
                "entities": {key: _best_candidate(values)[0]
                             for key, values in entities.items()},
                "selected": None, "confidence": None,
                "agreement_count": 0}
    if len(entities) == 1:
        key, values = next(iter(entities.items()))
        selected, selected_evidence = _best_candidate(values)
        score = selected_evidence.get("score")
        try:
            score = float(score) if score is not None else 0.0
            agreement_bonus = max(0.0, float(agreement_bonus))
        except (TypeError, ValueError):
            score, agreement_bonus = 0.0, 0.0
        confidence = min(1.0, score + agreement_bonus * (len(values) - 1))
        return {"status": "success", "reason": None,
                "entities": {key: selected}, "selected": selected,
                "confidence": round(confidence, 4),
                "agreement_count": len(values)}
    return {"status": "failed",
            "reason": ("invalid_tmdb_entity" if invalid_entities else
                       "insufficient_title_evidence" if candidates else "no_tmdb_match"),
            "entities": {}, "selected": None, "confidence": None,
            "agreement_count": 0}


def _best_candidate(values):
    """选择加权分数最高的结果；分数相同时按稳定的识别器 ID 排序。"""
    def rank(item):
        candidate, evidence = item
        try:
            score = float(evidence.get("score"))
        except (TypeError, ValueError):
            score = 0.0
        return score, str(candidate[0])

    return max(values, key=rank)
