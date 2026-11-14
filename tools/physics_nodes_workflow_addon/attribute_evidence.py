"""Read named physics attributes from actual evaluated components, without realizing.

In Blender 5.2, realizing an instance can replace an instance attribute with a
same-named prototype attribute. GeometrySet.instances_pointcloud preserves the
actual INSTANCE values instead. All borrowed values are consumed before any
frame/depsgraph change. Nested prototype state and internal solver state are
explicitly outside this top-level output contract.
"""
from __future__ import annotations

import hashlib
import json
import math

ATTRIBUTE_SCHEMA = "pnw-output-attributes/1"
TYPE_FIELDS = {
    "FLOAT": "value", "INT": "value", "BOOLEAN": "value", "INT8": "value",
    "STRING": "value", "QUATERNION": "value", "FLOAT4X4": "value",
    "INT16_2D": "value", "INT32_2D": "value", "FLOAT_VECTOR": "vector",
    "FLOAT2": "vector", "FLOAT4": "vector", "FLOAT_COLOR": "color", "BYTE_COLOR": "color",
}


class AttributeEvidenceError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _plain(value, label):
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AttributeEvidenceError("NON_FINITE_PHYSICS_ATTRIBUTE", f"{label} is not finite")
        return f"{0.0 if value == 0.0 else value:.6f}"
    try:
        return [_plain(item, label) for item in value]
    except TypeError as exc:
        raise AttributeEvidenceError("UNSUPPORTED_PHYSICS_ATTRIBUTE_VALUE", label) from exc


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sample_output_attributes(eval_obj, *, velocity_threshold=0.05, reference_digest):
    try:
        geometry = eval_obj.evaluated_geometry()
        components = [("mesh", geometry.mesh), ("pointcloud", geometry.pointcloud),
                      ("curves", geometry.curves), ("instances", geometry.instances_pointcloud())]
    except Exception as exc:
        raise AttributeEvidenceError("OUTPUT_ATTRIBUTE_ACCESS_FAILED", str(exc)) from exc

    rows, component_rows = [], []
    speed_count, speed_sum, speed_max, resting_count = 0, 0.0, 0.0, 0
    for component, data in components:
        component_row = {"component": component, "present": data is not None}
        component_rows.append(component_row)
        if data is None:
            continue
        if component == "instances":
            # Keep index alignment between transforms and attribute arrays.
            # Sorting values independently would lose which instance owns them.
            transforms = data.attributes.get("instance_transform")
            references = data.attributes.get(".reference_index")
            if transforms is None or references is None or len(transforms.data) != len(references.data):
                raise AttributeEvidenceError("INSTANCE_ATTRIBUTE_BINDING_MISSING", "Missing instance transform/reference rows")
            binding = hashlib.sha256()
            reference_digests = [reference_digest(ref) for ref in geometry.instance_references()]
            for index in range(len(transforms.data)):
                reference_index = int(references.data[index].value)
                if not 0 <= reference_index < len(reference_digests):
                    raise AttributeEvidenceError("INSTANCE_ATTRIBUTE_BINDING_MISSING", "Invalid instance reference index")
                row = [_plain(transforms.data[index].value, "instance_transform"),
                       reference_index, reference_digests[reference_index]]
                binding.update((_json(row)+"\n").encode())
            component_row["instance_order_binding_sha256"] = binding.hexdigest()
            component_row["instance_count"] = len(transforms.data)
        for attr in sorted(data.attributes, key=lambda item: item.name):
            if not attr.name.startswith("pnw_"):
                continue
            if attr.data_type not in TYPE_FIELDS:
                raise AttributeEvidenceError("UNSUPPORTED_PHYSICS_ATTRIBUTE_TYPE", f"{component}.{attr.name}: {attr.data_type}")
            record = {"component": component, "name": attr.name,
                      "domain": "INSTANCE" if component == "instances" else attr.domain,
                      "storage_domain": attr.domain, "data_type": attr.data_type, "count": len(attr.data)}
            digest = hashlib.sha256()
            digest.update((_json(record)+"\n").encode())
            field = TYPE_FIELDS[attr.data_type]
            for index, item in enumerate(attr.data):
                value = getattr(item, field)
                copied = _plain(value, f"{component}.{attr.name}[{index}]")
                digest.update((_json(copied)+"\n").encode())
                if attr.name == "pnw_velocity" and attr.data_type == "FLOAT_VECTOR":
                    speed = math.sqrt(sum(float(v)**2 for v in copied))
                    speed_count += 1
                    speed_sum += speed
                    speed_max = max(speed_max, speed)
                    resting_count += speed <= velocity_threshold
            record["sha256"] = digest.hexdigest()
            rows.append(record)

    payload = {"schema": ATTRIBUTE_SCHEMA, "components": component_rows, "attributes": rows,
               "scope": "top_level_evaluated_output_components"}
    digest = hashlib.sha256(_json(payload).encode()).hexdigest()
    return {**payload, "sha256": digest,
            "attribute_names": sorted({row["name"] for row in rows}),
            "coverage": {"physics_attributes_compared": bool(rows),
                         "attribute_inventory_compared": True,
                         "physics_attribute_scope": payload["scope"],
                         "internal_solver_state_compared": False,
                         "nested_reference_attributes_compared": False,
                         "availability": "present" if rows else "no_pnw_attributes_in_output"},
            "velocity_statistics": {"count": speed_count,
                                    "mean": speed_sum/speed_count if speed_count else None,
                                    "max": speed_max if speed_count else None,
                                    "resting_ratio": resting_count/speed_count if speed_count else None,
                                    "threshold": velocity_threshold,
                                    "source": "evaluated_output_pnw_velocity"}}
