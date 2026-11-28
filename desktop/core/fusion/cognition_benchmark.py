"""Cognition's public FrontierCode 2.0 measurements, not an Artificial Analysis substitute."""
from __future__ import annotations

import json
import math
import urllib.request
from collections.abc import Mapping
from typing import Any

BENCHMARK = "FrontierCode 2.0"
URL = "https://cognition.com/data/swe-2-1/frontier-code-2.0.json"
MAX_BYTES = 128 * 1024
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_SWE_21 = "devin:swe-2-1"
_MODEL_IDS = frozenset((_SWE_21, *(_SWE_21 + "-" + effort for effort in _EFFORTS)))


def matches(model: str) -> bool:
    # The suffix is part of the ACP model ID, not the separately configured `thought_level`.
    return model in _MODEL_IDS


def _number(value: Any, *, maximum: float = math.inf) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def clean(rows: Any) -> dict[str, dict[str, float]]:
    """Keep a verified score/cost pair for each measured effort, with score as a fraction."""
    result = {}
    if isinstance(rows, Mapping):
        for effort in _EFFORTS:
            row = rows.get(effort)
            if isinstance(row, Mapping):
                score = _number(row.get("new_score"), maximum=1)
                cost = _number(row.get("cost"))
                if score is not None and cost is not None:
                    result[effort] = {"new_score": score, "cost": cost}
    return result


def parse(payload: Any) -> dict[str, dict[str, float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return {}
    harness = payload.get("harness")
    if not isinstance(harness, dict) or harness.get("SWE-2.1") != "Devin CLI":
        return {}
    model = payload["data"].get("SWE-2.1")
    if not isinstance(model, dict):
        return {}
    return clean({effort: row.get("main") for effort, row in model.items() if isinstance(row, dict)})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def download() -> dict[str, dict[str, float]]:
    request = urllib.request.Request(URL, headers={"User-Agent": "WittyCode Fusion model profiles"})
    with _urlopen(request, timeout=10) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("The Cognition benchmark response is larger than expected.")
    return parse(json.loads(data))
