"""MMVC_Rest_TTS の VRAM 解放(アイドルトリム)が、RVC変換と重ならないことのテスト。

実行(voice-changer の server ディレクトリで):
    ~/.conda/envs/rvc/bin/python -m unittest discover -s tests -p 'test_tts_trim.py' -v

GPU・モデル不要。_trim_vram はスタブに差し替える。守りたい性質:
  1. 解放は変換(ロック保持中)と同時に走らない
  2. 要求が来ている/来たら解放しない
  3. 解放の実行中はタイマーの再設定でキャンセルされない
  4. キャンセル/例外のあとでもロックが必ず戻る(デッドロックしない)
"""
import asyncio
import importlib.util
import sys
import time
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))  # mods.* を import できるように
_spec = importlib.util.spec_from_file_location("tts_under_test", SERVER / "restapi" / "MMVC_Rest_TTS.py")
tts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tts)


def make_obj(trim_impl):
    obj = object.__new__(tts.MMVC_Rest_TTS)
    obj._waiting = 0
    obj._rvc_lock = tts._PriorityLock()
    obj._trim_task = None
    obj._trimming = False
    obj._trim_vram = trim_impl
    return obj


def lock_is_free(lock) -> bool:
    return (not lock._locked) and not lock._waiters


class TestTrim(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._idle = tts.IDLE_TRIM_SEC
        tts.IDLE_TRIM_SEC = 0.05
        self.events = []

    def tearDown(self):
        tts.IDLE_TRIM_SEC = self._idle

    def stub(self, duration=0.0, fail=False):
        def trim():
            self.events.append("trim_start")
            time.sleep(duration)
            self.events.append("trim_end")
            if fail:
                raise RuntimeError("boom")
            return {"reserved": 2}, {"reserved": 1}
        return trim

    async def test_idle_trim_runs_once_when_quiet(self):
        o = make_obj(self.stub())
        o._schedule_trim()
        await asyncio.sleep(0.3)
        self.assertEqual(self.events, ["trim_start", "trim_end"])
        self.assertTrue(lock_is_free(o._rvc_lock))
        self.assertFalse(o._trimming)

    async def test_no_trim_when_request_in_flight_at_fire_time(self):
        o = make_obj(self.stub())
        o._waiting = 1
        o._schedule_trim()
        await asyncio.sleep(0.3)
        self.assertEqual(self.events, [])

    async def test_no_trim_if_request_arrives_while_waiting_for_lock(self):
        o = make_obj(self.stub())
        await o._rvc_lock.acquire((0, 0))  # 変換中を模擬
        o._schedule_trim()
        await asyncio.sleep(0.2)           # タイマーは満了し、トリムはロック待ち
        o._waiting = 1                     # この間に新しい要求が到着
        o._rvc_lock.release()
        await asyncio.sleep(0.2)
        self.assertEqual(self.events, [])  # ロック取得後の再確認で見送る
        self.assertTrue(lock_is_free(o._rvc_lock))

    async def test_trim_never_overlaps_a_conversion(self):
        o = make_obj(self.stub(duration=0.2))
        o._schedule_trim()
        await asyncio.sleep(0.12)          # 解放スレッドが走っている最中
        self.assertTrue(o._trimming)
        order = []

        async def conversion():
            await o._rvc_lock.acquire((0, 0))
            order.append("convert_start")
            self.events.append("convert_start")
            o._rvc_lock.release()

        await conversion()
        self.assertEqual(self.events, ["trim_start", "trim_end", "convert_start"])  # 解放が終わってから変換

    async def test_conversion_in_progress_blocks_trim(self):
        o = make_obj(self.stub())
        await o._rvc_lock.acquire((0, 0))
        self.events.append("convert_start")
        o._schedule_trim()
        await asyncio.sleep(0.25)
        self.assertEqual(self.events, ["convert_start"])  # 変換中は解放が始まらない
        self.events.append("convert_end")
        o._rvc_lock.release()
        await asyncio.sleep(0.2)
        self.assertEqual(self.events, ["convert_start", "convert_end", "trim_start", "trim_end"])

    async def test_reschedule_does_not_cancel_running_trim(self):
        o = make_obj(self.stub(duration=0.3))
        o._schedule_trim()
        await asyncio.sleep(0.15)
        running = o._trim_task
        self.assertTrue(o._trimming)
        o._schedule_trim()                 # 変換が終わるたびに呼ばれる想定
        self.assertFalse(running.cancelled())
        await asyncio.sleep(1.0)
        # 実行中の解放は最後まで完了し、延長されたタイマーの解放がそのあとに1回だけ走る(重ならない)
        self.assertEqual(self.events, ["trim_start", "trim_end", "trim_start", "trim_end"])
        self.assertFalse(o._trimming)
        self.assertTrue(lock_is_free(o._rvc_lock))

    async def test_reschedule_cancels_pending_timer(self):
        o = make_obj(self.stub())
        o._schedule_trim()
        await asyncio.sleep(0.02)
        first = o._trim_task
        o._schedule_trim()                 # タイマーを延長
        await asyncio.sleep(0.02)
        self.assertTrue(first.cancelled() or first.done())
        await asyncio.sleep(0.25)
        self.assertEqual(self.events, ["trim_start", "trim_end"])  # 解放は1回だけ

    async def test_cancel_while_waiting_for_lock_leaves_lock_usable(self):
        o = make_obj(self.stub())
        await o._rvc_lock.acquire((0, 0))
        o._schedule_trim()
        await asyncio.sleep(0.2)
        o._trim_task.cancel()
        await asyncio.gather(o._trim_task, return_exceptions=True)
        o._rvc_lock.release()
        self.assertTrue(lock_is_free(o._rvc_lock))
        await asyncio.wait_for(o._rvc_lock.acquire((0, 0)), 1)  # デッドロックしない
        o._rvc_lock.release()
        self.assertFalse(o._trimming)

    async def test_manual_trim_returns_result_and_releases_lock(self):
        o = make_obj(self.stub())
        before, after = await o._trim_exclusive()
        self.assertEqual((before, after), ({"reserved": 2}, {"reserved": 1}))
        self.assertTrue(lock_is_free(o._rvc_lock))
        self.assertFalse(o._trimming)

    async def test_manual_trim_waits_for_conversion(self):
        o = make_obj(self.stub())
        await o._rvc_lock.acquire((0, 0))
        t = asyncio.ensure_future(o._trim_exclusive())
        await asyncio.sleep(0.1)
        self.assertEqual(self.events, [])
        o._rvc_lock.release()
        await asyncio.wait_for(t, 1)
        self.assertEqual(self.events, ["trim_start", "trim_end"])

    async def test_lock_released_when_trim_raises(self):
        o = make_obj(self.stub(fail=True))
        with self.assertRaises(RuntimeError):
            await o._trim_exclusive()
        self.assertTrue(lock_is_free(o._rvc_lock))
        self.assertFalse(o._trimming)

    async def test_lock_released_when_manual_caller_is_cancelled_mid_trim(self):
        o = make_obj(self.stub(duration=0.2))
        t = asyncio.ensure_future(o._trim_exclusive())
        await asyncio.sleep(0.1)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        self.assertEqual(self.events, ["trim_start", "trim_end"])  # スレッド終了後にロックを返す
        self.assertTrue(lock_is_free(o._rvc_lock))
        self.assertFalse(o._trimming)

    async def test_priority_order_conversions_go_before_trim(self):
        o = make_obj(self.stub())
        await o._rvc_lock.acquire((0, 0))
        t = asyncio.ensure_future(o._trim_exclusive())
        await asyncio.sleep(0.05)
        async def conv(prio):
            await o._rvc_lock.acquire(prio)
            self.events.append("convert")
            o._rvc_lock.release()

        c = asyncio.ensure_future(conv((5, 0)))
        await asyncio.sleep(0.05)
        o._rvc_lock.release()
        await asyncio.wait_for(asyncio.gather(t, c), 1)
        self.assertEqual(self.events, ["convert", "trim_start", "trim_end"])  # 変換が先、解放は最後


if __name__ == "__main__":
    unittest.main()
