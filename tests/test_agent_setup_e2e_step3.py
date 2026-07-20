"""増分7: エージェント選択セットアップの一連(マイグレーション/セットアップ待ち/ログアウト)
を隔離環境(実ファイルシステム)でシーケンスとして通すE2E。

既存の test_harness_setup_step3 / test_logout_step3 は個別ブランチを mock で固定するが、
本テストは実フラグファイル・実認証ファイル・実バイナリを使い、実 boot と同じ順序で
daemon.harness_ready() → notify_setup_waiting() → boot_runtime_when_ready() を駆動して、
「セットアップ待ち → install → runtime 起動」「logout → セットアップ待ち復帰」までを一連で検証する。
外部・重量境界(HA API の urlopen、start_runtime_threads、自己再起動)だけを mock する。すべて
TemporaryDirectory に隔離し、本番の /data・あかねの記憶には一切触れない。

実 boot 順の正本は daemon.py 末尾(多重起動ガード以降): `if harness_ready(): start_runtime_threads()
else: notify_setup_waiting(); Thread(boot_runtime_when_ready)`。既存 claude 認証がある移行ケースでは
harness_ready() が先に走ってフラグを"claude"へ移行するため、その後の notify は "missing" ではなく
"valid/claude" 経路(=「Claude Codeをインストール」)になる(グランドファザーの「再インストール」本文は
フラグ書込が失敗した縁でのみ出る別分岐で、test_harness_setup_step3 が単体で担保する)。
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
sys.path.insert(0, str(ROOT / "embodied_ha" / "web"))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import claude_setup  # noqa: E402
import harness_state  # noqa: E402
from web import server  # noqa: E402


def _load_daemon_without_boot():
    path = ROOT / "embodied_ha" / "daemon.py"
    source = path.read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType("daemon_e2e_step3_test")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


daemon = _load_daemon_without_boot()


def _mock_response():
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class AgentSetupLifecycleE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.flag = base / "state" / "selected_harness"
        self.config_dir = base / "claude-home"
        self.install_root = base / "claude-cli"
        self.config_dir.mkdir(parents=True)
        (self.config_dir / "projects").mkdir()  # 永続プロジェクト(=記憶)。logoutで消えない対象。
        self.binary = self.install_root / "bin" / "claude"

        self._env = mock.patch.dict(os.environ, {
            "EHA_HARNESS_FLAG_FILE": str(self.flag),
            "CLAUDE_CONFIG_DIR": str(self.config_dir),
            "EHA_CLAUDE_INSTALL_ROOT": str(self.install_root),
            "HA_URL": "http://supervisor/core/api",
            "SUPERVISOR_TOKEN": "test-token",
        }, clear=False)
        self._env.start()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        # ホスト隔離(受け入れ条件8-b): resolve_claude_bin() の PATH フォールバックが dev/ホスト側の
        # claude を拾って「未installでも ready」に見せる隠蔽を排除する。DIY配置バイナリ(binary_path)
        # の実在だけで判定させ、本テストを host-hermetic にする。
        self._which = mock.patch.object(claude_setup.shutil, "which", return_value=None)
        self._which.start()
        daemon._setup_wait_notification_sent = False
        self._reset_restart_latch()

    def tearDown(self):
        self._reset_restart_latch()
        self._which.stop()
        self._env.stop()
        self._tmp.cleanup()

    @staticmethod
    def _reset_restart_latch():
        with server._self_restart_lock:
            server._self_restart_scheduled = False

    # --- 実ファイルを使った状態操作ヘルパ ---
    def _write_credentials(self):
        # サブスク認証実体(.credentials.json)を置く=claude認証あり。
        (self.config_dir / ".credentials.json").write_text("token", encoding="utf-8")

    def _install_binary(self):
        # DIY 配置バイナリを実在させる(install 相当)。実行可能にして resolve_claude_bin が拾う。
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_text("#!/bin/sh\necho claude\n", encoding="utf-8")
        os.chmod(self.binary, 0o755)

    def _notify_once(self):
        daemon._setup_wait_notification_sent = False
        with mock.patch.object(daemon.urllib.request, "urlopen", return_value=_mock_response()) as urlopen:
            daemon.notify_setup_waiting()
        self.assertEqual(urlopen.call_count, 1)
        return json.loads(urlopen.call_args.args[0].data.decode("utf-8"))

    def _boot_and_expect_runtime(self):
        """実オーケストレータ boot_runtime_when_ready() を走らせ、ready→runtime起動を観測する。

        start_runtime_threads(重量境界)と sleep・urlopen(既に latch 済みなので発火しない想定)を
        差し替える。ready 状態から呼ぶので待機ループは即抜け、start_runtime_threads が1回呼ばれる。
        「ready でも runtime を起動しない」退行はここで落ちる(sol finding 2)。
        """
        with mock.patch.object(daemon, "start_runtime_threads") as start, \
             mock.patch.object(daemon.time, "sleep") as sleep, \
             mock.patch.object(daemon.urllib.request, "urlopen", return_value=_mock_response()):
            daemon.boot_runtime_when_ready()
        start.assert_called_once_with()
        sleep.assert_not_called()

    # --- シーケンスA: グランドファザー移行 → セットアップ待ち → install → runtime起動 ---
    def test_grandfather_existing_claude_then_install_starts_runtime(self):
        # 既存構成相当: 旧認証実体あり・同梱/DIYバイナリ無し・ハーネスフラグ無し。
        self._write_credentials()
        self.assertFalse(self.flag.exists())

        # 実 boot 順(daemon.py 末尾): harness_ready() が先。認証ありなので一度だけ migration で
        # フラグを"claude"化し、バイナリ無しのため False(=セットアップ待ちへ)。
        self.assertFalse(daemon.harness_ready())
        self.assertEqual(self.flag.read_text(encoding="utf-8"), "claude\n")

        # 続く notify はフラグが既に"claude"(valid)なので install 案内。グランドファザーの
        # 「再インストール」本文は missing 分岐で、ここ(移行成功)では通らない。
        body = self._notify_once()
        self.assertIn("Claude Codeをインストール", body["message"])
        self.assertEqual(body["notification_id"], daemon._SETUP_WAIT_NOTIFICATION_ID)

        # install 相当: DIYバイナリを実在させる → オーケストレータが ready を検出し runtime 起動。
        self._install_binary()
        self.assertTrue(daemon.harness_ready())
        self._boot_and_expect_runtime()

    # --- シーケンスB: 新規構成 → セットアップ待ち → install+auth → runtime起動 ---
    def test_fresh_setup_selects_and_installs_then_starts_runtime(self):
        # 新規: 認証実体なし・バイナリなし・フラグなし。
        self.assertFalse(daemon.harness_ready())
        self.assertFalse(self.flag.exists())  # 認証が無いのでmigrationも起きない(フラグ不作成)。

        # セットアップ待ち通知: ハーネス選択を促す本文。
        body = self._notify_once()
        self.assertIn("ハーネスを選んでインストール", body["message"])

        # ハーネス選択(claude) + install + auth(認証はモック境界=実credentialファイルまで)。
        harness_state.set_selected_harness("claude")
        self._install_binary()
        self._write_credentials()

        self.assertTrue(daemon.harness_ready())
        self._boot_and_expect_runtime()

    # --- シーケンスC: ログアウト → 自己再起動 → セットアップ待ち復帰 ---
    def test_logout_clears_auth_keeps_memory_and_returns_to_setup_wait(self):
        # ready な claude 構成から開始。
        harness_state.set_selected_harness("claude")
        self._install_binary()
        self._write_credentials()
        memory = self.config_dir / "projects" / "session.json"
        memory.write_text("memory", encoding="utf-8")
        self.assertTrue(daemon.harness_ready())

        # ログアウト: 認証実体は消え、自己再起動が予約される(スケジューラ本体は既存単体でカバー
        # 済みのため、ここでは委譲=呼び出しを確認)。
        handler = object.__new__(server.Handler)
        handler.send_json = mock.Mock()
        with mock.patch.object(server, "_schedule_self_restart") as schedule_restart:
            handler._serve_setup_logout("claude")
        schedule_restart.assert_called_once_with()
        response, status = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertTrue(response["restarting"])
        self.assertIn("セットアップ待ち", response["message"])

        # 認証は消え、記憶(projects)は残る。選択フラグは"claude"のまま(ハーネス選択は保持)。
        self.assertFalse((self.config_dir / ".credentials.json").exists())
        self.assertTrue(memory.exists())
        self.assertEqual(self.flag.read_text(encoding="utf-8"), "claude\n")

        # 再起動後相当: フラグは"claude"・バイナリは残るが認証が無い → setup待ちへ戻り、
        # Claude 向けログイン案内が出る(ハーネス同一性まで固定)。
        self.assertFalse(daemon.harness_ready())
        body = self._notify_once()
        self.assertIn("Claude Codeにログイン", body["message"])


if __name__ == "__main__":
    unittest.main()
