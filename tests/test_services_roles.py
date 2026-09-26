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
        assert await services.subscription_id_of(c, -300, 1) is not None  # A's own sub survives


async def test_remove_chat_cascade_registrar_admin_revokes_and_drops_all_his_chats(
    db: Database,
):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.grant_admin(c, 1)
        await services.register_chat(c, -100, "chat1", 1)
        await services.register_chat(c, -200, "chat2", 1)

    async with db.transaction() as c:
        result = await services.remove_chat_cascade(c, -100)

    assert result.admin_demoted == 1
    assert sorted(result.chat_ids) == [-200, -100]
    async with db.reader() as c:
        assert await services.get_role(c, 1) is None
        assert await services.get_chat(c, -100) is None
        assert await services.get_chat(c, -200) is None


async def test_remove_chat_cascade_registrar_root_removes_only_that_chat(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.set_root(c, 1)
        await services.register_chat(c, -100, "chat1", 1)
        await services.register_chat(c, -200, "chat2", 1)

    async with db.transaction() as c:
        result = await services.remove_chat_cascade(c, -100)

    assert result.admin_demoted is None
    assert result.chat_ids == [-100]
    async with db.reader() as c:
        assert await services.get_role(c, 1) is Role.ROOT  # root untouched
        assert await services.get_chat(c, -100) is None
        assert await services.get_chat(c, -200) is not None  # other chat survives


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
