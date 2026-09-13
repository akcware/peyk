"""Consumers and people: a busy person's next message waits in the queue instead of parking a consumer on their lock."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from core.models import Observation
from core.repo import observation_repo
from tests.conftest import USER_ID
from workers import triage

OTHER = UUID("00000000-0000-4000-8000-000000000002")


def _obs(key: str, user_id: UUID = USER_ID) -> Observation:
    return Observation(user_id=user_id, source="telegram", source_key=key, kind="message_in",
                       occurred_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC), thread_key=str(user_id), payload={"text": key})


async def test_busy_person_does_not_hold_the_other_consumers(pool):
    async with pool.connection() as c:
        for key, uid in (("mine-1", USER_ID), ("mine-2", USER_ID), ("mine-3", USER_ID), ("theirs", OTHER)):
            await observation_repo.insert(c, _obs(key, uid))
    try:
        first = await triage.claim_for_consumer()
        second = await triage.claim_for_consumer()
        assert {first.user_id, second.user_id} == {USER_ID, OTHER}   # the other person is served at once
        assert await triage.claim_for_consumer() is None            # my next messages stay `new` for my next turn
        triage._busy.discard(USER_ID)                               # my turn ends
        assert (await triage.claim_for_consumer()).user_id == USER_ID
    finally:
        triage._busy.clear()
