"""VOICEVOX normal-speech option validation shared by runtime and Web API."""
import math


_FIELDS = {
    "volume": (0.5, 2.0),
    "pitch": (-0.15, 0.15),
    "speed": (0.5, 3.0),
}


def is_voicevox_provider(provider: object) -> bool:
    if not isinstance(provider, str):
        return False
    return provider.strip().removeprefix("tts.") == "voicevox_tts"


def validate_tts_options(value: object) -> dict:
    """Return a normalized options dict or raise ValueError."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("tts_options はオブジェクトで指定してください")

    unknown = set(value) - {"speaker", *_FIELDS}
    if unknown:
        raise ValueError(f"tts_options に未対応の項目があります: {', '.join(sorted(unknown))}")
    if value and "speaker" not in value:
        raise ValueError("tts_optionsを設定する場合はspeakerが必要です")

    normalized = {}
    if "speaker" in value:
        speaker = value["speaker"]
        if isinstance(speaker, bool) or not isinstance(speaker, int) or speaker < 0:
            raise ValueError("tts_options.speaker は0以上の整数で指定してください")
        normalized["speaker"] = speaker

    for key, (minimum, maximum) in _FIELDS.items():
        if key not in value:
            continue
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"tts_options.{key} は数値で指定してください")
        number = float(raw)
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError(
                f"tts_options.{key} は{minimum}以上{maximum}以下で指定してください"
            )
        normalized[key] = number
    return normalized


def normalize_tts_options(value: object) -> dict:
    """Runtime-safe validation: malformed hand-edited values are ignored."""
    try:
        return validate_tts_options(value)
    except ValueError:
        return {}
