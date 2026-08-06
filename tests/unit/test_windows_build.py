from __future__ import annotations

from pathlib import Path


def test_windows_build_workflow_publishes_single_file_executable() -> None:
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    spec = Path("etbc-iam-migration.spec").read_text(encoding="utf-8")
    build_requirements = set(
        Path("requirements-build-windows.lock").read_text(encoding="utf-8").splitlines()
    )

    assert "workflow_dispatch:" in workflow
    assert "runs-on: windows-2022" in workflow
    assert "python-version: \"3.12.10\"" in workflow
    assert "python -m PyInstaller --clean --noconfirm etbc-iam-migration.spec" in workflow
    assert "dist/etbc-iam-migrate.exe" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "if-no-files-found: error" in workflow
    assert 'name="etbc-iam-migrate"' in spec
    assert 'collect_data_files("etbc_migration")' in spec
    assert 'collect_data_files("tzdata")' in spec
    assert {
        "altgraph==0.17.5",
        "pefile==2024.8.26",
        "pyinstaller==6.21.0",
        "pyinstaller-hooks-contrib==2026.6",
        "pywin32-ctypes==0.2.3",
    } <= build_requirements


def test_windows_launcher_uses_the_same_cli_entrypoint() -> None:
    launcher = Path("packaging/windows_entry.py").read_text(encoding="utf-8")

    assert "from etbc_migration.cli import main" in launcher
    assert "raise SystemExit(main())" in launcher
