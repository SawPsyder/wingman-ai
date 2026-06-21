"""Config profiles = 'different Wingmen with different settings'.

A Profile bundles the knobs that affect Persistent Memory quality: the support
model's context window, the extraction sampling, the recall similarity
threshold, and (optionally) a different support model. The harness applies a
profile by mutating the live settings + reloading the model only when needed.

The named profiles below let you answer 'are our defaults good?'. The sweep
axes let you answer 'is there a *better* value?' — they generate a profile per
candidate value along a single axis.
"""

from dataclasses import dataclass

from services.skill_local_ai import SamplingPreset


@dataclass
class Profile:
    id: str
    description: str = ""
    n_ctx: int | None = None                 # None = use the user's saved setting
    extract_preset: SamplingPreset = SamplingPreset.PRECISE
    extract_temperature: float | None = None  # None = use the preset's temperature
    min_similarity: float | None = None       # None = MEMORY_MIN_SIMILARITY (0.5)
    support_model: str | None = None           # None = use the saved support model
    reasoning: bool = False


# The shipping defaults — the baseline every other profile is compared against.
DEFAULT = Profile(
    id="default",
    description="Current shipping defaults (PRECISE temp 0.1, saved n_ctx, sim 0.5)",
)

NAMED_PROFILES = {
    "default": DEFAULT,
    "temp0": Profile("temp0", "Extraction at temperature 0.0 (greedy)",
                     extract_temperature=0.0),
    "temp03": Profile("temp03", "Extraction at temperature 0.3",
                      extract_temperature=0.3),
    "bigctx": Profile("bigctx", "Larger context window (8192)", n_ctx=8192),
    "smallctx": Profile("smallctx", "Minimum context window (2048)", n_ctx=2048),
    "loose_recall": Profile("loose_recall", "Lower recall threshold (0.35)",
                            min_similarity=0.35),
    "tight_recall": Profile("tight_recall", "Higher recall threshold (0.6)",
                            min_similarity=0.6),
    "reasoning": Profile("reasoning", "Extraction with reasoning ON (expected to fail on 2B)",
                         reasoning=True),
    "balanced_extract": Profile("balanced_extract", "Extraction with BALANCED preset",
                                extract_preset=SamplingPreset.BALANCED),
}


# ── sweep axes: generate a profile per candidate value ───────────────────

def sweep_profiles(axis: str) -> list[Profile]:
    if axis in ("temp", "temperature", "extract_temp"):
        return [Profile(f"temp_{t}", f"extraction temperature {t}",
                        extract_temperature=t)
                for t in (0.0, 0.1, 0.2, 0.3, 0.5, 0.7)]
    if axis in ("ctx", "n_ctx"):
        return [Profile(f"ctx_{c}", f"n_ctx {c}", n_ctx=c)
                for c in (2048, 4096, 6144, 8192)]
    if axis in ("sim", "min_similarity", "recall"):
        return [Profile(f"sim_{s}", f"min_similarity {s}", min_similarity=s)
                for s in (0.30, 0.40, 0.50, 0.60, 0.70)]
    raise ValueError(f"unknown sweep axis '{axis}' "
                     "(try: temp | n_ctx | min_similarity)")


def resolve_profiles(names: list[str]) -> list[Profile]:
    out = []
    for n in names:
        if n not in NAMED_PROFILES:
            raise ValueError(f"unknown profile '{n}'. "
                             f"Known: {', '.join(NAMED_PROFILES)}")
        out.append(NAMED_PROFILES[n])
    return out
