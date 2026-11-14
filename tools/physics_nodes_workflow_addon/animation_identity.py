"""Cache signatures for simple keyframe-driven collider transforms.

This module deliberately supports only a narrow, fully signed subset:

* a plain MESH object used as a collider,
* object-level ``location`` / ``rotation_euler`` /
  ``rotation_quaternion`` / ``scale`` FCurves,
* curves stored in the channelbag of the *currently bound* Action slot,
* a static object parent chain (object parenting, no animation, drivers,
  constraints or rigid-body simulation anywhere in the chain).

Everything else returns an explicit reason.  The caller must refuse to sign a
cache identity unless the returned ``supported`` flag is true.  No function in
this module changes the scene frame, subframe, selection or any animation data.
"""

from __future__ import annotations

import math
from typing import Any

import bpy


ANIMATION_SCHEMA = "pnw-simple-transform-animation/1.0"

_ANIMATED_TRANSFORM_CHANNELS = {
    "location": 3,
    "rotation_euler": 3,
    "rotation_quaternion": 4,
    "scale": 3,
}

# These channels define the rest of the local transform.  They may be static
# (and are signed as such); animating any of them is outside the supported
# subset.
_STATIC_TRANSFORM_CHANNELS = {
    "delta_location": 3,
    "delta_rotation_euler": 3,
    "delta_rotation_quaternion": 4,
    "delta_scale": 3,
}

_EULER_ROTATION_MODES = frozenset({"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"})
_SUPPORTED_ROTATION_MODES = _EULER_ROTATION_MODES | {"QUATERNION"}


def _round_float(value: float, digits: int = 9) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite animation value")
    rounded = round(value, digits)
    return 0.0 if rounded == 0.0 else rounded


def _vector_payload(vector: Any, size: int | None = None) -> list[float] | None:
    try:
        values = [_round_float(float(value)) for value in vector]
    except Exception:
        return None
    if size is not None:
        if len(values) < size:
            return None
        values = values[:size]
    return values


def _matrix_payload(matrix: Any) -> list[list[float]] | None:
    try:
        return [[_round_float(float(value)) for value in row] for row in matrix]
    except Exception:
        return None


def _property_vector(owner: Any, name: str, size: int) -> list[float] | None:
    try:
        value = getattr(owner, name)
    except Exception:
        return None
    return _vector_payload(value, size)


def _id_name(id_block: Any) -> str | None:
    if id_block is None:
        return None
    return str(getattr(id_block, "name", ""))


def _action_ref(action: Any) -> dict[str, Any] | None:
    if action is None:
        return None
    return {
        "name": _id_name(action),
        "is_action_layered": bool(getattr(action, "is_action_layered", False)),
        "is_action_legacy": bool(getattr(action, "is_action_legacy", False)),
    }


def _slot_ref(slot: Any) -> dict[str, Any] | None:
    if slot is None:
        return None
    return {
        "identifier": str(getattr(slot, "identifier", "")),
        "name_display": str(getattr(slot, "name_display", "")),
        "target_id_type": str(getattr(slot, "target_id_type", "")),
        "handle": int(getattr(slot, "handle", 0)),
    }


def _driver_payload(driver: Any) -> dict[str, Any]:
    return {
        "data_path": str(getattr(driver, "data_path", "")),
        "array_index": int(getattr(driver, "array_index", 0)),
        "is_valid": bool(getattr(driver, "is_valid", False)),
    }


def _has_animation(id_block: Any) -> bool:
    if id_block is None:
        return False
    try:
        animation_data = getattr(id_block, "animation_data", None)
    except Exception:
        # An unreadable animation state cannot be proven static.
        return True
    if animation_data is None:
        return False
    return bool(
        getattr(animation_data, "action", None) is not None
        or list(getattr(animation_data, "drivers", []) or [])
        or list(getattr(animation_data, "nla_tracks", []) or [])
        or getattr(animation_data, "action_slot", None) is not None
        or int(getattr(animation_data, "action_slot_handle", 0) or 0) != 0
    )


