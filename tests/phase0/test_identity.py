from __future__ import annotations

import pytest

from core.identity import display_name_from_header, normalize_email, normalize_phone
from core.repo import identity_repo
from tests.conftest import USER_ID


def test_identity_normalize_pure():
    assert normalize_email("Foo@Bar.com ") == "foo@bar.com"
    assert normalize_email("Mara Lindqvist <Mara@Example.test>") == "mara@example.test"
    assert display_name_from_header("Mara Lindqvist <mara@example.test>") == "Mara Lindqvist"
    assert normalize_phone("+49 176 1234567") == "+491761234567"
    assert normalize_phone("017612345 67", "DE") == "+491761234567"
    with pytest.raises(ValueError):
        normalize_phone("not a number")


async def test_identity_normalize(conn):
    p1 = await identity_repo.resolve(conn, USER_ID, "phone", "+49 176 1234567")
    p2 = await identity_repo.resolve(conn, USER_ID, "phone", "017612345 67", default_region="DE")
    assert p1 == p2
    e1 = await identity_repo.resolve(conn, USER_ID, "email", "Foo@Bar.com ", display_name="Foo")
    e2 = await identity_repo.resolve(conn, USER_ID, "email", "foo@bar.com")
    assert e1 == e2 and e1 != p1
    cur = await conn.execute("select kind, value from identity where user_id = %s order by kind", (USER_ID,))
    rows = [(r["kind"], r["value"]) for r in await cur.fetchall()]
    assert rows == [("email", "foo@bar.com"), ("phone", "+491761234567")]
    cur = await conn.execute("select display_name from person where id = %s", (e1,))
    assert (await cur.fetchone())["display_name"] == "Foo"
