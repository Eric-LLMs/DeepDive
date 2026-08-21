"""Vocab isolation: public + private domains, per-owner dedup scope, copy-on-import clone."""
from uuid import uuid4

import pytest

from core.application.services import VocabError
from tests._vocab_fakes import make_vocab


async def test_public_domains_visible_to_everyone(tmp_path):
    svc, *_ = make_vocab()
    a, b = uuid4(), uuid4()
    await svc.add_domain("CET4")  # public

    names_a = {d.name for d in await svc.list_domains(a)}
    names_b = {d.name for d in await svc.list_domains(b)}
    names_guest = {d.name for d in await svc.list_domains(None)}
    assert names_a == names_b == names_guest == {"CET4"}


async def test_private_domain_hidden_from_others(tmp_path):
    svc, *_ = make_vocab()
    a, b = uuid4(), uuid4()
    await svc.add_domain("私密词库", user_id=a)

    assert [d.name for d in await svc.list_domains(a)] == ["私密词库"]
    assert await svc.list_domains(b) == []
    assert await svc.list_domains(None) == []

    private = (await svc.list_domains(a))[0]
    with pytest.raises(VocabError) as e:
        await svc.list_terms(private.id, user_id=b)
    assert e.value.status_code == 403


async def test_domain_name_dedup_scoped_per_owner(tmp_path):
    svc, domains, *_ = make_vocab()
    a, b = uuid4(), uuid4()

    await svc.add_domain("CET4")
    await svc.add_domain("CET4", user_id=a)
    await svc.add_domain("CET4", user_id=b)

    assert len(domains.rows) == 3
    assert len(await svc.list_domains(a)) == 2  # public + own
    assert len(await svc.list_domains(None)) == 1  # public only


async def test_sentence_dedup_scoped_per_owner(tmp_path):
    svc, _, _, sentences, _ = make_vocab()
    a, b = uuid4(), uuid4()
    d_a = await svc.add_domain("A", user_id=a)
    d_b = await svc.add_domain("B", user_id=b)

    await svc.add_sentence(d_a.id, "the same sentence", user_id=a)
    await svc.add_sentence(d_b.id, "the same sentence", user_id=b)

    # Two owners may each own the same sentence; each domain sees its own row.
    assert len(sentences.rows) == 2
    assert len(await svc.list_sentences(d_a.id, user_id=a)) == 1
    assert len(await svc.list_sentences(d_b.id, user_id=b)) == 1


async def test_guest_cannot_add_term_to_private_domain(tmp_path):
    svc, *_ = make_vocab()
    a = uuid4()
    private = await svc.add_domain("mine", user_id=a)

    with pytest.raises(VocabError) as e:
        await svc.add_term(private.id, "apple", user_id=None)
    assert e.value.status_code == 403


async def test_clone_public_domain_creates_private_copy(tmp_path):
    svc, domains, terms, sentences, matches = make_vocab()
    owner, cloner = uuid4(), uuid4()

    src = await svc.add_domain("CET4")  # public
    await svc.import_terms_structured(src.id, [("apple", "苹果", 3, 2)])
    await svc.import_sentences_structured(src.id, ["An apple a day keeps the doctor away."])
    # Auto-link should have created a match for "apple" → that sentence.
    src_term = (await svc.list_terms(src.id))[0]
    assert await matches.list_for_term(src_term.id)

    clone = await svc.clone_domain(cloner, src.id)
    assert clone.user_id == cloner
    assert clone.id != src.id

    # The clone is private to the cloner (who also still sees the public source).
    assert {d.id for d in await svc.list_domains(cloner)} == {src.id, clone.id}
    assert {d.id for d in await svc.list_domains(owner)} == {src.id}

    # Deep copy: same terms + sentences + matches in the new domain.
    clone_terms = await svc.list_terms(clone.id, user_id=cloner)
    assert [t.word for t in clone_terms] == ["apple"]
    clone_sentences = await svc.list_sentences(clone.id, user_id=cloner)
    assert len(clone_sentences) == 1
    assert clone_sentences[0].content_en.startswith("An apple a day")
    # Matches recreated against the cloned term.
    assert await matches.list_for_term(clone_terms[0].id)
