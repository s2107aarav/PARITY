"""Provider counters are optional evidence, never assumed present on failures."""


def has_output_tokens(record: dict) -> bool:
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return False
    count = usage.get("output_tokens")
    return isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0
