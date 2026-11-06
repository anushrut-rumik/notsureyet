from types import SimpleNamespace

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from agent.arkiv import ArkivStore
from agent.wallet import Wallets
from agent.wallet_http import create_wallet_app
from tests.test_web3 import Chain, MemoryArkiv
from tests.test_web3 import config as config


@pytest.fixture
def connected(config):
    config.env["AGENT_WALLET_PUBLIC_URL"] = "https://wallet.example"
    bridge = MemoryArkiv()
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    chain = Chain()
    wallets = Wallets(config, store, chain)
    return config, store, bridge, chain, wallets


def connect(wallets, user="u", account=None):
    account = account or Account.create()
    link = wallets.mobile.issue(user)
    token = link["url"].split("/")[-1]
    challenge = wallets.mobile.challenge(token, account.address)
    signature = Account.sign_message(encode_defunct(text=challenge["message"]), account.key).signature.hex()
    wallets.mobile.connect(token, signature)
    return account, token


async def test_connection_proves_wallet_control_and_preserves_existing_agent_key(connected):
    config, store, _bridge, _chain, wallets = connected
    try:
        old = wallets.create("u")
        account, token = connect(wallets)
        assert wallets.get("u")["address"] == account.address
        assert wallets.get("u")["kind"] == "metamask"
        assert store.db.execute("SELECT address FROM wallets WHERE user_id='u'").fetchone()[0] == old["address"]
        assert list(config.data_dir.glob("wallet-*.json"))
        with pytest.raises(ValueError, match="already been used"):
            wallets.mobile.challenge(token, account.address)
        other = wallets.mobile.issue("other")["url"].split("/")[-1]
        challenge = wallets.mobile.challenge(other, account.address)
        signature = Account.sign_message(
            encode_defunct(text=challenge["message"]), Account.create().key
        ).signature.hex()
        with pytest.raises(ValueError, match="does not match"):
            wallets.mobile.connect(other, signature)
        with pytest.raises(ValueError):
            wallets.get("other")
    finally:
        await wallets.close()
        store.close()


async def test_metamask_needs_chat_approval_and_admits_only_one_wallet_request(connected):
    _config, store, bridge, chain, wallets = connected
    try:
        connect(wallets)
        task = store.create_task("u", "Pay", "Pay once", "Verified receipt")
        payment = await wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
        token = wallets.mobile.issue("u", "payment", payment["id"])["url"].split("/")[-1]
        with pytest.raises(ValueError, match="not waiting"):
            await wallets.mobile.claim(token)
        with pytest.raises(ValueError, match="explicit incoming"):
            await wallets.approve("u", payment["id"])
        event = store.event(
            "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=task["id"], source="user"
        )
        reviewed = await wallets.approve("u", payment["id"], event)
        assert reviewed["status"] == "awaiting_signature" and reviewed["confirmation_link"]
        claimed = await wallets.mobile.claim(token)
        assert claimed["transaction"] == payment["transaction"]
        assert wallets.load("u", payment["id"])["signing_requested_at"]
        assert bridge.rows[-1]["task"] == task["id"]
        with pytest.raises(ValueError, match="already started"):
            await wallets.mobile.claim(token)
        other = wallets.mobile.issue("u", "payment", payment["id"])["url"].split("/")[-1]
        with pytest.raises(ValueError, match="already started"):
            await wallets.mobile.claim(other)
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
        with pytest.raises(ValueError, match="uncertain"):
            wallets.reject("u", payment["id"])
    finally:
        await wallets.close()
        store.close()


async def test_matching_hash_and_receipt_recover_without_a_second_signature(connected):
    config, store, bridge, chain, wallets = connected
    account, _ = connect(wallets)
    task = store.create_task("u", "Pay", "Pay once", "Verified receipt")
    payment = await wallets.prepare("u", task["id"], "0x" + "12" * 20, "0.001")
    event = store.event(
        "u", "payment_approval", {"intent": wallets.fingerprint(payment)}, task_id=task["id"], source="user"
    )
    reviewed = await wallets.approve("u", payment["id"], event)
    # Both forms of the link share the same capability token.
    token = reviewed["confirmation_link"].split("/")[-1]
    await wallets.mobile.claim(token)
    payment["tx_hash"] = "0x" + "ab" * 32
    chain.confirm(payment)
    chain.transaction["value"] = "0x1"
    with pytest.raises(ValueError, match="does not match"):
        await wallets.mobile.attach(token, payment["tx_hash"])
    assert wallets.load("u", payment["id"])["tx_hash"] is None
    chain.confirm(payment)
    await wallets.mobile.attach(token, payment["tx_hash"])
    await wallets.close()
    store.close()
    config.db_path.unlink()
    store = ArkivStore(config.db_path, config, bridge)
    store.recover()
    wallets = Wallets(config, store, chain)
    try:
        assert wallets.get("u")["address"] == account.address
        assert (await wallets.status("u", payment["id"]))["status"] == "confirmed"
        with pytest.raises(ValueError, match="already started"):
            await wallets.mobile.claim(token)
        assert not any(method.startswith("eth_send") for method, _ in chain.calls)
    finally:
        await wallets.close()
        store.close()


async def test_public_wallet_service_exposes_no_chat_or_task_api_and_checks_origin(connected):
    _config, store, _bridge, _chain, wallets = connected
    try:
        app = create_wallet_app(SimpleNamespace(wallets=wallets))
        token = wallets.mobile.issue("u")["url"].split("/")[-1]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://wallet.example") as client:
            response = await client.get("/connect/" + token)
            assert response.status_code == 200 and response.headers["referrer-policy"] == "no-referrer"
            assert (await client.get("/tasks")).status_code == 404
            assert (await client.post("/chat", json={"message": "hello"})).status_code == 404
            assert (await client.get("/docs")).status_code == 404
            assert (await client.get("/api/link/not-a-real-token")).status_code == 400
            account = Account.create()
            response = await client.post(
                "/api/link/" + token + "/challenge",
                json={"address": account.address},
                headers={"Origin": "https://evil.example"},
            )
            assert response.status_code == 403
            assert (await client.get("/assets/mobile-wallet.js")).status_code == 200
    finally:
        await wallets.close()
        store.close()
