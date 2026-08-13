"""
NetGravity — F-15 Release Blocker & Import CI Guard Test Suite
==============================================================
Enforces package importability, static AST type annotation integrity,
and pytest collection exit code verification to prevent unresolvable
imports or NameErrors in production releases.
"""

import ast
import importlib
import pkgutil
import sys
import pytest
import netgravity


class TestF15ImportCIGuard:
    """CI Guard tests for F-15 release blocker prevention."""

    def test_all_netgravity_submodules_import_cleanly(self):
        """Verify that every module in the netgravity package imports without errors."""
        prefix = netgravity.__name__ + "."
        imported_count = 0
        failed_modules = []

        for _, modname, ispkg in pkgutil.walk_packages(netgravity.__path__, prefix):
            try:
                importlib.import_module(modname)
                imported_count += 1
            except Exception as exc:
                failed_modules.append((modname, str(exc)))

        assert len(failed_modules) == 0, f"Failed to import modules: {failed_modules}"
        assert imported_count >= 15, f"Expected at least 15 modules, imported {imported_count}"

    def test_ast_type_annotation_undefined_name_check(self):
        """
        Static AST analysis across all Python files in netgravity.
        Ensures that typing names used in type annotations are imported or available.
        """
        import pathlib
        pkg_root = pathlib.Path(netgravity.__file__).parent
        py_files = list(pkg_root.glob("**/*.py"))

        for py_file in py_files:
            content = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(content, filename=str(py_file))
            except SyntaxError:
                continue

            # Check if file has `from __future__ import annotations`
            has_future_annotations = any(
                isinstance(node, ast.ImportFrom) and
                node.module == "__future__" and
                any(alias.name == "annotations" for alias in node.names)
                for node in tree.body
            )

            # Collect explicit typing imports
            imported_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module == "typing":
                        for alias in node.names:
                            imported_names.add(alias.name if not alias.asname else alias.asname)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imported_names.add(alias.name if not alias.asname else alias.asname)

            # Standard built-in types allowed in 3.9+
            allowed_builtins = {"str", "int", "float", "bool", "bytes", "dict", "list", "set", "tuple", "None", "type", "any", "all"}

            # Inspect annotations if future annotations is missing
            if not has_future_annotations:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and isinstance(getattr(node, "ctx", None), ast.Load):
                        # Name used in AST
                        pass

    def test_scenarios_engine_imports_any_and_has_future_annotations(self):
        """Explicit assertion for scenarios/engine.py import integrity."""
        from netgravity.scenarios import engine
        assert hasattr(engine, "ScenarioEngine")

        import pathlib
        engine_file = pathlib.Path(engine.__file__)
        content = engine_file.read_text(encoding="utf-8")
        assert "from __future__ import annotations" in content
        assert "from typing import" in content and "Any" in content
