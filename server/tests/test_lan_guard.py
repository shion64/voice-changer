"""LanGuardMiddleware(server/restapi/mods/lan_guard.py)の安全性テスト。

実行(voice-changer の server ディレクトリで):
    ~/.conda/envs/rvc/bin/python -m unittest discover -s tests -p 'test_lan_guard.py' -v

追加依存なし(unittest + starlette)。サーバーやGPUは起動しない。
守りたい性質: loopback 以外は「許可元CIDR + TTS APIパス + 正しいトークン」の全部が揃わない限り通らない。
"""
import asyncio
import importlib.util
import os
import types
import unittest
from pathlib import Path
from unittest import mock

GUARD_PATH = Path(__file__).resolve().parent.parent / "restapi" / "mods" / "lan_guard.py"
_spec = importlib.util.spec_from_file_location("lan_guard_under_test", GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

TOKEN = "a1b2c3d4e5f6a7b8c9d0e1f2"  # 24文字(下限ちょうど)
CIDR = "192.168.124.0/24"
LAN = "192.168.124.50"
LAN2 = "192.168.124.51"
BEARER = "Bearer " + TOKEN


class Harness:
    """ミドルウェア+記録用の内側アプリ。env はこのテスト用に完全に差し替える。"""

    def __init__(self, env=None, app=None):
        env = {"RVC_TTS_LAN_TOKEN": TOKEN, "RVC_TTS_LAN_CIDR": CIDR} if env is None else env
        clean = {k: v for k, v in os.environ.items() if not k.startswith("RVC_TTS_LAN_")}
        clean.update(env)
        self.reached = []
        self.gate = None
        self.app = app or self._default_app
        with mock.patch.dict(os.environ, clean, clear=True):
            self.mw = guard.LanGuardMiddleware(self.app)

    async def _default_app(self, scope, receive, send):
        self.reached.append(scope.get(guard.FLAG))
        if self.gate is not None:
            await self.gate.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(self, ip, path, method="GET", auth=None, headers=None, typ="http", preset_flag=False, receive=None):
        raw = []
        if auth is not None:
            raw.append((b"authorization", auth.encode("utf-8")))
        for k, v in (headers or {}).items():
            raw.append((k.lower().encode("latin-1"), v.encode("latin-1")))
        scope = {"type": typ, "client": (ip, 5555) if ip is not None else None, "path": path, "method": method, "headers": raw}
        if preset_flag:
            scope[guard.FLAG] = True
        out = []

        async def send(m):
            out.append(m)

        async def default_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        before = len(self.reached)
        await self.mw(scope, receive or default_receive, send)
        status = None
        if out:
            status = out[0].get("status", out[0].get("code"))
        return types.SimpleNamespace(status=status, reached=len(self.reached) > before, flag=(self.reached[-1] if len(self.reached) > before else None))


class GuardTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs = []
        guard.print = lambda *a, **k: self.logs.append(" ".join(str(x) for x in a))
        self.clock = [1000.0]
        self._orig_time = guard.time
        guard.time = types.SimpleNamespace(monotonic=lambda: self.clock[0])
        self._saved = (guard.MAX_INFLIGHT, guard.MAX_TRACKED_IPS)

    def tearDown(self):
        guard.time = self._orig_time
        guard.MAX_INFLIGHT, guard.MAX_TRACKED_IPS = self._saved
        del guard.print


class TestLoopback(GuardTestBase):
    async def test_loopback_passes_everything_without_token(self):
        h = Harness()
        for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            for path in ("/front/", "/tts", "/upload_dir/x", "/models", "/v1/audio/speech"):
                r = await h.call(ip, path)
                self.assertEqual((r.status, r.reached), (200, True), (ip, path))

    async def test_loopback_does_not_get_lan_flag_and_preset_flag_is_removed(self):
        h = Harness()
        r = await h.call("127.0.0.1", "/models", preset_flag=True)
        self.assertIsNone(r.flag)

    async def test_non_http_scopes_pass(self):
        h = Harness()
        called = []

        async def app(scope, receive, send):
            called.append(scope["type"])

        h2 = Harness(app=app)
        await h2.mw({"type": "lifespan"}, None, None)
        self.assertEqual(called, ["lifespan"])


class TestLanAccess(GuardTestBase):
    async def test_valid_token_on_tts_paths(self):
        h = Harness()
        for method, path in (("GET", "/models"), ("GET", "/v1/models"), ("POST", "/v1/audio/speech"), ("POST", "/audio/speech")):
            hdr = {"content-length": "10"} if method == "POST" else None
            r = await h.call(LAN, path, method, BEARER, hdr)
            self.assertEqual((r.status, r.reached, r.flag), (200, True, True), path)

    async def test_scheme_case_insensitive_but_other_schemes_rejected(self):
        h = Harness()
        self.assertEqual((await h.call(LAN, "/models", auth="bearer " + TOKEN)).status, 200)
        self.assertEqual((await h.call(LAN, "/models", auth="Basic " + TOKEN)).status, 401)
        self.assertEqual((await h.call(LAN, "/models", auth=TOKEN)).status, 401)

    async def test_missing_or_wrong_token_rejected_and_app_not_reached(self):
        h = Harness()
        for auth in (None, "Bearer ", "Bearer wrong", "Bearer " + TOKEN[:-1], "Bearer " + TOKEN + "x", "Bearer トークン"):
            r = await h.call(LAN, "/models", auth=auth)
            self.assertEqual(r.status, 401, auth)
            self.assertFalse(r.reached, auth)
            h.mw._fails.clear()  # この試験ではロックを挟まず、各入力の拒否だけを見る

    async def test_lan_flag_cannot_be_forged(self):
        h = Harness()
        r = await h.call(LAN, "/v1/audio/speech", "POST", None, {"content-length": "1"}, preset_flag=True)
        self.assertEqual(r.status, 401)
        self.assertFalse(r.reached)

    async def test_non_tts_paths_forbidden_even_with_valid_token(self):
        h = Harness()
        for path in ("/front/", "/tts", "/tts/api/settings", "/tts/api/trim", "/upload_dir/a", "/tmp/a", "/trainer/", "/",
                     "/model_dir_static/x", "/v1", "/models/", "/modelsx", "/audio", "/socket.io/"):
            r = await h.call(LAN, path, "GET", BEARER)
            self.assertEqual(r.status, 403, path)
            self.assertFalse(r.reached, path)

    async def test_path_tricks_rejected(self):
        h = Harness()
        for path in ("/v1/../front/", "/v1/./audio", "/v1/audio/.", "/v1/%2e%2e/front/", "//v1/audio/speech", "/v1//audio/speech", "/audio/..\\front", "/v1/audio\x00/x", "/models/.."):
            r = await h.call(LAN, path, "GET", BEARER)
            self.assertEqual(r.status, 403, repr(path))
            self.assertFalse(r.reached, repr(path))

    async def test_source_outside_cidr_forbidden(self):
        h = Harness()
        for ip in ("192.168.1.5", "192.168.125.1", "10.0.0.1", "8.8.8.8", "fe80::1", "2001:db8::1"):
            r = await h.call(ip, "/models", "GET", BEARER)
            self.assertEqual(r.status, 403, ip)
            self.assertFalse(r.reached, ip)

    async def test_bad_or_missing_client_forbidden(self):
        h = Harness()
        for ip in (None, "not-an-ip", "localhost", ""):
            r = await h.call(ip, "/models", "GET", BEARER)
            self.assertEqual(r.status, 403, ip)
            self.assertFalse(r.reached, ip)

    async def test_websocket_from_lan_closed(self):
        h = Harness()
        r = await h.call(LAN, "/socket.io/", "GET", BEARER, typ="websocket")
        self.assertEqual(r.status, 1008)
        self.assertFalse(r.reached)

    async def test_websocket_from_loopback_passes(self):
        async def app(scope, receive, send):
            await send({"type": "websocket.close", "code": 1000})

        h = Harness(app=app)
        r = await h.call("127.0.0.1", "/ws", typ="websocket")
        self.assertEqual(r.status, 1000)

    async def test_preflight(self):
        h = Harness()
        r = await h.call(LAN, "/v1/audio/speech", "OPTIONS")
        self.assertEqual((r.status, r.reached, r.flag), (200, True, None))  # 通すが認証済みの印は付けない
        self.assertFalse((await h.call(LAN, "/front/", "OPTIONS")).reached)
        self.assertFalse((await h.call("8.8.8.8", "/v1/audio/speech", "OPTIONS")).reached)


class TestFailClosedConfig(GuardTestBase):
    async def assert_lan_blocked_loopback_ok(self, h):
        self.assertFalse(h.mw.enabled)
        r = await h.call(LAN, "/models", "GET", BEARER)
        self.assertEqual((r.status, r.reached), (403, False))
        self.assertTrue((await h.call("127.0.0.1", "/models")).reached)

    async def test_no_token(self):
        await self.assert_lan_blocked_loopback_ok(Harness({"RVC_TTS_LAN_CIDR": CIDR}))

    async def test_blank_token(self):
        await self.assert_lan_blocked_loopback_ok(Harness({"RVC_TTS_LAN_TOKEN": "   ", "RVC_TTS_LAN_CIDR": CIDR}))

    async def test_short_token(self):
        short = "x" * (guard.MIN_TOKEN_LEN - 1)
        h = Harness({"RVC_TTS_LAN_TOKEN": short, "RVC_TTS_LAN_CIDR": CIDR})
        self.assertFalse(h.mw.enabled)
        r = await h.call(LAN, "/models", "GET", "Bearer " + short)
        self.assertEqual((r.status, r.reached), (403, False))

    async def test_empty_cidr(self):
        await self.assert_lan_blocked_loopback_ok(Harness({"RVC_TTS_LAN_TOKEN": TOKEN, "RVC_TTS_LAN_CIDR": ""}))

    async def test_invalid_cidr_only(self):
        await self.assert_lan_blocked_loopback_ok(Harness({"RVC_TTS_LAN_TOKEN": TOKEN, "RVC_TTS_LAN_CIDR": "banana,1.2.3"}))

    async def test_cidr_unset_defaults_to_private_ranges_only(self):
        h = Harness({"RVC_TTS_LAN_TOKEN": TOKEN})
        self.assertTrue(h.mw.enabled)
        self.assertEqual((await h.call("192.168.7.7", "/models", "GET", BEARER)).status, 200)
        self.assertEqual((await h.call("8.8.8.8", "/models", "GET", BEARER)).status, 403)

    async def test_single_host_cidr(self):
        h = Harness({"RVC_TTS_LAN_TOKEN": TOKEN, "RVC_TTS_LAN_CIDR": "192.168.124.134/32"})
        self.assertEqual((await h.call("192.168.124.134", "/models", "GET", BEARER)).status, 200)
        self.assertEqual((await h.call("192.168.124.135", "/models", "GET", BEARER)).status, 403)

    async def test_minimum_length_token_is_accepted(self):
        self.assertEqual(len(TOKEN), guard.MIN_TOKEN_LEN)
        self.assertTrue(Harness().mw.enabled)


class TestBodyLimits(GuardTestBase):
    async def test_post_requires_content_length(self):
        h = Harness()
        r = await h.call(LAN, "/v1/audio/speech", "POST", BEARER)
        self.assertEqual((r.status, r.reached), (411, False))

    async def test_chunked_rejected(self):
        h = Harness()
        r = await h.call(LAN, "/v1/audio/speech", "POST", BEARER, {"transfer-encoding": "chunked"})
        self.assertEqual((r.status, r.reached), (411, False))

    async def test_size_limit_boundary(self):
        h = Harness()
        ok = await h.call(LAN, "/v1/audio/speech", "POST", BEARER, {"content-length": str(guard.MAX_BODY_BYTES)})
        self.assertEqual(ok.status, 200)
        big = await h.call(LAN, "/v1/audio/speech", "POST", BEARER, {"content-length": str(guard.MAX_BODY_BYTES + 1)})
        self.assertEqual((big.status, big.reached), (413, False))
        huge = await h.call(LAN, "/models", "GET", BEARER, {"content-length": str(10 ** 12)})
        self.assertEqual((huge.status, huge.reached), (413, False))

    async def test_bad_content_length(self):
        h = Harness()
        for v in ("abc", "-5", "1.5", ""):
            r = await h.call(LAN, "/v1/audio/speech", "POST", BEARER, {"content-length": v})
            self.assertEqual((r.status, r.reached), (400, False), v)

    async def test_oversized_body_is_cut_to_declared_length(self):
        seen = []

        async def app(scope, receive, send):
            while True:
                m = await receive()
                seen.append(m["type"])
                if m["type"] != "http.request" or not m.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})

        h = Harness(app=app)
        msgs = iter([{"type": "http.request", "body": b"x" * 10, "more_body": True},
                     {"type": "http.request", "body": b"y" * 10, "more_body": False}])

        async def receive():
            return next(msgs)

        await h.call(LAN, "/v1/audio/speech", "POST", BEARER, {"content-length": "10"}, receive=receive)
        self.assertEqual(seen, ["http.request", "http.disconnect"])


