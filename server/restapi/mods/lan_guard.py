"""loopback 以外(LAN)からのアクセスを TTS API だけに絞る最外層ミドルウェア。

安全方針(fail closed):
- loopback からのリクエストは従来どおり素通し。
- loopback 以外は、(1) RVC_TTS_LAN_TOKEN が設定済み(24文字以上)、(2) 送信元が許可CIDR内、
  (3) パスが /models, /v1/*, /audio/* のどれか、(4) Authorization: Bearer <token> が一致、
  のすべてを満たす場合のみ通す。それ以外(WebSocket、GUI、アップロード、設定など)は全て拒否。
- 認証失敗が同一IPから短時間に続くとロック(429)。ロック中は正しいトークンでも拒否。
- 本文サイズ上限(Content-Length 必須、chunked 拒否)。認証済みの同時処理数にも上限。
- 認証済みの印(scope["tts_lan_ok"])はこのミドルウェアだけが付ける。毎回先に消すので外部から偽装できない。
- トークンはログに出さない。
環境変数:
  RVC_TTS_LAN_TOKEN  共有トークン(未設定なら LAN は全拒否)
  RVC_TTS_LAN_CIDR   許可する送信元(カンマ区切り)。未設定ならプライベートアドレス全般、
                     設定済みで空/不正なら全拒否。
"""
import hmac
import ipaddress
import os
import time
from typing import Optional

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_CIDRS = "192.168.0.0/16,172.16.0.0/12,10.0.0.0/8"
MIN_TOKEN_LEN = 24
FLAG = "tts_lan_ok"

MAX_BODY_BYTES = 256 * 1024     # 本文の上限
MAX_FAILS = 5                   # この回数の認証失敗で
FAIL_WINDOW_SEC = 60.0          # この時間内に起きたら
LOCK_SEC = 60.0                 # この時間ロック
MAX_TRACKED_IPS = 1024          # 失敗記録の最大IP数
MAX_INFLIGHT = 8                # 認証済みの同時処理数


def _is_tts_api_path(path: str) -> bool:
    # ".." "." "//" "\" "%" NUL を含むパスは、別ルートへの回り込みに使われ得るので TTS API 扱いにしない
    if ".." in path or "/./" in path or path.endswith("/.") or "//" in path or "\\" in path or "%" in path or "\x00" in path:
        return False
    return path == "/models" or path.startswith("/v1/") or path.startswith("/audio/")


def _short(s: str, n: int = 100) -> str:
    return repr(s[:n])


class LanGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        token = os.environ.get("RVC_TTS_LAN_TOKEN", "").strip()
        self.token: Optional[bytes] = token.encode("utf-8") if len(token) >= MIN_TOKEN_LEN else None
        self.nets = []
        for part in os.environ.get("RVC_TTS_LAN_CIDR", DEFAULT_CIDRS).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                self.nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                print(f"[TTS-LAN] 無効なCIDRを無視: {_short(part)}")
        self.enabled = self.token is not None and bool(self.nets)
        self._seen: set = set()
        self._fails: dict = {}   # ip文字列 -> 失敗時刻のリスト
        self._locked: dict = {}  # ip文字列 -> ロック解除時刻
        self._inflight = 0
        if self.enabled:
            print("[TTS-LAN] LAN からの TTS API を許可(トークン認証)。許可元: " + ", ".join(str(n) for n in self.nets))
        else:
            print("[TTS-LAN] LAN アクセスは無効(トークンが未設定/短い、または許可CIDRが空)。loopback のみ。")

    @staticmethod
    def _client_ip(scope: Scope):
        client = scope.get("client")
        if not client:
            return None
        try:
            ip = ipaddress.ip_address(client[0])
        except ValueError:
            return None
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        return ip

    async def _deny(self, scope: Scope, receive: Receive, send: Send, status: int, reason: str, ip, retry_after: int = 0) -> None:
        print(f"[TTS-LAN] 拒否 {status} ip={ip} path={_short(scope.get('path', ''))} 理由={reason}")
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = {"error": {"message": reason, "type": "tts_lan_error"}}
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        await JSONResponse(body, status_code=status, headers=headers)(scope, receive, send)

    # --- 認証失敗ロック ---
    def _lock_remaining(self, key: str, now: float) -> int:
        until = self._locked.get(key, 0.0)
        if until > now:
            return int(until - now) + 1
        self._locked.pop(key, None)
        return 0

    def _prune(self, now: float) -> None:
        for k in [k for k, u in self._locked.items() if u <= now]:
            del self._locked[k]
        for k in list(self._fails):
            kept = [t for t in self._fails[k] if now - t < FAIL_WINDOW_SEC]
            if kept:
                self._fails[k] = kept
            else:
                del self._fails[k]

    def _record_fail(self, key: str, now: float) -> bool:
        """失敗を記録。ロックに入ったら True。"""
        times = [t for t in self._fails.get(key, []) if now - t < FAIL_WINDOW_SEC]
        times.append(now)
        if len(times) >= MAX_FAILS:
            self._fails.pop(key, None)
            self._locked[key] = now + LOCK_SEC
            return True
        self._fails[key] = times
        return False

    @staticmethod
    def _limited_receive(receive: Receive, declared: int) -> Receive:
        total = 0

        async def wrapped():
            nonlocal total
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body", b""))
                if total > declared:
                    return {"type": "http.disconnect"}
            return msg

        return wrapped

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        scope.pop(FLAG, None)  # 偽装防止: 必ず先に消す
        ip = self._client_ip(scope)
        if ip is not None and ip.is_loopback:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if ip is None or not self.enabled:
            await self._deny(scope, receive, send, 403, "LAN access disabled", ip)
            return
        if scope["type"] == "websocket" or not _is_tts_api_path(path):
            await self._deny(scope, receive, send, 403, "forbidden path", ip)
            return
        if not any(ip in net for net in self.nets):
            await self._deny(scope, receive, send, 403, "forbidden source", ip)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin", "")
        key = str(ip)
        if (key, origin) not in self._seen and len(self._seen) < 64:
            self._seen.add((key, origin))
            print(f"[TTS-LAN] 新規接続 ip={ip} origin={_short(origin)}")

        if scope.get("method") == "OPTIONS":
            # CORSプリフライトはAuthorizationを付けない。応答は内側のCORSミドルウェア(許可オリジンのみ)。
            # 印は付けないので、ルート側は処理しない。
            await self.app(scope, receive, send)
            return

        now = time.monotonic()
        wait = self._lock_remaining(key, now)
        if wait:
            await self._deny(scope, receive, send, 429, "too many failed attempts", ip, retry_after=wait)
            return
        if len(self._fails) + len(self._locked) >= MAX_TRACKED_IPS:
            self._prune(now)
            if key not in self._fails and len(self._fails) + len(self._locked) >= MAX_TRACKED_IPS:
                await self._deny(scope, receive, send, 429, "too many clients", ip, retry_after=int(FAIL_WINDOW_SEC))
                return

        # 本文サイズ: chunked 拒否、POST等は Content-Length 必須、上限超えは 413
        if headers.get("transfer-encoding"):
            await self._deny(scope, receive, send, 411, "chunked body not allowed", ip)
            return
        raw_len = headers.get("content-length")
        if raw_len is None:
            if scope.get("method") in ("POST", "PUT", "PATCH"):
                await self._deny(scope, receive, send, 411, "content-length required", ip)
                return
            declared = 0
        else:
            try:
                declared = int(raw_len)
            except ValueError:
                declared = -1
            if declared < 0:
                await self._deny(scope, receive, send, 400, "bad content-length", ip)
                return
            if declared > MAX_BODY_BYTES:
                await self._deny(scope, receive, send, 413, "body too large", ip)
                return

        scheme, _, cred = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(cred.strip().encode("utf-8"), self.token):
            locked = self._record_fail(key, now)
            await self._deny(scope, receive, send, 429 if locked else 401,
                             "too many failed attempts" if locked else "invalid token", ip,
                             retry_after=int(LOCK_SEC) if locked else 0)
            return
        self._fails.pop(key, None)

        if self._inflight >= MAX_INFLIGHT:
            await self._deny(scope, receive, send, 429, "too many concurrent requests", ip, retry_after=5)
            return

        scope[FLAG] = True
        self._inflight += 1
        try:
            await self.app(scope, self._limited_receive(receive, declared), send)
        finally:
            self._inflight -= 1
