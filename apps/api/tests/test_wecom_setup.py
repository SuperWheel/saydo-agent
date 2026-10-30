"""企业微信配置页：Bot ID / Secret 写入 .env、保存后当场重连；连接状态；Secret 不回传、不进审计。"""

import time

import pytest

from app import config
from app.channels import wecom
from app.config import get_settings

from .test_wecom import FakeServer, FakeWS

BOT = "aibTestBot0123456789"
SECRET = "S3cretForLongConnection_abcdef"


@pytest.fixture
def env_file(client, tmp_path, monkeypatch):
    """临时 .env（绝不碰开发者本机的那份），去掉会盖住 .env 的环境变量，长连接换成假服务端。"""
    path = tmp_path / ".env"
    path.write_text("# 说明注释\nAPP_SECRET=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    for k in ("WECOM_BOT_ID", "WECOM_BOT_SECRET", "APP_H5_URL"):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def use_server(monkeypatch, *conns) -> FakeServer:
    server = FakeServer(*conns)
    monkeypatch.setattr(wecom, "ws_connect", server)
    return server


def wait_state(client, *states, timeout=3.0) -> dict:
    end = time.monotonic() + timeout
    while True:
        st = client.get("/admin/wecom-config").json()
        if st["state"] in states or time.monotonic() > end:
            return st
        time.sleep(0.02)


def test_status_when_not_configured(client, env_file):
    st = client.get("/admin/wecom-config").json()
    assert st["state"] == "off" and not st["configured"] and st["bot_id"] == "" and not st["has_secret"]


def test_save_connects_without_restart(client, env_file, monkeypatch):
    ws = FakeWS()
    use_server(monkeypatch, ws)
    r = client.post("/admin/wecom-config", json={"bot_id": BOT, "secret": SECRET})
    assert r.status_code == 200 and SECRET not in r.text and BOT not in r.text  # 只回传打码的 Bot ID
    st = wait_state(client, "connected")
    assert st["state"] == "connected" and st["state_text"] == "已连接" and st["configured"] and st["has_secret"]
    assert ws.of("aibot_subscribe")[0]["body"] == {"bot_id": BOT, "secret": SECRET}
    text = env_file.read_text(encoding="utf-8")
    assert f"WECOM_BOT_ID={BOT}" in text and f"WECOM_BOT_SECRET={SECRET}" in text and "APP_SECRET=keep-me" in text
    audit = client.get("/admin/audit?limit=5").json()
    assert any(e["event"] == "wecom_config" for e in audit) and SECRET not in str(audit)


def test_wrong_secret_shows_auth_failed(client, env_file, monkeypatch):
    use_server(monkeypatch, FakeWS(errcode=40001), FakeWS(errcode=40001))
    client.post("/admin/wecom-config", json={"bot_id": BOT, "secret": SECRET})
    st = wait_state(client, "auth_failed")
    assert st["state"] == "auth_failed" and "40001" in st["last_error"] and "不对" in st["state_text"]


def test_resave_reconnects_and_keeps_secret(client, env_file, monkeypatch):
    first, second = FakeWS(), FakeWS()
    use_server(monkeypatch, first, second)
    client.post("/admin/wecom-config", json={"bot_id": BOT, "secret": SECRET})
    wait_state(client, "connected")
    r = client.post("/admin/wecom-config", json={"app_h5_url": "http://192.168.1.8:5173/"})  # 只改 App 地址
    assert r.json()["app_h5_url"] == "http://192.168.1.8:5173"
    wait_state(client, "connected")
    assert second.of("aibot_subscribe")[0]["body"]["secret"] == SECRET  # 留空沿用已填写的 Secret


@pytest.mark.parametrize("body, word", [
    ({"bot_id": "abc\nADMIN_PASSWORD=x", "secret": SECRET}, "Bot ID"),
    ({"bot_id": BOT, "secret": "has space inside"}, "Secret"),
    ({"bot_id": BOT, "secret": "abcdefgh#X=1"}, "Secret"),
    ({"bot_id": BOT, "secret": 'abcdefgh"\nADMIN_PASSWORD=x'}, "Secret"),
    ({"bot_id": BOT, "secret": SECRET, "app_h5_url": "javascript:alert(1)"}, "App 地址"),
    ({"bot_id": BOT}, "Secret"),
    ({"secret": SECRET}, "Bot ID"),
])
def test_bad_input_rejected_and_env_untouched(client, env_file, monkeypatch, body, word):
    use_server(monkeypatch)
    before = env_file.read_text(encoding="utf-8")
    r = client.post("/admin/wecom-config", json=body)
    assert r.status_code == 400 and word in r.json()["detail"]
    assert env_file.read_text(encoding="utf-8") == before


def test_disable_stops_channel_and_clears_env(client, env_file, monkeypatch):
    use_server(monkeypatch, FakeWS())
    client.post("/admin/wecom-config", json={"bot_id": BOT, "secret": SECRET})
    wait_state(client, "connected")
    st = client.post("/admin/wecom-config", json={"disable": True}).json()
    assert st["state"] == "off" and not st["configured"]
    text = env_file.read_text(encoding="utf-8")
    assert "WECOM_BOT_ID=\n" in text and "WECOM_BOT_SECRET=\n" in text and SECRET not in text


def test_base64_secret_accepted(client, env_file, monkeypatch):
    use_server(monkeypatch, FakeWS())
    r = client.post("/admin/wecom-config", json={"bot_id": BOT, "secret": "Ab+cd/ef0123456789=="})
    assert r.status_code == 200 and "WECOM_BOT_SECRET=Ab+cd/ef0123456789==" in env_file.read_text(encoding="utf-8")