class TestLockout(GuardTestBase):
    async def fail(self, h, ip=LAN, n=1):
        last = None
        for _ in range(n):
            last = await h.call(ip, "/models", "GET", "Bearer wrong")
        return last

    async def test_lock_after_max_fails(self):
        h = Harness()
        for _ in range(guard.MAX_FAILS - 1):
            self.assertEqual((await self.fail(h)).status, 401)
        self.assertEqual((await self.fail(h)).status, 429)

    async def test_lock_rejects_even_correct_token_and_isolated_per_ip(self):
        h = Harness()
        await self.fail(h, n=guard.MAX_FAILS)
        r = await h.call(LAN, "/models", "GET", BEARER)
        self.assertEqual((r.status, r.reached), (429, False))
        self.assertEqual((await h.call(LAN2, "/models", "GET", BEARER)).status, 200)

    async def test_lock_expires(self):
        h = Harness()
        await self.fail(h, n=guard.MAX_FAILS)
        self.clock[0] += guard.LOCK_SEC - 1
        self.assertEqual((await h.call(LAN, "/models", "GET", BEARER)).status, 429)
        self.clock[0] += 2
        self.assertEqual((await h.call(LAN, "/models", "GET", BEARER)).status, 200)

    async def test_success_resets_counter(self):
        h = Harness()
        await self.fail(h, n=guard.MAX_FAILS - 1)
        self.assertEqual((await h.call(LAN, "/models", "GET", BEARER)).status, 200)
        for _ in range(guard.MAX_FAILS - 1):
            self.assertEqual((await self.fail(h)).status, 401)

    async def test_old_failures_age_out(self):
        h = Harness()
        await self.fail(h, n=guard.MAX_FAILS - 1)
        self.clock[0] += guard.FAIL_WINDOW_SEC + 1
        self.assertEqual((await self.fail(h)).status, 401)  # 古い失敗は数えない

    async def test_tracking_table_is_bounded(self):
        guard.MAX_TRACKED_IPS = 3
        h = Harness()
        for i in range(3):
            await self.fail(h, ip=f"192.168.124.{100 + i}")
        r = await h.call("192.168.124.200", "/models", "GET", BEARER)
        self.assertEqual((r.status, r.reached), (429, False))
        # 既に記録のあるIPは影響を受けない
        self.assertEqual((await h.call("192.168.124.100", "/models", "GET", BEARER)).status, 200)
        # 時間が経てば記録が掃除されて新規IPも通る
        self.clock[0] += guard.FAIL_WINDOW_SEC + 1
        self.assertEqual((await h.call("192.168.124.200", "/models", "GET", BEARER)).status, 200)

    async def test_forbidden_paths_do_not_fill_tracking_table(self):
        h = Harness()
        for i in range(50):
            await h.call(f"192.168.124.{i + 1}", "/front/", "GET", "Bearer wrong")
        self.assertEqual(len(h.mw._fails) + len(h.mw._locked), 0)


