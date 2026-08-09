from collections.abc import Mapping
from types import MappingProxyType

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type FrozenJsonValue = JsonScalar | tuple[FrozenJsonValue, ...] | Mapping[str, FrozenJsonValue]


def freeze_json(value: JsonValue | FrozenJsonValue) -> FrozenJsonValue:
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    return value


def freeze_json_mapping(
    value: Mapping[str, JsonValue | FrozenJsonValue],
) -> Mapping[str, FrozenJsonValue]:
    return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
