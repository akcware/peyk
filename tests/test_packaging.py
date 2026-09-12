import subprocess
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).parents[1]


def test_wheel_contains_database_migrations(tmp_path):
    result = subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheel = next(tmp_path.glob("*.whl"))
    with ZipFile(wheel) as archive:
        packaged = {
            Path(name).name
            for name in archive.namelist()
            if name.startswith("db/migrations/") and name.endswith(".sql")
        }
    expected = {path.name for path in (ROOT / "db/migrations").glob("*.sql")}

    assert expected
    assert packaged == expected