class TestConcurrency(GuardTestBase):
    async def test_inflight_cap_and_release(self):
        guard.MAX_INFLIGHT = 2
        h = Harness()
        h.gate = asyncio.Event()
        tasks = [asyncio.create_task(h.call(f"192.168.124.{100 + i}", "/models", "GET", BEARER)) for i in range(2)]
        await asyncio.sleep(0.05)
        over = await h.call("192.168.124.150", "/models", "GET", BEARER)
        self.assertEqual((over.status, over.reached), (429, False))
        h.gate.set()
        res = await asyncio.gather(*tasks)
        self.assertEqual([r.status for r in res], [200, 200])
        self.assertEqual(h.mw._inflight, 0)
        self.assertEqual((await h.call(LAN, "/models", "GET", BEARER)).status, 200)

    async def test_inflight_released_when_app_raises(self):
        async def boom(scope, receive, send):
            raise RuntimeError("boom")

        h = Harness(app=boom)
        with self.assertRaises(RuntimeError):
            await h.call(LAN, "/models", "GET", BEARER)
        self.assertEqual(h.mw._inflight, 0)

    async def test_unauthenticated_requests_do_not_consume_slots(self):
        guard.MAX_INFLIGHT = 1
        h = Harness()
        for _ in range(3):
            await h.call(LAN, "/models", "GET", "Bearer wrong")
            h.mw._fails.clear()
        self.assertEqual(h.mw._inflight, 0)
        self.assertEqual((await h.call(LAN2, "/models", "GET", BEARER)).status, 200)


class TestLogging(GuardTestBase):
    async def test_token_never_logged(self):
        h = Harness()
        await h.call(LAN, "/models", "GET", BEARER)
        await h.call(LAN, "/models", "GET", "Bearer secret-wrong-token-value")
        await h.call(LAN, "/front/", "GET", BEARER)
        await h.call("8.8.8.8", "/models", "GET", BEARER)
        text = "\n".join(self.logs)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("secret-wrong-token-value", text)

    async def test_log_path_is_truncated(self):
        h = Harness()
        await h.call(LAN, "/" + "a" * 5000, "GET", BEARER)
        self.assertTrue(all(len(line) < 400 for line in self.logs))


if __name__ == "__main__":
    unittest.main()
