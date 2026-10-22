"""确定性地合并实体，并保留每次查询和各个独立来源。"""

from copy import deepcopy
import json
from app.media.recognition.contracts import Candidate, FieldEvidence


def merge_candidates(responses, plan):
    entities, items = {}, {x.query_id: x for x in plan.items}
    ordered = sorted(responses, key=lambda x: (x.query_id, x.page))
    for response in ordered:
        item = items[response.query_id]
        for raw in response.consumed_results if response.consumed_results is not None else response.raw_json["results"]:
            kind = raw.get("media_type", item.endpoint_type)
            if kind not in {"movie", "tv"} or type(raw.get("id")) is not int or raw["id"] <= 0:
                continue
            key = kind, raw["id"]
            row = entities.setdefault(key, {"variants": [], "sources": {}, "queries": set()})
            row["variants"].append(raw)
            row["queries"].add(item.query_id)
            for source_id in item.sources:
                row["sources"][source_id] = plan.sources[source_id]
    result = []
    for (kind, tmdb_id), row in sorted(entities.items()):
        variants = sorted(row["variants"], key=lambda x: (-len(x), json.dumps(x, sort_keys=True, ensure_ascii=False)))
        fields = {}
        for raw in reversed(variants):
            fields.update(deepcopy(raw))
        fields["adult"] = any(raw.get("adult") is True for raw in variants)
        fields["_sources"] = [row["sources"][k] for k in sorted(row["sources"])]
        fields["_queries"] = sorted(row["queries"])
        title = fields.get("title") or fields.get("name") or ""
        candidate = Candidate(kind, tmdb_id, title, fields)
        candidate.evidence = [FieldEvidence(value["field"], value["provider_id"]+":"+value["family"], value["title"])
                              for value in fields["_sources"]]
        result.append(candidate)
    return result
