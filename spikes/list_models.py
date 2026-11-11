"""Spike (a): list the models this account can use and pick a cheap one.

Writes fixtures/models.json. No agent turn is started (no credits used).
"""

from __future__ import annotations

import asyncio
import json
import time

from droid_sdk import list_models

from _common import DEFAULT_MODEL, FIXTURES, SANDBOX, make_runtime, prepare_sandbox, setup_console, to_jsonable

_LIGHT_HINTS = ("haiku", "mini", "flash", "lite", "light", "small", "nano")


def _cost_key(model) -> tuple[float, int, str]:
    multiplier = model.token_multiplier if model.token_multiplier is not None else float("inf")
    hinted = 0 if any(h in model.id.lower() for h in _LIGHT_HINTS) else 1
    return multiplier, hinted, model.id


async def main() -> int:
    prepare_sandbox()
    runtime, droid = make_runtime()
    print(f"droid: {droid['executable']} ({droid['version']})")
    started = time.perf_counter()
    models = await list_models(include_disabled=True, cwd=SANDBOX, runtime=runtime)
    elapsed = time.perf_counter() - started
    print(f"list_models: {len(models)} models in {elapsed:.2f}s (one-shot droid process)")
    print(f"{'':2}{'id':44} {'display_name':34} {'provider':10} {'mult':>5} {'default':8} efforts")
    for model in models:
        flag = "x " if model.disabled else "  "
        mult = "-" if model.token_multiplier is None else f"{model.token_multiplier:g}"
        efforts = ",".join(e.value for e in model.supported_reasoning_efforts)
        print(
            f"{flag}{model.id:44} {model.display_name:34} {model.model_provider.value:10} {mult:>5} "
            f"{model.default_reasoning_effort.value:8} {efforts}"
            + (f"  [disabled: {model.disabled_reason}]" if model.disabled else "")
        )
    enabled = [m for m in models if not m.disabled and not m.is_custom]
    if enabled:
        pick = min(enabled, key=_cost_key)
        print(f"cheapest enabled built-in model: {pick.id} ({pick.display_name}, multiplier {pick.token_multiplier})")
    print(f"spikes default model (DEFAULT_MODEL / WITTYCODE_MODEL): {DEFAULT_MODEL}")
    if DEFAULT_MODEL not in {m.id for m in enabled}:
        print("WARNING: DEFAULT_MODEL is not an enabled built-in model in this catalog")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "models.json").write_text(
        json.dumps(
            {"elapsed_s": round(elapsed, 3), "models": to_jsonable(models)},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {FIXTURES / 'models.json'}")
    return 0


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))