def _static_channels_payload(
    obj: Any,
    animated_channels: set[tuple[str, int]],
    *,
    reasons: list[str] | None = None,
    prefix: str = "static_channel",
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    all_channels = dict(_ANIMATED_TRANSFORM_CHANNELS)
    all_channels.update(_STATIC_TRANSFORM_CHANNELS)
    for name, size in all_channels.items():
        values = _property_vector(obj, name, size)
        if values is None:
            if reasons is not None:
                reasons.append(f"{prefix}:unreadable_or_non_finite:{name}")
            continue
        per_index = {
            str(index): values[index]
            for index in range(size)
            if (name, index) not in animated_channels
        }
        if per_index:
            result[name] = per_index
    return result


def _keyframe_payload(point: Any, *, prefix: str, reasons: list[str]) -> dict[str, Any]:
    co = _vector_payload(getattr(point, "co", ()), 2)
    handle_left = _vector_payload(getattr(point, "handle_left", ()), 2)
    handle_right = _vector_payload(getattr(point, "handle_right", ()), 2)
    if co is None:
        reasons.append(f"{prefix}:keyframe_co_unreadable_or_non_finite")
    if handle_left is None:
        reasons.append(f"{prefix}:keyframe_handle_left_unreadable_or_non_finite")
    if handle_right is None:
        reasons.append(f"{prefix}:keyframe_handle_right_unreadable_or_non_finite")
    payload: dict[str, Any] = {
        "co": co,
        "handle_left": handle_left,
        "handle_right": handle_right,
        "handle_left_type": str(getattr(point, "handle_left_type", "")),
        "handle_right_type": str(getattr(point, "handle_right_type", "")),
        "interpolation": str(getattr(point, "interpolation", "")),
        "easing": str(getattr(point, "easing", "")),
        "type": str(getattr(point, "type", "")),
    }
    for name in ("back", "amplitude", "period"):
        try:
            payload[name] = _round_float(float(getattr(point, name)))
        except Exception:
            payload[name] = None
            reasons.append(f"{prefix}:keyframe_{name}_unreadable_or_non_finite")
    return payload


def _fcurve_payload(
    curve: Any,
    *,
    prefix: str,
    reasons: list[str],
    animated_channels: set[tuple[str, int]],
    seen_channels: set[tuple[str, int]],
) -> dict[str, Any]:
    data_path = str(getattr(curve, "data_path", ""))
    try:
        array_index = int(getattr(curve, "array_index", 0))
    except Exception:
        array_index = -1
        reasons.append(f"{prefix}:animation_channel_array_index_unreadable:{data_path}")
    expected_size = _ANIMATED_TRANSFORM_CHANNELS.get(data_path)
    if expected_size is None:
        reasons.append(f"{prefix}:animation_channel_unsupported:{data_path}[{array_index}]")
    elif array_index < 0 or array_index >= expected_size:
        reasons.append(f"{prefix}:animation_channel_index_out_of_range:{data_path}[{array_index}]")

    channel_key = (data_path, array_index)
    if channel_key in seen_channels:
        reasons.append(f"{prefix}:animation_duplicate_channel:{data_path}[{array_index}]")
    seen_channels.add(channel_key)

    modifiers = list(getattr(curve, "modifiers", []) or [])
    modifier_types = sorted(str(getattr(modifier, "type", "")) for modifier in modifiers)
    if modifiers:
        reasons.append(f"{prefix}:animation_curve_modifiers_unsupported:{data_path}[{array_index}]:{','.join(modifier_types)}")
    if getattr(curve, "driver", None) is not None:
        reasons.append(f"{prefix}:animation_curve_driver_unsupported:{data_path}[{array_index}]")
    try:
        sampled_count = len(getattr(curve, "sampled_points", []) or [])
    except Exception:
        sampled_count = 0
        reasons.append(f"{prefix}:animation_curve_sampled_points_unreadable:{data_path}[{array_index}]")
    if sampled_count:
        reasons.append(f"{prefix}:animation_curve_sampled_points_unsupported:{data_path}[{array_index}]:{sampled_count}")
    is_valid = bool(getattr(curve, "is_valid", True))
    if not is_valid:
        reasons.append(f"{prefix}:animation_curve_invalid:{data_path}[{array_index}]")

    keyframes = list(getattr(curve, "keyframe_points", []) or [])
    mute = bool(getattr(curve, "mute", False))
    effective = (
        expected_size is not None
        and 0 <= array_index < expected_size
        and is_valid
        and not mute
        and bool(keyframes)
        and not modifiers
        and getattr(curve, "driver", None) is None
        and not sampled_count
    )
    if effective:
        animated_channels.add(channel_key)

    return {
        "data_path": data_path,
        "array_index": array_index,
        "extrapolation": str(getattr(curve, "extrapolation", "")),
        "mute": mute,
        "auto_smoothing": str(getattr(curve, "auto_smoothing", "")),
        "is_valid": is_valid,
        "is_empty": bool(getattr(curve, "is_empty", not keyframes)),
        "modifiers": modifier_types,
        "sampled_point_count": sampled_count,
        "effective": effective,
        "keyframes": [
            _keyframe_payload(
                point,
                prefix=f"{prefix}:{data_path}[{array_index}]:keyframe{keyframe_index}",
                reasons=reasons,
            )
            for keyframe_index, point in enumerate(keyframes)
        ],
    }


def _channelbag_payload(
    channelbag: Any,
    *,
    prefix: str,
    reasons: list[str],
    animated_channels: set[tuple[str, int]],
) -> dict[str, Any]:
    seen_channels: set[tuple[str, int]] = set()
    curves = []
    try:
        fcurves = sorted(
            list(channelbag.fcurves),
            key=lambda curve: (str(getattr(curve, "data_path", "")), int(getattr(curve, "array_index", 0))),
        )
    except Exception as exc:
        fcurves = []
        reasons.append(
            f"{prefix}:animation_channelbag_fcurves_unreadable:"
            f"{type(exc).__name__}"
        )
    for curve in fcurves:
        curves.append(
            _fcurve_payload(
                curve,
                prefix=prefix,
                reasons=reasons,
                animated_channels=animated_channels,
                seen_channels=seen_channels,
            )
        )
    return {
        "slot_handle": int(getattr(channelbag, "slot_handle", 0)),
        "slot_identifier": str(getattr(getattr(channelbag, "slot", None), "identifier", "")),
        "fcurves": curves,
    }


def _action_layers_payload(
    action: Any,
    slot: Any,
    *,
    prefix: str,
    reasons: list[str],
    animated_channels: set[tuple[str, int]],
) -> tuple[list[dict[str, Any]], int]:
    layers_payload: list[dict[str, Any]] = []
    matched_bags = 0
    slot_handle = int(getattr(slot, "handle", 0)) if slot is not None else None
    try:
        layers = list(getattr(action, "layers", []) or [])
    except Exception as exc:
        layers = []
        reasons.append(f"{prefix}:animation_layers_unreadable:{type(exc).__name__}")
    for layer_index, layer in enumerate(layers):
        strips_payload: list[dict[str, Any]] = []
        try:
            strips = list(getattr(layer, "strips", []) or [])
        except Exception as exc:
            strips = []
            reasons.append(
                f"{prefix}:animation_layer_strips_unreadable:{layer_index}:{type(exc).__name__}"
            )
        for strip_index, strip in enumerate(strips):
            strip_type = str(getattr(strip, "type", ""))
            if strip_type != "KEYFRAME":
                reasons.append(f"{prefix}:animation_strip_unsupported:{layer_index}:{strip_index}:{strip_type}")
            bags_payload: list[dict[str, Any]] = []
            try:
                bags = list(getattr(strip, "channelbags", []) or [])
            except Exception as exc:
                bags = []
                reasons.append(
                    f"{prefix}:animation_strip_channelbags_unreadable:"
                    f"{layer_index}:{strip_index}:{type(exc).__name__}"
                )
            for bag in bags:
                if slot_handle is None or int(getattr(bag, "slot_handle", 0)) != slot_handle:
                    continue
                matched_bags += 1
                bags_payload.append(
                    _channelbag_payload(
                        bag,
                        prefix=f"{prefix}:layer{layer_index}:strip{strip_index}",
                        reasons=reasons,
                        animated_channels=animated_channels,
                    )
                )
            strips_payload.append(
                {
                    "index": strip_index,
                    "type": strip_type,
                    "channelbags": bags_payload,
                }
            )
        layers_payload.append({"index": layer_index, "strips": strips_payload})
    return layers_payload, matched_bags


def _action_payload(
    action: Any,
    slot: Any,
    *,
    prefix: str,
    reasons: list[str],
    animated_channels: set[tuple[str, int]],
) -> tuple[dict[str, Any], int]:
    layers_payload, matched_bags = _action_layers_payload(
        action,
        slot,
        prefix=prefix,
        reasons=reasons,
        animated_channels=animated_channels,
    )
    payload: dict[str, Any] = {
        "name": _id_name(action),
        "is_action_layered": bool(getattr(action, "is_action_layered", False)),
        "is_action_legacy": bool(getattr(action, "is_action_legacy", False)),
        "use_frame_range": bool(getattr(action, "use_frame_range", False)),
        "use_cyclic": bool(getattr(action, "use_cyclic", False)),
        "layers": layers_payload,
    }
    for name in ("frame_start", "frame_end"):
        try:
            payload[name] = _round_float(float(getattr(action, name)))
        except Exception:
            payload[name] = None
            reasons.append(f"{prefix}:animation_action_{name}_unreadable_or_non_finite")
    return payload, matched_bags


def sign_object_transform(obj: Any) -> dict[str, Any]:
    """Return a static signature for a collider object's transform animation.

    The returned dictionary is JSON-friendly and stable across scene frames for
    supported cases.  It never contains evaluated world/local matrices for an
    animated object and never changes frame/subframe/selection.
    """
    reasons: list[str] = []
    matrix_parent_inverse = _matrix_payload(getattr(obj, "matrix_parent_inverse", []))
    if matrix_parent_inverse is None:
        reasons.append("animation:matrix_parent_inverse_unreadable_or_non_finite")
    for reason in static_parent_value_reasons(obj):
        reasons.append(reason)
    payload: dict[str, Any] = {
        "schema": ANIMATION_SCHEMA,
        "rotation_mode": str(getattr(obj, "rotation_mode", "")),
        "parent_type": str(getattr(obj, "parent_type", "OBJECT")),
        "parent_name": _id_name(getattr(obj, "parent", None)),
        "parent_bone": str(getattr(obj, "parent_bone", "") or ""),
        "matrix_parent_inverse": matrix_parent_inverse,
        "static_channels": {},
        "animation": {"animated": False},
    }

    try:
        animation_data = getattr(obj, "animation_data", None)
    except Exception as exc:
        animation_data = None
        reasons.append(f"animation:animation_data_unreadable:{type(exc).__name__}")
    action = getattr(animation_data, "action", None) if animation_data is not None else None
    slot = getattr(animation_data, "action_slot", None) if animation_data is not None else None
    try:
        drivers = list(getattr(animation_data, "drivers", []) or []) if animation_data is not None else []
    except Exception as exc:
        drivers = []
        reasons.append(f"animation:drivers_unreadable:{type(exc).__name__}")
    try:
        nla_tracks = list(getattr(animation_data, "nla_tracks", []) or []) if animation_data is not None else []
    except Exception as exc:
        nla_tracks = []
        reasons.append(f"animation:nla_tracks_unreadable:{type(exc).__name__}")
    try:
        handle = int(getattr(animation_data, "action_slot_handle", 0) or 0) if animation_data is not None else 0
    except Exception as exc:
        handle = 0
        reasons.append(f"animation:action_slot_handle_unreadable:{type(exc).__name__}")

    animated = bool(action is not None or slot is not None or drivers or nla_tracks or handle)
    if not animated:
        payload["static_channels"] = _static_channels_payload(
            obj,
            set(),
            reasons=reasons,
            prefix="animation",
        )
        return {
            "supported": not reasons,
            "animated": False,
            "reasons": reasons,
            "payload": payload,
        }

    if nla_tracks:
        reasons.append(
            "animation_nla_unsupported:" + ",".join(sorted(str(getattr(track, "name", "")) for track in nla_tracks))
        )
    if animation_data is not None and bool(getattr(animation_data, "use_tweak_mode", False)):
        reasons.append("animation_nla_tweak_mode_unsupported")
    if drivers:
        reasons.append(
            "animation_drivers_unsupported:"
            + ",".join(
                sorted(
                    f"{getattr(driver, 'data_path', '')}[{getattr(driver, 'array_index', 0)}]"
                    for driver in drivers
                )
            )
        )

    animated_channels: set[tuple[str, int]] = set()
    matched_bags = 0
    if action is None:
        reasons.append("animation_action_missing")
    else:
        if not bool(getattr(action, "is_action_layered", False)):
            reasons.append("animation_action_not_layered")
        if bool(getattr(action, "is_action_legacy", False)):
            reasons.append("animation_action_legacy_unsupported")
        if slot is None:
            reasons.append("animation_action_slot_unbound")
        else:
            if str(getattr(slot, "target_id_type", "")) != "OBJECT":
                reasons.append(f"animation_slot_target_id_type_unsupported:{getattr(slot, 'target_id_type', '')}")
            slot_handle = int(getattr(slot, "handle", 0))
            if handle not in {0, slot_handle}:
                reasons.append(f"animation_slot_handle_mismatch:{handle}:{slot_handle}")
            if str(getattr(obj, "rotation_mode", "")) not in _SUPPORTED_ROTATION_MODES:
                reasons.append(
                    f"animation_rotation_mode_unsupported:{getattr(obj, 'rotation_mode', '')}"
                )
    action_payload = None
    if action is not None:
        action_payload, matched_bags = _action_payload(
            action,
            slot,
            prefix="animation",
            reasons=reasons,
            animated_channels=animated_channels,
        )

    payload["static_channels"] = _static_channels_payload(
        obj,
        animated_channels,
        reasons=reasons,
        prefix="animation",
    )
    payload["animation"] = {
        "animated": True,
        "action": _action_ref(action),
        "slot": _slot_ref(slot),
        "anim_data": {
            "action_influence": (
                _round_float(float(getattr(animation_data, "action_influence", 1.0)))
                if animation_data is not None
                else None
            ),
            "action_blend_type": str(getattr(animation_data, "action_blend_type", ""))
            if animation_data is not None
            else None,
            "action_extrapolation": str(getattr(animation_data, "action_extrapolation", ""))
            if animation_data is not None
            else None,
            "use_nla": bool(getattr(animation_data, "use_nla", False)) if animation_data is not None else False,
            "use_tweak_mode": bool(getattr(animation_data, "use_tweak_mode", False))
            if animation_data is not None
            else False,
            "action_slot_handle": handle,
        },
        "drivers": sorted((_driver_payload(driver) for driver in drivers), key=lambda item: (item["data_path"], item["array_index"])),
        "nla_tracks": sorted(str(getattr(track, "name", "")) for track in nla_tracks),
        "action_payload": action_payload,
        "animated_channels": [
            f"{data_path}[{array_index}]" for data_path, array_index in sorted(animated_channels)
        ],
        "bound_slot_channelbag_count": matched_bags,
    }
    return {
        "supported": not reasons,
        "animated": True,
        "reasons": reasons,
        "payload": payload,
    }


def _static_transform_value_reasons(owner: Any, *, prefix: str) -> list[str]:
    reasons: list[str] = []
    _static_channels_payload(owner, set(), reasons=reasons, prefix=prefix)
    for name in ("matrix_world", "matrix_local", "matrix_parent_inverse"):
        if _matrix_payload(getattr(owner, name, [])) is None:
            reasons.append(f"{prefix}:{name}_unreadable_or_non_finite")
    return reasons


def static_parent_value_reasons(obj: Any) -> list[str]:
    """Reject unreadable/non-finite static-parent transform values.

    This deliberately checks only values, not structural staticness.  The
    animated-collider path additionally calls :func:`static_parent_reasons`
    for animation/constraint/rigid-body rejection.
    """
    reasons: list[str] = []
    current = getattr(obj, "parent", None)
    seen: set[int] = set()
    while current is not None:
        try:
            pointer = int(current.as_pointer())
        except Exception:
            pointer = id(current)
        if pointer in seen:
            reasons.append(f"parent_cycle:{getattr(current, 'name', '')}")
            break
        seen.add(pointer)
        reasons.extend(
            _static_transform_value_reasons(
                current,
                prefix=f"parent:{getattr(current, 'name', '')}",
            )
        )
        current = getattr(current, "parent", None)
    return reasons


def static_parent_reasons(obj: Any) -> list[str]:
    """Reasons why the object's parent chain is not a signed static transform."""
    reasons: list[str] = []
    child = obj
    current = getattr(obj, "parent", None)
    seen: set[int] = set()
    while current is not None:
        try:
            pointer = int(current.as_pointer())
        except Exception:
            pointer = id(current)
        if pointer in seen:
            reasons.append(f"parent_cycle:{getattr(current, 'name', '')}")
            break
        seen.add(pointer)

        parent_type = str(getattr(child, "parent_type", "OBJECT"))
        if parent_type != "OBJECT":
            reasons.append(
                f"parent_type_unsupported:{getattr(child, 'name', '')}:{parent_type}"
            )
        if str(getattr(child, "parent_bone", "") or ""):
            reasons.append(
                f"parent_bone_unsupported:{getattr(child, 'name', '')}:{getattr(child, 'parent_bone', '')}"
            )
        if _has_animation(current):
            reasons.append(f"parent_animated:{getattr(current, 'name', '')}")
        data = getattr(current, "data", None)
        if _has_animation(data):
            reasons.append(f"parent_data_animated:{getattr(current, 'name', '')}")
        shape_keys = getattr(data, "shape_keys", None)
        if _has_animation(shape_keys):
            reasons.append(f"parent_shape_keys_animated:{getattr(current, 'name', '')}")
        try:
            constraints = list(getattr(current, "constraints", []) or [])
        except Exception:
            constraints = []
        if constraints:
            reasons.append(
                f"parent_constraints_unsupported:{getattr(current, 'name', '')}:"
                + ",".join(sorted(str(getattr(constraint, "name", "")) for constraint in constraints))
            )
        if getattr(current, "rigid_body", None) is not None:
            reasons.append(f"parent_rigid_body_unsupported:{getattr(current, 'name', '')}")
        for reason in _static_transform_value_reasons(
            current,
            prefix=f"parent:{getattr(current, 'name', '')}",
        ):
            reasons.append(reason)

        child = current
        current = getattr(current, "parent", None)
    return reasons


def static_parent_chain_payload(obj: Any) -> list[dict[str, Any]]:
    """Frame-stable payload for a parent chain already validated as static."""
    chain: list[dict[str, Any]] = []
    current = getattr(obj, "parent", None)
    seen: set[int] = set()
    while current is not None:
        try:
            pointer = int(current.as_pointer())
        except Exception:
            pointer = id(current)
        if pointer in seen:
            chain.append({"name": getattr(current, "name", ""), "cycle": True})
            break
        seen.add(pointer)
        chain.append(
            {
                "name": getattr(current, "name", ""),
                "type": getattr(current, "type", ""),
                "parent_type": str(getattr(current, "parent_type", "OBJECT")),
                "parent_bone": str(getattr(current, "parent_bone", "") or ""),
                "rotation_mode": str(getattr(current, "rotation_mode", "")),
                "static_channels": _static_channels_payload(current, set()),
                "matrix_world": _matrix_payload(getattr(current, "matrix_world", [])),
                "matrix_local": _matrix_payload(getattr(current, "matrix_local", [])),
                "matrix_parent_inverse": _matrix_payload(getattr(current, "matrix_parent_inverse", [])),
                "hide_viewport": bool(getattr(current, "hide_viewport", False)),
                "hide_render": bool(getattr(current, "hide_render", False)),
            }
        )
        current = getattr(current, "parent", None)
    return chain
