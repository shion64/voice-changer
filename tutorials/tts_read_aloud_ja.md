# TTS 統合(Read Aloud × voice-changer)

Firefox 拡張 **Read Aloud** の「OpenAI 互換カスタム音声」から、voice-changer の統合 TTS
(Edge-TTS で読み上げ → RVC で一括変換)を使い、好きな声で Web ページを読み上げます。

## 仕組み

```
Read Aloud ──POST /v1/audio/speech──▶ voice-changer ─▶ Edge-TTS ─▶ RVC ─▶ 音声(Read Aloud が再生)
```

- 長文は 120 文字ごとに分割します。Edge-TTS は全チャンクを並列(同時 4)、RVC 変換は到着順に直列で処理します。
- RVC の出力が無音だった場合は、余白 0.5 秒を足して再変換します。それでも無音なら元の声(Edge-TTS)で代用します。
- 待機 10 秒で空き VRAM を解放します(確保済み 1396 → 1022 MiB)。
- ログは `[TTS] ok ...` の形式で、`tts_wait` / `rvc` / `enc` の内訳が出ます。

## エンドポイント

| パス | 用途 |
| --- | --- |
| `POST /v1/audio/speech` | 読み上げ(OpenAI 互換) |
| `GET /v1/models`, `GET /models` | モデル一覧 |
| `GET /tts` | 設定・テスト用ページ(loopback のみ) |

## 起動

`run-Arch-AMD.sh` で起動します。Read Aloud から使うには、拡張機能の origin の許可が必要です。

```
python MMVCServerSIO.py --rest_port 18888 --use_gpu --tts-allowed-origins moz-extension://<拡張機能のUUID>
```

- `--tts-allowed-origins` は複数回指定できます(PC とスマホなど)。許可した origin は **TTS API のパスだけ** 使えます。
- UUID は端末ごとに違います。Read Aloud の設定画面(`moz-extension://<UUID>/custom-voices.html`)のアドレスバーで確認できます。
  分からない場合は、一度読み上げを試すと、`[TTS-LAN] 新規接続 ... origin=moz-extension://...` がログに出ます(LAN 利用時)。

## Read Aloud の設定(PC)

「カスタム音声」→ OpenAI の欄に入力します。

- **API URL**: `http://127.0.0.1:18888`
- **API Key**: 空欄
- **Voice List**:

```json
[{"voice":"RVC_アルトリア","langs":["ja-JP"],"model":"rvc-tts","edge_voice":"ja-JP-NanamiNeural"}]
```

声の高さが合わない場合は、voice-changer の GUI で TUNE を調整してください。

## スマホ(LAN)から使う

PC がスマホのテザリングに繋がっている構成を想定した手順です。**LAN への公開は loopback より弱いので、必ず次の安全対策を有効にしてください。**

### 1. 仕組みと安全方針

`server/restapi/mods/lan_guard.py` が最外層のガードとして動き、loopback 以外からのアクセスを次の条件でのみ通します(1つでも欠ければ拒否します)。

1. `RVC_TTS_LAN_TOKEN`(24 文字以上)が設定済みであること
2. 送信元が `RVC_TTS_LAN_CIDR` の範囲内であること(未設定時はプライベートアドレス全般)
3. パスが `/models`、`/v1/*`、`/audio/*` のどれかであること(`..` や `//` を含むパスは拒否)
4. `Authorization: Bearer <トークン>` が一致すること

それ以外(GUI、アップロード、設定ページ、WebSocket など)は、LAN からは常に拒否します。さらに次の制限があります。

- 認証失敗が同一 IP から 60 秒以内に 5 回続くと 60 秒ロック(429)。ロック中は正しいトークンでも拒否します。
- 本文は 256KB まで。`Content-Length` 必須、chunked は拒否します。
- 認証済みの同時処理は 8 件まで。
- トークンはログに出しません。

### 2. セットアップ

`~/.config/rvc-tts-lan-token` にトークン(24 文字以上、権限 0600)を置き、起動スクリプトで読み込みます。

```bash
export RVC_TTS_LAN_TOKEN="$(cat "$HOME/.config/rvc-tts-lan-token" 2>/dev/null)"
export RVC_TTS_LAN_CIDR="<許可する送信元。例: スマホ1台なら 192.168.124.134/32>"
python MMVCServerSIO.py --rest_port 18888 --use_gpu --host 0.0.0.0 \
  --tts-allowed-origins moz-extension://<PCのUUID> --tts-allowed-origins moz-extension://<スマホのUUID>
```

- `--host 0.0.0.0` にしても、LAN からはガードを通ったものだけが届きます。
- ファイアウォール(firewalld など)でも、18888/tcp は **許可する端末の IP だけ** に開けてください。

### 3. スマホ(Firefox for Android)の Read Aloud

- **API URL**: `http://<PCのIP>:18888`
- **API Key**: 上のトークン
- **Voice List**: PC と同じ

PC の IP は `ip -4 -br addr` で確認できます。テザリングを繋ぎ直すと PC・スマホの IP が変わることがあります。その場合は、ファイアウォールの許可元と `RVC_TTS_LAN_CIDR`、Read Aloud の URL を直して、voice-changer を再起動してください。

### 4. 注意

- 通信は平文の HTTP です。同じネットワークの他の端末が傍受できる可能性があるので、信頼できる自分のテザリングだけで使ってください。
- トークンを他人に見せない・チャットや公開リポジトリに貼らないでください。漏れた場合は、トークンファイルを作り直して再起動し、スマホ側の値も更新します。
- `[TTS-LAN] 拒否 ...` のログが身に覚えのない IP で出ていたら、許可元を狭めるか、トークンを作り直してください。

### 5. テスト

ガードの安全性テストがあります(サーバーや GPU は不要です)。

```bash
cd server
python -m unittest discover -s tests -p 'test_lan_guard.py' -v
```

## トラブルシュート

| 症状 | 確認すること |
| --- | --- |
| Read Aloud に `NetworkError` | PC のログに `[TTS-LAN]` が出るか。出なければ未到達(ファイアウォール・IP・VPN)。出れば origin の許可漏れ |
| `Invalid origin header`(400) | その拡張機能の UUID が `--tts-allowed-origins` にない |
| 401 `invalid token` | API Key が違う。前後の空白に注意 |
| 429 | 認証失敗のロック中か、同時処理が上限。60 秒待つ |
| 413 / 411 | 本文が 256KB 超、または `Content-Length` なし |
| 声が出ない・無音 | ログの `fallbacks`。`export RVC_OFFLINE_PAD_SEC=0.5` を試す。GUI の半精度を切って試す |
