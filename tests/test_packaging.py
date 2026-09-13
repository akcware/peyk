"""The wheel must carry db/migrations/*.sql — migrate.py resolves them relative to its own file,
so a non-editable install without them starts with an empty schema."""

import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"


def test_wheel_ships_every_sql_migration(tmp_path: Path) -> None:
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr

    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        shipped = {Path(n).name for n in zf.namelist() if n.startswith("db/migrations/")}

    on_disk = {p.name for p in MIGRATIONS.glob("*.sql")}
    assert on_disk, "no migrations found on disk — test is looking in the wrong place"
    assert shipped >= on_disk, f"missing from wheel: {sorted(on_disk - shipped)}"
