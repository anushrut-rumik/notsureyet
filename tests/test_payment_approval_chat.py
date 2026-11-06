"""Plain chat approval remains bound to the exact review and cannot send twice."""

import pytest

from agent.arkiv import ArkivStore
from agent.chat_commands import ChatCommands
from agent.payments import prepare_request
from agent.runtime import Runtime
from tests.test_agent import Script
from tests.test_mobile_wallet import connect
from tests.test_web3 import Chain, MemoryArkiv
from tests.test_web3 import config as config


@pytest.fixture
async def app(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    runtime = Runtime(config, Script())
    runtime.store.close()
    runtime.store = ArkivStore(config.db_path, config, MemoryArkiv())
    runtime.engine.store = runtime.store
    runtime.wallets.store = runtime.store
    runtime.recovered = False
    runtime.recover()
    runtime.wallets.create("u")
    connect(runtime.wallets)
    chain = Chain()
    chain.chain = 84532
    runtime.wallets.base_rpc = chain
    runtime.test_chain = chain
    try:
        yield runtime
    finally:
        await runtime.close()


async def show(app, amount="0.005"):
    payment = await prepare_request(
        app.wallets, "u", asset="ETH", amount=amount, source_network="base_sepolia", destination_network="base_sepolia"
    )
    app.store.notice("u", ChatCommands.review(payment))
    return payment


@pytest.mark.parametrize("command", ["approve", "Approve it", "approve this!", "/approve"])
async def test_plain_approval_selects_latest_review_and_repeated_approval_never_sends(app, command):
    first = await show(app, "0.001")
    latest = await show(app)
    reply = await app.chat("u", command)
    assert "Open MetaMask" in reply
    assert f"Payment ID: {latest['id']}" in reply
    assert "https://wallet.example/payment/" in reply
    assert app.wallets.load("u", first["id"])["status"] == "awaiting_approval"
    assert app.wallets.load("u", latest["id"])["status"] == "awaiting_signature"
    assert "Open MetaMask" in await app.chat("u", "approve")
    assert not any(method.startswith("eth_send") for method, _ in app.test_chain.calls)
    assert not app.models.calls


async def test_zero_letter_typo_and_whitespace_are_unambiguous(app):
    payment = await show(app)
    payment = app.wallets.load("u", payment["id"])
    payment["approval_code"] = "89F0642F0F05"
    app.wallets.save(payment)
    assert "Open MetaMask" in await app.chat("u", "Approve\u00a0 89F0642FOFO5")
    assert not app.models.calls


async def test_expired_plain_approval_refreshes_same_payment_without_approving(app):
    payment = await show(app)
    old = app.wallets.load("u", payment["id"])
    old["expires_at"] = 0
    app.wallets.save(old)
    reply = await app.chat("u", "approve")
    assert "review expired" in reply
    fresh = app.wallets.load("u", payment["id"])
    assert fresh["status"] == "awaiting_approval"
    assert fresh["approval_code"] != old["approval_code"]
    assert len(app.wallets.list("u")) == 1
    assert not app.store.db.execute("SELECT 1 FROM events WHERE kind='payment_approval'").fetchone()
    assert "Open MetaMask" in await app.chat("u", "approve")
    assert not app.models.calls


async def test_missing_ambiguous_or_other_chat_review_cannot_approve(app):
    await show(app)
    assert "clear payment review" in await app.chat("other", "approve")
    app.store.notice("u", "approve AAAAAAAAAAAA or approve BBBBBBBBBBBB")
    assert "clear payment review" in await app.chat("u", "approve")
    assert "This message did not authorize" in await app.chat("u", "approve nonsense")
    assert not app.store.db.execute("SELECT 1 FROM events WHERE kind='payment_approval'").fetchone()
    assert not app.models.calls


async def test_expired_unsigned_link_refreshes_same_payment_on_plain_approve(app):
    payment = await show(app)
    await app.chat("u", "approve")
    old = app.wallets.load("u", payment["id"])
    old["expires_at"] = 0
    app.wallets.save(old)
    reply = await app.chat("u", "approve")
    assert "review expired" in reply
    fresh = app.wallets.load("u", payment["id"])
    assert fresh["status"] == "awaiting_approval" and fresh["approval_code"] != old["approval_code"]
    assert len(app.wallets.list("u")) == 1
    assert "Open MetaMask" in await app.chat("u", "approve")
    assert not any(method.startswith("eth_send") for method, _ in app.test_chain.calls)


async def test_newer_lending_review_does_not_approve_older_payment(app):
    await show(app)
    app.store.notice("u", "Review this plan. Reply approve lending plan 1 abcdef123456")
    assert "clear payment review" in await app.chat("u", "approve")
    assert not app.store.db.execute("SELECT 1 FROM events WHERE kind='payment_approval'").fetchone()
