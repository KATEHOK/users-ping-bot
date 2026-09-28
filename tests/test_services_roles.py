import pytest

from app.db import Database
from app.models import Role
from app.services import Services


async def test_grant_admin_created(db: Database):
    services = Services()
    async with db.transaction() as c:
        result = await services.grant_admin(c, 5)
    assert result.status == "created"
    async with db.reader() as c:
        assert await services.get_role(c, 5) is Role.ADMIN


async def test_grant_admin_by_id_before_first_contact(db: Database):
    # plan section 5: admin can be granted before the user ever wrote to the bot
    services = Services()
    async with db.transaction() as c:
        result = await services.grant_admin(c, 777)
    assert result.status == "created"


async def test_grant_admin_exists_is_noop(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.grant_admin(c, 5)
        second = await services.grant_admin(c, 5)
    assert second.status == "exists"
    async with db.reader() as c:
        assert await services.get_role(c, 5) is Role.ADMIN


async def test_grant_admin_on_root_is_noop(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.set_root(c, 1)
        result = await services.grant_admin(c, 1)
    assert result.status == "is_root"
    async with db.reader() as c:
        assert await services.get_role(c, 1) is Role.ROOT


async def test_revoke_admin_not_admin_is_noop(db: Database):
    services = Services()
    async with db.transaction() as c:
        result = await services.revoke_admin(c, 42)
    assert result.revoked is False
    assert result.chat_ids == []


async def test_revoke_admin_clears_all_chats_and_subscriptions_but_not_foreign_ones(
    db: Database,
):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)  # admin A
        await services.touch_user(c, 2)  # subscriber, and other registrar
        await services.grant_admin(c, 1)
        await services.register_chat(c, -100, "A chat 1", 1)
        await services.register_chat(c, -200, "A chat 2", 1)
        await services.register_chat(c, -300, "Foreign chat", 2)
        await services.subscribe(c, -100, 2)
        await services.subscribe(c, -300, 1)  # A subscribed in someone else's chat

    async with db.transaction() as c:
        result = await services.revoke_admin(c, 1)

    assert result.revoked is True
    assert sorted(result.chat_ids) == [-200, -100]

    async with db.reader() as c:
        assert await services.get_role(c, 1) is None
        assert await services.get_chat(c, -100) is None
        assert await services.get_chat(c, -200) is None
        foreign = await services.get_chat(c, -300)
        assert foreign is not None  # chats registered by others survive
        assert await services.is_subscribed(c, -300, 1)  # A's own sub survives


async def test_remove_chat_removes_only_that_chat_and_keeps_registrar_admin(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.touch_user(c, 2)
        await services.grant_admin(c, 1)
        await services.register_chat(c, -100, "chat1", 1)
        await services.register_chat(c, -200, "chat2", 1)
        await services.subscribe(c, -100, 2)
        await services.subscribe(c, -200, 2)

    async with db.transaction() as c:
        result = await services.remove_chat(c, -100)

    assert result.chat_ids == [-100]
    async with db.reader() as c:
        assert await services.get_role(c, 1) is Role.ADMIN  # registrar untouched
        assert await services.get_chat(c, -100) is None
        assert await services.get_chat(c, -200) is not None
        assert not await services.is_subscribed(c, -100, 2)  # subscriptions went with the chat
        assert await services.is_subscribed(c, -200, 2)
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert [(e.event_type, e.target_id) for e in events] == [("chat_farewell", -100)]


async def test_remove_chat_registered_by_root_keeps_root(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.set_root(c, 1)
        await services.register_chat(c, -100, "chat1", 1)
        await services.register_chat(c, -200, "chat2", 1)

    async with db.transaction() as c:
        result = await services.remove_chat(c, -100)

    assert result.chat_ids == [-100]
    async with db.reader() as c:
        assert await services.get_role(c, 1) is Role.ROOT
        assert await services.get_chat(c, -200) is not None


async def test_remove_chat_unknown_is_noop_and_follows_alias(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, -100, "chat", 1)
        assert (await services.remove_chat(c, -999)).chat_ids == []
        await services.migrate_chat(c, -100, -200)

    async with db.transaction() as c:
        result = await services.remove_chat(c, -100)  # old id of a migrated group
    assert result.chat_ids == [-200]
    async with db.reader() as c:
        assert await services.get_chat(c, -200) is None


async def test_revoke_admin_farewell_events_carry_each_chat_language(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.grant_admin(c, 1)
        await services.register_chat(c, -100, "a", 1)
        await services.register_chat(c, -200, "b", 1)
        await services.set_chat_lang(c, -200, "ru")
        await services.revoke_admin(c, 1)
    async with db.reader() as c:
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert {e.target_id: e.payload["lang"] for e in events} == {-100: "en", -200: "ru"}


async def test_load_actor_is_chat_owner_matrix(db: Database):
    services = Services()
    async with db.transaction() as c:
        for uid in (1, 2, 3, 4, 5):
            await services.touch_user(c, uid)
        await services.set_root(c, 1)  # root
        await services.grant_admin(c, 2)  # registrar admin
        await services.grant_admin(c, 3)  # foreign admin
        await services.register_chat(c, -100, "chat", 2)
        await services.subscribe(c, -100, 4)
        await services.subscribe(c, -100, 3)

    async with db.reader() as c:
        owner_flags = {
            uid: (await services.load_actor(c, uid, chat_id=-100)).is_chat_owner
            for uid in (1, 2, 3, 4, 5)
        }
        subscriber = await services.load_actor(c, 4, chat_id=-100)
        foreign = await services.load_actor(c, 3, chat_id=-100)
        inactive = {
            uid: await services.load_actor(c, uid, chat_id=-555) for uid in (1, 2, 3, 4)
        }
        no_chat = await services.load_actor(c, 2)

    assert owner_flags == {1: True, 2: True, 3: False, 4: False, 5: False}
    assert subscriber.is_subscriber and not subscriber.is_chat_owner
    assert foreign.role is Role.ADMIN and foreign.is_subscriber and not foreign.is_chat_owner
    assert not any(a.is_chat_owner or a.is_subscriber for a in inactive.values())
    assert inactive[1].role is Role.ROOT
    assert not no_chat.is_chat_owner


async def test_load_actor_registrar_who_lost_admin_is_not_owner(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 2)
        await services.register_chat(c, -100, "chat", 2)  # registrar without a role
    async with db.reader() as c:
        actor = await services.load_actor(c, 2, chat_id=-100)
    assert actor.role is None and not actor.is_chat_owner


async def test_set_root_same_id_is_noop(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.set_root(c, 1)
        await services.register_chat(c, -100, "chat", 1)

    async with db.transaction() as c:
        result = await services.set_root(c, 1)

    assert result.changed is False
    assert result.dropped_chat_ids == []
    async with db.reader() as c:
        assert await services.get_chat(c, -100) is not None
        assert await services.get_role(c, 1) is Role.ROOT


async def test_set_root_promotes_admin_and_keeps_his_chats(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)  # old root A
        await services.touch_user(c, 2)  # admin B, to be promoted
        await services.set_root(c, 1)
        await services.grant_admin(c, 2)
        await services.register_chat(c, -100, "A's chat", 1)
        await services.register_chat(c, -200, "B's chat", 2)

    async with db.transaction() as c:
        result = await services.set_root(c, 2)

    assert result.changed is True
    assert result.previous_root_id == 1
    assert result.dropped_chat_ids == [-100]

    async with db.reader() as c:
        assert await services.get_role(c, 1) is None  # A lost his role entirely
        assert await services.get_role(c, 2) is Role.ROOT
        assert await services.get_chat(c, -100) is None  # A's registration dropped
        kept = await services.get_chat(c, -200)
        assert kept is not None  # B's own chat kept across the promotion
        assert kept.registered_by == 2


async def test_set_root_notified_previous_only_with_recorded_contact(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1, private_contact=True)
        await services.set_root(c, 1)

    async with db.transaction() as c:
        result = await services.set_root(c, 2)
    assert result.notified_previous is True

    # second promotion: new previous root (2) never had private contact
    async with db.transaction() as c:
        await services.touch_user(c, 3)
        result2 = await services.set_root(c, 3)
    assert result2.notified_previous is False


async def test_set_root_never_transiently_violates_single_root_invariant(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.set_root(c, 1)

    # a successful set_root call itself proves the ordering is safe (the
    # roles_single_root partial unique index would raise IntegrityError
    # if the old root row were not removed before the new one is inserted)
    async with db.transaction() as c:
        await services.set_root(c, 2)

    async with db.reader() as c:
        cursor = await c.execute("SELECT COUNT(*) FROM roles WHERE role = 'root'")
        (count,) = await cursor.fetchone()
    assert count == 1


async def test_failing_transaction_leaves_no_partial_cascade(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.grant_admin(c, 1)
        await services.register_chat(c, -100, "chat1", 1)
        await services.register_chat(c, -200, "chat2", 1)

    with pytest.raises(RuntimeError):
        async with db.transaction() as c:
            await services.revoke_admin(c, 1)
            raise RuntimeError("boom mid-cascade")

    async with db.reader() as c:
        assert await services.get_role(c, 1) is Role.ADMIN
        assert await services.get_chat(c, -100) is not None
        assert await services.get_chat(c, -200) is not None


async def test_claim_update_duplicate_returns_false_and_effect_not_rerun(db: Database):
    services = Services()

    async def process_once(bot_id, update_id):
        async with db.transaction() as c:
            claimed = await services.claim_update(c, bot_id, update_id)
            if not claimed:
                return False
            await services.touch_user(c, 1)
            await services.grant_admin(c, 1)
            return True

    first = await process_once(1, 1000)
    assert first is True
    second = await process_once(1, 1000)
    assert second is False

    async with db.reader() as c:
        cursor = await c.execute("SELECT COUNT(*) FROM roles WHERE user_id = 1")
        (count,) = await cursor.fetchone()
    assert count == 1  # grant_admin's effect ran exactly once


async def test_set_root_drops_previous_root_chats_with_farewells_and_subscriptions(
    db: Database,
):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.touch_user(c, 5)
        await services.set_root(c, 1)
        await services.register_chat(c, -100, "A's chat", 1)
        await services.subscribe(c, -100, 5)
        await services.set_root(c, 2)
    async with db.reader() as c:
        assert await services.list_subscribers(c, -100) == []
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert [(e.event_type, e.target_id) for e in events] == [("chat_farewell", -100)]


async def test_claim_update_records_outcome_and_prune_deletes_old_rows(db: Database):
    from datetime import datetime, timedelta, timezone

    from app.clock import iso
    from conftest import FakeClock

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    services = Services(clock=clock)
    async with db.transaction() as c:
        assert await services.claim_update(c, 1, 1, "ignored")
        assert await services.claim_update(c, 1, 2, "error")
    clock.advance(3 * 86400)
    async with db.transaction() as c:
        assert await services.claim_update(c, 1, 3)
        assert not await services.claim_update(c, 1, 3)

    cutoff = iso(clock.now() - timedelta(days=2))
    async with db.transaction() as c:
        deleted = await services.prune_processed_updates(c, older_than=cutoff)
    assert deleted == 2
    async with db.reader() as c:
        cursor = await c.execute("SELECT update_id, outcome FROM processed_updates")
        assert await cursor.fetchall() == [(3, "ok")]
    async with db.transaction() as c:
        # a pruned update id is claimable again
        assert await services.claim_update(c, 1, 1, "ignored")
        assert await services.prune_processed_updates(c, older_than=cutoff) == 0
