"""daemon の runtime 起動ポーラが、見送り後も再試行することのテスト（codexレビューP1①）。

`start_runtime_threads()` は、直前に取り直した snapshot が未準備なら起動を見送って返る。
ポーラが `harness_ready()` を抜けた直後に1回だけ呼ぶ作りだと、その窓を踏んだときに
**loop/chat/MQTT が起動しないまま誰も再試行しない**。daemon 自体は生き続けるので、
外からは「起動しているのに何も動かない」に見える。

daemon.py は import すると flock を取って別プロセスと競合するため、ここでは
`boot_runtime_when_ready()` の制御フローだけを同じ形で再現して検証する。
"""
import threading
import unittest


class _BootHarness:
    """boot_runtime_when_ready() と同じ制御フローの再現。

    `ready_sequence` は harness_ready() の戻り値、`snapshot_sequence` は
    start_runtime_threads() 内部の再取得 snapshot が ready を認めるかを表す。
    """

    def __init__(self, ready_sequence, snapshot_sequence):
        self.ready_sequence = list(ready_sequence)
        self.snapshot_sequence = list(snapshot_sequence)
        self.runtime_started = threading.Event()
        self.start_attempts = 0
        self.sleeps = 0

    def _harness_ready(self):
        return self.ready_sequence.pop(0) if self.ready_sequence else True

    def _start_runtime_threads(self) -> bool:
        self.start_attempts += 1
        if self.runtime_started.is_set():
            return True
        snap_ready = self.snapshot_sequence.pop(0) if self.snapshot_sequence else True
        if not snap_ready:
            return False  # 起動を見送る
        self.runtime_started.set()
        return True

    def _sleep(self, _seconds):
        self.sleeps += 1
        if self.sleeps > 50:
            raise AssertionError("ポーラが収束しない（無限ループ）")

    def boot(self):
        # 実装（daemon.boot_runtime_when_ready）と同じ形。
        # 起動できたかは Event ではなく **戻り値** で判断する。Event を見ると、
        # start_runtime_threads をモックしたテストで永久に立たず無限ループになる。
        while True:
            if self._harness_ready():
                if self._start_runtime_threads():
                    return
            self._sleep(5)


class BootRetryTests(unittest.TestCase):
    def test_retries_after_snapshot_declines(self):
        """外側 True → 内側 snapshot False の窓を踏んでも、次周期で起動する。"""
        h = _BootHarness(ready_sequence=[True, True], snapshot_sequence=[False, True])
        h.boot()
        self.assertTrue(h.runtime_started.is_set(), "見送り後に再試行されず runtime が上がらない")
        self.assertEqual(h.start_attempts, 2)

    def test_waits_while_not_ready(self):
        h = _BootHarness(ready_sequence=[False, False, True], snapshot_sequence=[True])
        h.boot()
        self.assertTrue(h.runtime_started.is_set())
        self.assertEqual(h.start_attempts, 1, "未準備の間は start を呼ばない")

    def test_returns_immediately_when_already_started(self):
        """起動済みなら1回問い合わせて即座に抜ける（待機しない）。"""
        h = _BootHarness(ready_sequence=[True], snapshot_sequence=[True])
        h.runtime_started.set()
        h.boot()
        self.assertEqual(h.start_attempts, 1)
        self.assertEqual(h.sleeps, 0)

    def test_repeated_declines_keep_retrying(self):
        """何度見送られても諦めない（諦めると無言で死ぬのが元のバグ）。"""
        h = _BootHarness(
            ready_sequence=[True] * 6,
            snapshot_sequence=[False, False, False, False, True],
        )
        h.boot()
        self.assertTrue(h.runtime_started.is_set())
        self.assertEqual(h.start_attempts, 5)


class SourceContractTests(unittest.TestCase):
    """実装が「1回呼んで終わり」に戻っていないこと。"""

    def test_boot_loops_until_runtime_started(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "embodied_ha" / "daemon.py").read_text(encoding="utf-8")
        block = src[src.index("def boot_runtime_when_ready"):]
        block = block[: block.index("\n# ---")]
        self.assertIn("if start_runtime_threads():", block,
                      "起動できたかを戻り値で確認せずに抜けている（見送りを検知できない）")
        self.assertIn("time.sleep(5)", block, "再試行の待機が消えている")

    def test_startup_path_falls_back_to_poller_when_start_declines(self):
        """起動時パスも、見送られたらポーラを立てること。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "embodied_ha" / "daemon.py").read_text(encoding="utf-8")
        block = src[src.index("# --- Web UI / runtime 起動 ---"):]
        self.assertIn("_runtime_started_at_boot = harness_ready() and start_runtime_threads()", block,
                      "ready でも見送られた場合にポーラが立たない")
        self.assertIn("if _runtime_started_at_boot:", block)
        self.assertIn("dismiss_setup_wait_notification()", block)


if __name__ == "__main__":
    unittest.main()
