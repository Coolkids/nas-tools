"""根据已计算的标题证据，确定性地选择媒体实体。"""


TITLE_SIMILARITY_MIN_SCORE = 0.88
TITLE_SIMILARITY_MIN_MARGIN = 0.05


def select_title_similarity(evidences, minimum_score=TITLE_SIMILARITY_MIN_SCORE,
                            minimum_margin=TITLE_SIMILARITY_MIN_MARGIN):
    """仅在完整标题相似度足够高且明显领先时，返回获胜证据的下标。"""
    scores = []
    for evidence in evidences:
        try:
            score = float(evidence["similarity_score"])
        except (KeyError, TypeError, ValueError):
            return None
        if not 0 <= score <= 1:
            return None
        scores.append(score)
    if not scores:
        return None
    ranked = sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
    best = ranked[0]
    if scores[best] < minimum_score:
        return None
    if len(ranked) > 1 and round(scores[best] - scores[ranked[1]], 4) < minimum_margin:
        return None
    return best


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
    """先比较完整标题相似度，再选择唯一的强匹配或模糊匹配实体。

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

    combined = {key: list(values) for key, values in strong.items()}
    for key, values in fuzzy.items():
        combined.setdefault(key, []).extend(values)
    entity_keys = list(combined)
    similarity_evidence = []
    for values in combined.values():
        try:
            scores = [float(item["similarity_score"]) for _, item in values]
            similarity_evidence.append({"similarity_score": max(scores)}
                                       if all(0 <= score <= 1 for score in scores) else {})
        except (KeyError, TypeError, ValueError):
            similarity_evidence.append({})
    winner = select_title_similarity(similarity_evidence) if len(combined) > 1 else None
    entities = strong or fuzzy
    if winner is not None:
        entities = {entity_keys[winner]: combined[entity_keys[winner]]}
    elif all("similarity_score" in item for item in similarity_evidence) and any(
            similarity_evidence[index]["similarity_score"] >= TITLE_SIMILARITY_MIN_SCORE
            for index, key in enumerate(entity_keys) if key in fuzzy):
        # 完整标题的近似匹配与包含匹配过于接近时，同样保留歧义。
        entities = combined
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
