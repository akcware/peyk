"""Real Titan embeddings (pytest -m llm)."""
from __future__ import annotations

import pytest

from core.embeddings import TitanEmbedder
from core.repo import memory_repo
from tests.conftest import USER_ID

pytestmark = pytest.mark.llm


async def test_memory_search_returns_relevant(conn, settings):
    emb = TitanEmbedder(region=settings.AWS_REGION)
    facts = ["Uses Postgres with pgvector in the proactive-agent project", "Lives in Berlin near Prenzlauer Berg",
             "Prefers meetings in the morning before 11", "Client Mara Lindqvist usually pays invoices late",
             "Daughter goes to Kita Sonnenschein on weekdays"]
    for f in facts:
        await memory_repo.insert(conn, USER_ID, f, await emb.embed(f))
    hits = await memory_repo.search(conn, USER_ID, await emb.embed("hangi projede Postgres kullanıyorum"), k=3)
    assert hits[0]["text"].startswith("Uses Postgres"), [h["text"] for h in hits]
    hits = await memory_repo.search(conn, USER_ID, await emb.embed("when do I like to have meetings?"), k=1)
    assert "morning" in hits[0]["text"]
