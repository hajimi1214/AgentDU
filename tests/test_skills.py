from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agentdu.permissions import ReadOnlyPolicy
from agentdu.skills import SkillsExtension
from agentdu.tools import ToolRegistry


class SkillsTests(unittest.TestCase):
    def test_skill_discovery_progressive_loading_and_resource_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".agents" / "skills"
            skill_dir = root / "test-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: \"test-skill\" # metadata comment\n"
                "description: >-\n"
                "  Helps with a focused task.\n"
                "  Includes a second sentence.\n"
                "---\n"
                "Use this instruction when requested.\n",
                encoding="utf-8",
            )
            (skill_dir / "REFERENCE.md").write_text("Reference text\n", encoding="utf-8")
            extension = SkillsExtension((root,))
            registry = ToolRegistry(Path(directory), ReadOnlyPolicy())
            extension.register_tools(registry)

            async def read_tools() -> tuple[str, str, str]:
                listing = await registry.execute("list_skills", {})
                instructions = await registry.execute("load_skill", {"name": "test-skill"})
                resource = await registry.execute(
                    "read_skill_resource",
                    {"name": "test-skill", "path": "REFERENCE.md"},
                )
                return listing.text, instructions.text, resource.text

            listing, instructions, resource = asyncio.run(read_tools())

            self.assertEqual(
                json.loads(listing),
                [
                    {
                        "name": "test-skill",
                        "description": "Helps with a focused task. Includes a second sentence.",
                    }
                ],
            )
            self.assertEqual(instructions, "Use this instruction when requested.")
            self.assertEqual(resource, "Reference text\n")
            escaped = asyncio.run(
                registry.execute(
                    "read_skill_resource",
                    {"name": "test-skill", "path": "../outside.txt"},
                )
            )
            self.assertTrue(escaped.is_error)

    def test_skill_name_must_match_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / "directory-name"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: other-name\ndescription: Description\n---\nInstructions\n",
                encoding="utf-8",
            )
            extension = SkillsExtension((root,))

            with self.assertRaisesRegex(ValueError, "must match its directory"):
                extension.register_tools(ToolRegistry(root, ReadOnlyPolicy()))


if __name__ == "__main__":
    unittest.main()
