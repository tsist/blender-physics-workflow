"""Contract tests for rigid-scatter activation_frame."""

from __future__ import annotations

from pathlib import Path
import sys
import tomllib
import unittest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from physics_nodes_workflow_addon import capabilities, contract  # noqa: E402


class TestActivationFrameContract(unittest.TestCase):
    def test_all_presets_default_to_zero(self) -> None:
        for preset_id in contract.PRESET_DEFINITIONS:
            with self.subTest(preset_id=preset_id):
                self.assertEqual(contract.PRESET_DEFINITIONS[preset_id]["defaults"]["activation_frame"], 0)
                request = contract.normalize_request({"preset_id": preset_id, "operation": "build"})
                self.assertEqual(request["parameters"]["activation_frame"], 0)

    def test_rigid_accepts_absolute_frames_without_frame_end_clamping(self) -> None:
        for value in (0, 1, 5, 100000):
            with self.subTest(value=value):
                request = contract.normalize_request(
                    {
                        "preset_id": "rigid_drop_pack",
                        "parameters": {"activation_frame": value},
                    }
                )
                self.assertEqual(request["parameters"]["activation_frame"], value)

        before_start = contract.normalize_request(
            {
                "preset_id": "rigid_drop_pack",
                "parameters": {"frame_start": 10, "frame_end": 20, "activation_frame": 1},
            }
        )
        self.assertEqual(before_start["parameters"]["activation_frame"], 1)

        after_end = contract.normalize_request(
            {
                "preset_id": "rigid_drop_pack",
                "parameters": {"frame_start": 10, "frame_end": 20, "activation_frame": 21},
            }
        )
        self.assertEqual(after_end["parameters"]["activation_frame"], 21)

    def test_activation_frame_rejects_non_strict_integers(self) -> None:
        bad_values = (
            True,
            False,
            0.0,
            1.0,
            1.5,
            -1,
            100001,
            float("inf"),
            float("-inf"),
            float("nan"),
            "5",
            None,
            [5],
            {"frame": 5},
        )
        for value in bad_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(contract.PhysicsContractError) as ctx:
                    contract.normalize_request(
                        {
                            "preset_id": "rigid_drop_pack",
                            "parameters": {"activation_frame": value},
                        }
                    )
                self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")
                self.assertIn("activation_frame", ctx.exception.message)

    def test_non_rigid_domains_only_allow_zero(self) -> None:
        for preset_id in ("cloth_banner_wind", "rope_catenary_swing", "fluid_fountain_sdf", "smoke_plume_volume"):
            with self.subTest(preset_id=preset_id):
                accepted = contract.normalize_request(
                    {"preset_id": preset_id, "parameters": {"activation_frame": 0}}
                )
                self.assertEqual(accepted["parameters"]["activation_frame"], 0)
                with self.assertRaises(contract.PhysicsContractError) as ctx:
                    contract.normalize_request(
                        {"preset_id": preset_id, "parameters": {"activation_frame": 1}}
                    )
                self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

    def test_activation_frame_changes_parameters_hash(self) -> None:
        base = {
            "operation": "build",
            "preset_id": "rigid_drop_pack",
            "target_object": "Target",
        }
        default = contract.normalize_request(base)
        immediate = contract.normalize_request({**base, "parameters": {"activation_frame": 0}})
        delayed = contract.normalize_request({**base, "parameters": {"activation_frame": 6}})
        other_delayed = contract.normalize_request({**base, "parameters": {"activation_frame": 7}})

        self.assertEqual(default["parameters_hash"], immediate["parameters_hash"])
        self.assertNotEqual(default["parameters_hash"], delayed["parameters_hash"])
        self.assertNotEqual(delayed["parameters_hash"], other_delayed["parameters_hash"])
        self.assertEqual(
            delayed["parameters_hash"],
            contract.normalize_request({**base, "parameters": {"activation_frame": 6}})["parameters_hash"],
        )

    def test_capability_declares_only_whole_group_activation(self) -> None:
        described = capabilities.describe_capabilities()
        self.assertEqual(
            described["rigid_activation"],
            "whole_workflow_at_scene_frame_zero_immediate",
        )
        manifest = tomllib.loads((TOOLS_DIR / "physics_nodes_workflow_addon" / "blender_manifest.toml").read_text(encoding="utf-8"))
        self.assertEqual(described["implementation_version"], manifest["version"])
        self.assertEqual(
            contract.describe_contract()["capabilities"]["rigid_activation"],
            "whole_workflow_at_scene_frame_zero_immediate",
        )


if __name__ == "__main__":
    unittest.main()
