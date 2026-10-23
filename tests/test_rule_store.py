import os
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from obs_tool.rule_store import (
    store_path,
    load_rules,
    normalized_name,
    validate_name,
    next_name,
    name_conflicts,
    save_rules,
)


class TestRuleStore(unittest.TestCase):
    def test_store_path_env_and_default(self):
        with patch.dict(os.environ, {"OBS_SETTINGS_DIR": "/custom/path"}):
            self.assertEqual(store_path(), Path("/custom/path/layer_rules.json"))

        with patch.dict(os.environ, {"LOCALAPPDATA": "/local/appdata"}):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OBS_SETTINGS_DIR", None)
                self.assertEqual(
                    store_path(), Path("/local/appdata/OBS工具/layer_rules.json")
                )

        with patch.object(Path, "home", return_value=Path("/home/user")):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OBS_SETTINGS_DIR", None)
                os.environ.pop("LOCALAPPDATA", None)
                self.assertEqual(
                    store_path(), Path("/home/user/OBS工具/layer_rules.json")
                )

    def test_load_rules_non_existent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            non_existent = Path(tmpdir) / "missing.json"
            self.assertEqual(load_rules(non_existent), [])

    def test_load_rules_invalid_schema_or_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "rules.json"

            # Non-dict structure
            p.write_text(json.dumps(["not", "a", "dict"]))
            with self.assertRaisesRegex(ValueError, "自定义规则文件版本不支持"):
                load_rules(p)

            # Invalid schema version
            p.write_text(json.dumps({"schema_version": 2, "rules": []}))
            with self.assertRaisesRegex(ValueError, "自定义规则文件版本不支持"):
                load_rules(p)

            # Invalid rules type
            p.write_text(json.dumps({"schema_version": 1, "rules": "not-a-list"}))
            with self.assertRaisesRegex(ValueError, "规则库结构无效"):
                load_rules(p)

    def test_load_rules_capacity_and_rule_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "rules.json"

            # More than 256 rules
            too_many = [
                {"name": f"Rule{i}", "positions": [0.2, 0.8]} for i in range(257)
            ]
            p.write_text(json.dumps({"schema_version": 1, "rules": too_many}))
            with self.assertRaisesRegex(ValueError, "保存的规则数"):
                load_rules(p)

            # Invalid rule name (not str or empty)
            invalid_name = [{"name": 123, "positions": [0.2, 0.8]}]
            p.write_text(json.dumps({"schema_version": 1, "rules": invalid_name}))
            with self.assertRaisesRegex(ValueError, "规则名称无效"):
                load_rules(p)

            empty_name = [{"name": "   ", "positions": [0.2, 0.8]}]
            p.write_text(json.dumps({"schema_version": 1, "rules": empty_name}))
            with self.assertRaisesRegex(ValueError, "规则名称无效"):
                load_rules(p)

            # Invalid positions
            invalid_pos = [{"name": "ValidName", "positions": [0.8, 0.2]}]
            p.write_text(json.dumps({"schema_version": 1, "rules": invalid_pos}))
            with self.assertRaises(ValueError):
                load_rules(p)

    def test_normalized_name(self):
        self.assertEqual(normalized_name("  Test  Name  "), "testname")
        # Unicode normalization NFKC: full-width １２３ -> 123
        self.assertEqual(normalized_name("规则１"), "规则1")
        self.assertEqual(normalized_name("Rule A"), "rulea")

    def test_validate_name(self):
        # Empty / whitespace name
        with self.assertRaisesRegex(ValueError, "请填写规则名称"):
            validate_name("   ", [])

        # Reserved default names
        reserved_names = [
            "默认规则",
            "默认规则(0.2层)",
            "按行业规范测深",
            "系统规则方案",
            "根据水深自动选择",
            "恢复按水深自动选择",
        ]
        for name in reserved_names:
            with self.assertRaisesRegex(
                ValueError, "这个名称留给默认规则，请换一个名称"
            ):
                validate_name(name, [])

        # Duplicate name
        existing_rules = [{"name": "方案一", "positions": [0.2, 0.8]}]
        with self.assertRaisesRegex(ValueError, "已有同名规则，请换一个名称"):
            validate_name(" 方案 一 ", existing_rules)

        # Valid name
        self.assertEqual(validate_name("  新规则  ", existing_rules), "新规则")

    def test_next_name(self):
        self.assertEqual(next_name([]), "自定义规则1")

        rules = [
            {"name": "自定义规则1", "positions": [0.2]},
            {"name": "自定义规则2", "positions": [0.2]},
        ]
        self.assertEqual(next_name(rules), "自定义规则3")

        # Handles normalized match
        rules_normalized = [{"name": " 自定义 规则 1 ", "positions": [0.2]}]
        self.assertEqual(next_name(rules_normalized), "自定义规则2")

    def test_name_conflicts(self):
        rules = [
            {"name": "合法规则1", "positions": [0.2]},
            {"name": "默认规则", "positions": [0.2]},
            {"name": "合法规则1", "positions": [0.2]},
        ]
        conflicts = name_conflicts(rules)
        self.assertEqual(len(conflicts), 2)
        self.assertEqual(conflicts[0][0], 1)
        self.assertIn("这个名称留给默认规则", conflicts[0][1])
        self.assertEqual(conflicts[1][0], 2)
        self.assertIn("已有同名规则", conflicts[1][1])

    def test_save_rules_validation_backup_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "layer_rules.json"

            # Invalid positions or name in save_rules
            invalid_rules = [{"name": "默认规则", "positions": [0.2]}]
            with self.assertRaises(ValueError):
                save_rules(invalid_rules, file_path)

            valid_rules = [{"name": "自定义1", "positions": [0.2, 0.8]}]
            save_rules(valid_rules, file_path)
            self.assertTrue(file_path.exists())
            self.assertEqual(load_rules(file_path), valid_rules)

            # Saving again when file exists should create a backup file
            updated_rules = [
                {"name": "自定义1", "positions": [0.2, 0.8]},
                {"name": "自定义2", "positions": [0.1, 0.5, 0.9]},
            ]
            save_rules(updated_rules, file_path)

            backups = list(file_path.parent.glob("layer_rules.json.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(load_rules(file_path), updated_rules)


if __name__ == "__main__":
    unittest.main()
