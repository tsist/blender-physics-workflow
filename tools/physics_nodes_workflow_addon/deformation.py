"""Optional cloth effectors using the installed Blender dynamics assets.

The surrounding Simulation Zone owns time and cache state. These effectors
extend its existing XPBD step, without nesting another Simulation Zone.
"""
from __future__ import annotations


def extend_cloth_step(tree, gin, cloth_bundle, solver, get_geometry, rest_position, builtin, add_socket):
    for name, kind, default in (
        ("Bending", "NodeSocketBool", False),
        ("Bend Softness", "NodeSocketFloat", 0.1),
        ("Tearing", "NodeSocketBool", False),
        ("Tear Threshold", "NodeSocketFloat", 1.2),
    ):
        add_socket(tree, name, "INPUT", kind, default)

    bend = tree.nodes.new("GeometryNodeGroup")
    bend.node_tree = builtin["Cross Edge Length Constraint"]
    bend.label = "Native cross-edge bending (mesh faces)"
    tree.links.new(gin.outputs["Bend Softness"], bend.inputs["Softness"])
    tree.links.new(rest_position, bend.inputs["Rest Position"])

    tear = tree.nodes.new("GeometryNodeGroup")
    tear.node_tree = builtin["Tearing Effector"]
    tear.label = "Native strain-based tearing"
    tear.inputs["Mode"].default_value = "All"
    tree.links.new(gin.outputs["Tear Threshold"], tear.inputs["Threshold"])
    tree.links.new(rest_position, tear.inputs["Voronoi Rest Position"])

    for label, node, output, toggle in (
        ("Cross Edge Length", bend, "Constraint", "Bending"),
        ("Tearing", tear, "Effector", "Tearing"),
    ):
        enabled = tree.nodes.new("GeometryNodeSwitch")
        enabled.input_type = "BUNDLE"
        enabled.label = label + " enable"
        tree.links.new(gin.outputs[toggle], enabled.inputs["Switch"])
        tree.links.new(node.outputs[output], enabled.inputs["True"])
        cloth_bundle.bundle_items.new("BUNDLE", label)
        tree.links.new(enabled.outputs["Output"], cloth_bundle.inputs[label])

    post = tree.nodes.new("GeometryNodeGroup")
    post.node_tree = builtin["Evaluate Custom Effectors"]
    post.inputs["Stage"].default_value = "POST_SOLVE"
    post.label = "Apply native post-solve tearing"
    tree.links.new(solver.outputs["World"], post.inputs["World"])
    tree.links.new(post.outputs["World"], get_geometry.inputs["Bundle"])
