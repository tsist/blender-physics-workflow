"""Read-only measurements with explicit scope and unavailable-value semantics."""
from __future__ import annotations

import math


class GroundVertices:
    """Accumulate world-space mesh vertex heights without retaining expanded meshes."""

    def __init__(self, ground_z, tolerance=1e-4):
        self.ground_z = ground_z
        self.tolerance = tolerance
        self.count = 0
        self.below = 0
        self.minimum = None
        self.invalid = False
        self.unsupported = set()
        self._prototypes = {}

    def add(self, z):
        if not math.isfinite(z):
            self.invalid = True
            return
        value = z - self.ground_z
        self.count += 1
        self.below += value < -self.tolerance
        self.minimum = value if self.minimum is None else min(self.minimum, value)

    def add_instance(self, instance):
        obj = instance.object
        data = getattr(obj, 'data', None)
        if getattr(obj, 'type', None) != 'MESH' or data is None:
            self.unsupported.add('instance_' + str(getattr(obj, 'type', 'unknown')))
            return
        key = data.as_pointer()
        points = self._prototypes.get(key)
        if points is None:
            points = [tuple(vertex.co) for vertex in data.vertices]
            self._prototypes[key] = points
        row = instance.matrix_world[2]
        for x, y, z in points:
            self.add(row[0] * x + row[1] * y + row[2] * z + row[3])

    def result(self):
        status = ('invalid_geometry' if self.invalid else
                  'partial' if self.unsupported else 'observed' if self.count else 'not_observable')
        return {'status': status, 'clearance_min': None if self.invalid else self.minimum,
                'vertex_count': self.count,
                'below_ground_vertex_count': None if self.invalid or not self.count else self.below,
                'unsupported_components': sorted(self.unsupported),
                'scope': 'evaluated_mesh_vertices_including_mesh_instances'}


def _components(geometry):
    if geometry is None:
        return
    components = [('instances', geometry.instances_pointcloud()), ('mesh', geometry.mesh),
                  ('pointcloud', geometry.pointcloud), ('curves', geometry.curves)]
    for name, data in components:
        if data is None:
            continue
        if name == 'mesh':
            count = len(data.vertices)
        elif name == 'instances':
            transform = data.attributes.get('instance_transform')
            count = len(transform.data) if transform else 0
        else:
            count = len(data.points)
        if not count:
            continue
        yield name, data, count


def velocity_observation(geometry, threshold):
    rows, speeds = [], []
    for name, data, count in _components(geometry):
        attr = data.attributes.get('pnw_velocity')
        status = ('not_observable' if attr is None else
                  'observed' if attr.data_type == 'FLOAT_VECTOR' and attr.domain == 'POINT'
                  and len(attr.data) == count else 'invalid_attribute')
        values = [math.sqrt(sum(float(v) ** 2 for v in item.vector)) for item in attr.data] if status == 'observed' else []
        if any(not math.isfinite(value) for value in values):
            status = 'invalid_attribute'
        if status == 'observed':
            speeds.extend(values)
        rows.append({'component': name, 'count': count, 'status': status})
    status = _combined_status([row['status'] for row in rows])
    return {'status': status, 'components': rows, 'count': len(speeds),
            'scope': 'top_level_output_point_rows', 'threshold': threshold,
            'mean': sum(speeds) / len(speeds) if status == 'observed' else None,
            'max': max(speeds) if status == 'observed' else None,
            'resting_ratio': sum(v <= threshold for v in speeds) / len(speeds) if status == 'observed' else None}


def _combined_status(statuses):
    return ('invalid_attribute' if 'invalid_attribute' in statuses else
            'observed' if statuses and all(s == 'observed' for s in statuses) else
            'partial' if 'observed' in statuses else 'not_observable')


def contact_convergence(geometry):
    """Keep contact evidence bound to every populated top-level output component."""
    rows = []
    for name, data, count in _components(geometry):
        residual = data.attributes.get('pnw_contact_residual')
        converged = data.attributes.get('pnw_contact_converged')
        status = 'observed'
        values = []
        if residual is None and converged is None:
            status = 'not_observable'
        elif (residual is None or converged is None or residual.data_type != 'FLOAT'
              or converged.data_type != 'BOOLEAN' or residual.domain != 'POINT'
              or converged.domain != 'POINT' or len(residual.data) != count
              or len(converged.data) != count):
            status = 'invalid_attribute'
        else:
            values = [float(item.value) for item in residual.data]
            if any(not math.isfinite(value) or value < 0 for value in values):
                status = 'invalid_attribute'
        rows.append({'component': name, 'count': count, 'status': status,
                     'max_residual': max(values) if status == 'observed' else None,
                     'converged': all(item.value for item in converged.data) if status == 'observed' else None})
    status = _combined_status([row['status'] for row in rows])
    return {'status': status, 'components': rows, 'count': sum(row['count'] for row in rows),
            'max_residual': max(row['max_residual'] for row in rows) if status == 'observed' else None,
            'converged': all(row['converged'] for row in rows) if status == 'observed' else None,
            'scope': 'uniform_sphere_overlap_not_resting_state'}


def solver_sleep(geometry):
    """Observe output rows; these are not necessarily unique physical bodies."""
    rows = []
    for name, data, count in _components(geometry):
        row = {'component': name, 'count': count}
        for field, attribute in [('sleeping', 'pnw_sleep'), ('active', 'pnw_active'),
                                 ('rooted', 'pnw_sleep_rooted')]:
            attr = data.attributes.get(attribute)
            status = ('not_observable' if attr is None else
                      'observed' if attr.data_type == 'BOOLEAN' and attr.domain == 'POINT'
                      and len(attr.data) == count else 'invalid_attribute')
            row[field] = {'status': status, 'count':
                          sum(bool(item.value) for item in attr.data) if status == 'observed' else None}
        rows.append(row)
    count = sum(row['count'] for row in rows)
    result = {'scope': 'top_level_output_point_rows_not_unique_bodies_or_resting_quality',
              'components': rows, 'count': count}
    for field in ('sleeping', 'active', 'rooted'):
        statuses = [row[field]['status'] for row in rows]
        status = _combined_status(statuses)
        total = sum(row[field]['count'] for row in rows) if status == 'observed' else None
        result[field + '_count'] = total
        result[field + '_status'] = status
    result['status'] = result['sleeping_status']
    result['sleeping_ratio'] = result['sleeping_count'] / count if result['status'] == 'observed' else None
    return result


def late_motion(points_a, points_b, frame_span, seconds):
    """An index-paired estimate, never an identity or continuous-motion guarantee."""
    reason = ('no_frame_interval' if frame_span <= 0 else
              'empty_output' if not points_a or not points_b else
              'point_count_changed' if len(points_a) != len(points_b) else None)
    deltas = [] if reason else [math.dist(a, b) for a, b in zip(points_a, points_b)]
    if any(not math.isfinite(value) for value in deltas):
        reason = 'non_finite_position'
        deltas = []
    return ({'status': 'not_observable' if reason else 'estimated', 'reason': reason,
             'scope': 'index_paired_sample_positions_no_identity_guarantee',
             'point_count_start': len(points_a), 'point_count_end': len(points_b),
             'dt_seconds': seconds,
             'mean_delta': sum(deltas) / len(deltas) if deltas else None,
             'max_delta': max(deltas) if deltas else None}, deltas)
