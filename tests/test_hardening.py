"""Hardening test suite (stdlib only, no external deps).

Run:  bash tests/run_tests.sh   (or python3 -m unittest discover -s tests -v)
"""
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
DEPLOY = REPO / "deploy"
WORKFLOWS = REPO / ".github" / "workflows"

FAKE_SECRET = "SUPERSECRET-TOKEN-abcdef0123456789"


def make_template_db(path, loglevel="debug", include_log=True, extra_rows=None):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
    tpl = {
        "log": {
            "access": "/var/log/xray-access.log",
            "error": "/var/log/xray-error.log",
            "loglevel": loglevel,
            "dnsLog": False,
        },
        "routing": {"rules": [{"outboundTag": "direct", "type": "field"}]},
        "inbounds": [],
    }
    if not include_log:
        tpl.pop("log")
    rows = [
        ("webPort", "2096"),
        ("secret", FAKE_SECRET),
        ("xrayTemplateConfig", json.dumps(tpl)),
    ]
    if extra_rows:
        rows.extend(extra_rows)
    conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def read_template_loglevel(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='xrayTemplateConfig'"
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]).get("log", {}).get("loglevel")


class TestSetXrayLoglevel(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "set_xray_loglevel.py"), *args],
            capture_output=True, text=True,
        )

    def test_dry_run_changes_nothing_and_signals_would_change(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "x-ui.db")
            make_template_db(db, loglevel="debug")
            r = self.run_script("--db", db, "--loglevel", "warning")
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("would change", r.stdout)
            self.assertIn("dry-run", r.stdout)
            self.assertEqual(read_template_loglevel(db), "debug")

    def test_apply_sets_warning_and_preserves_other_keys(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "x-ui.db")
            make_template_db(db, loglevel="debug")
            r = self.run_script("--db", db, "--loglevel", "warning",
                                "--apply", "--backup-dir", td)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(read_template_loglevel(db), "warning")
            # other keys intact
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            keys = {k for (k,) in conn.execute("SELECT key FROM settings")}
            conn.close()
            self.assertIn("webPort", keys)
            self.assertIn("secret", keys)

    def test_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "x-ui.db")
            make_template_db(db, loglevel="warning")
            r = self.run_script("--db", db, "--loglevel", "warning",
                                "--apply", "--backup-dir", td)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no-op", r.stdout)

    def test_missing_log_section_is_created(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "x-ui.db")
            make_template_db(db, include_log=False)
            r = self.run_script("--db", db, "--loglevel", "warning",
                                "--apply", "--backup-dir", td)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(read_template_loglevel(db), "warning")

    def test_backup_of_previous_value_created(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "x-ui.db")
            make_template_db(db, loglevel="debug")
            bdir = Path(td) / "backups"
            self.run_script("--db", db, "--loglevel", "warning",
                            "--apply", "--backup-dir", str(bdir))
            backups = list(bdir.glob("xrayTemplateConfig.*.json"))
            self.assertEqual(len(backups), 1)
            saved = json.loads(backups[0].read_text())
            self.assertEqual(saved["log"]["loglevel"], "debug")
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_invalid_json_fails_without_changes(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "x-ui.db")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE settings (key TEXT, value TEXT)")
            conn.execute("INSERT INTO settings VALUES ('xrayTemplateConfig', 'not-json{')")
            conn.commit(); conn.close()
            r = self.run_script("--db", db, "--loglevel", "warning", "--apply",
                                "--backup-dir", td)
            self.assertEqual(r.returncode, 1)
            self.assertIn("not valid JSON", r.stderr)

    def test_no_secret_values_in_output(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "x-ui.db")
            make_template_db(db, loglevel="debug")
            r = self.run_script("--db", db, "--loglevel", "warning",
                                "--apply", "--backup-dir", td)
            self.assertNotIn(FAKE_SECRET, r.stdout + r.stderr)


class TestDbBackup(unittest.TestCase):
    def make_src(self, path):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, fio TEXT, secret TEXT)")
        conn.execute("INSERT INTO users (fio, secret) VALUES ('test-user', ?)", (FAKE_SECRET,))
        conn.commit(); conn.close()

    def run_backup(self, src, bdir, keep="14"):
        env = dict(os.environ,
                   NETLINK_DB_PATH=str(src),
                   NETLINK_BACKUP_DIR=str(bdir),
                   NETLINK_BACKUP_KEEP=keep)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "netlink_db_backup.py")],
            capture_output=True, text=True, env=env,
        )

    def test_backup_consistent_and_integrity_ok(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "netlink.db"
            self.make_src(src)
            # hold a write transaction open on the source, like the live bot
            writer = sqlite3.connect(src)
            writer.execute("BEGIN")
            writer.execute("INSERT INTO users (fio, secret) VALUES ('locked', 'x')")
            r = self.run_backup(src, Path(td) / "backups")
            writer.rollback(); writer.close()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("integrity_check=ok", r.stdout)
            backups = list((Path(td) / "backups").glob("netlink-*.db"))
            self.assertEqual(len(backups), 1)
            # data readable in the copy
            conn = sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True)
            rows = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(rows, 1)
            # permissions
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((Path(td) / "backups").stat().st_mode), 0o700)

    def test_retention_keeps_14(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "netlink.db"
            self.make_src(src)
            bdir = Path(td) / "backups"
            bdir.mkdir()
            for i in range(20):
                (bdir / f"netlink-2026010{i % 10}-{i:04d}.db").write_bytes(b"old")
            r = self.run_backup(src, bdir, keep="14")
            self.assertEqual(r.returncode, 0, r.stderr)
            remaining = list(bdir.glob("netlink-*.db"))
            self.assertEqual(len(remaining), 14)
            self.assertIn("removed 7", r.stdout)  # 20 fake + 1 new = 21 -> keep 14

    def test_missing_source_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_backup(Path(td) / "nope.db", Path(td) / "backups")
            self.assertEqual(r.returncode, 1)
            self.assertIn("not found", r.stderr)

    def test_no_secret_values_in_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "netlink.db"
            self.make_src(src)
            r = self.run_backup(src, Path(td) / "backups")
            self.assertNotIn(FAKE_SECRET, r.stdout + r.stderr)


class TestSwapScript(unittest.TestCase):
    def test_syntax(self):
        r = subprocess.run(["bash", "-n", str(SCRIPTS / "setup_swap.sh")])
        self.assertEqual(r.returncode, 0)

    def test_print_fstab_line(self):
        r = subprocess.run(
            ["bash", str(SCRIPTS / "setup_swap.sh"), "--print-fstab-line"],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "/swapfile none swap sw 0 0")

    def test_dry_run_changes_nothing_and_reports(self):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, SWAP_FILE=str(Path(td) / "swapfile"),
                       NETLINK_SWAP_FSTAB=str(Path(td) / "fstab"),
                       NETLINK_SYSCTL_DIR=str(Path(td) / "sysctl.d"))
            r = subprocess.run(["bash", str(SCRIPTS / "setup_swap.sh")],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("DRY-RUN", r.stdout)
            self.assertFalse((Path(td) / "swapfile").exists())
            self.assertFalse((Path(td) / "sysctl.d").exists())

    def test_fstab_entry_detected_no_duplication(self):
        with tempfile.TemporaryDirectory() as td:
            # neutralize host swap state: pretend there is no active swap
            meminfo = Path(td) / "meminfo"
            meminfo.write_text("SwapTotal:       0 kB\nSwapFree:        0 kB\n")
            swapfile = str(Path(td) / "swapfile")
            fstab = Path(td) / "fstab"
            fstab.write_text(f"{swapfile} none swap sw 0 0\n")
            env = dict(os.environ, SWAP_FILE=swapfile,
                       NETLINK_SWAP_FSTAB=str(fstab),
                       NETLINK_SYSCTL_DIR=str(Path(td) / "sysctl.d"),
                       MEMINFO_FILE=str(meminfo))
            r = subprocess.run(["bash", str(SCRIPTS / "setup_swap.sh")],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("already present", r.stdout)
            # dry-run must not touch fstab
            self.assertEqual(fstab.read_text(), f"{swapfile} none swap sw 0 0\n")

    def test_insufficient_space_aborts(self):
        with tempfile.TemporaryDirectory() as td:
            tiny = Path(td) / "tiny"
            tiny.mkdir()
            # 1 MB filesystem is impossible to fake portably; instead force a huge
            # required size so the preflight math can never pass
            env = dict(os.environ, SWAP_FILE=str(tiny / "swapfile"),
                       SWAP_SIZE_MB="99999999",
                       NETLINK_SWAP_FSTAB=str(Path(td) / "fstab"),
                       NETLINK_SYSCTL_DIR=str(Path(td) / "sysctl.d"))
            r = subprocess.run(["bash", str(SCRIPTS / "setup_swap.sh")],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("insufficient free space", r.stderr)


class TestJournaldScript(unittest.TestCase):
    WANT_LINES = ["SystemMaxUse=256M", "RuntimeMaxUse=64M", "MaxRetentionSec=14day"]

    def test_syntax(self):
        r = subprocess.run(["bash", "-n", str(SCRIPTS / "install_journald_limits.sh")])
        self.assertEqual(r.returncode, 0)

    def test_apply_creates_exact_dropin_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            ddir = Path(td) / "journald.conf.d"
            env = dict(os.environ, NETLINK_JOURNALD_DIR=str(ddir))
            # dry-run first: no file created
            r = subprocess.run(["bash", str(SCRIPTS / "install_journald_limits.sh")],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(ddir.exists())
            # apply (without journald restart: not root/systemd here).
            # The script calls systemctl only after writing; emulate by making
            # systemctl a no-op stub on PATH.
            stub = Path(td) / "bin"
            stub.mkdir()
            (stub / "systemctl").write_text("#!/bin/bash\nexit 0\n")
            (stub / "systemctl").chmod(0o755)
            (stub / "journalctl").write_text("#!/bin/bash\necho 'archived journals: 1.7G'\n")
            (stub / "journalctl").chmod(0o755)
            env["PATH"] = f"{stub}:{env['PATH']}"
            r = subprocess.run(["bash", str(SCRIPTS / "install_journald_limits.sh"), "--apply"],
                               capture_output=True, text=True, env=env)
            # systemctl stub returns 0 for restart, vacuum journalctl returns text
            self.assertEqual(r.returncode, 0, r.stderr)
            dropin = ddir / "90-netlink-hardening.conf"
            self.assertTrue(dropin.exists())
            content = dropin.read_text()
            for line in self.WANT_LINES:
                self.assertIn(line, content)
            # second apply -> no-op
            r2 = subprocess.run(["bash", str(SCRIPTS / "install_journald_limits.sh"), "--apply"],
                                capture_output=True, text=True, env=env)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("no-op", r2.stdout)


class TestLogrotateConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = (DEPLOY / "xray-logs").read_text()

    def test_required_directives(self):
        for token in ("daily", "maxsize 100M", "rotate 7", "compress", "delaycompress",
                      "missingok", "notifempty", "copytruncate", "su root root"):
            self.assertIn(token, self.cfg)
        self.assertIn("/var/log/xray-access.log", self.cfg)
        self.assertIn("/var/log/xray-error.log", self.cfg)

    def test_no_standalone_size_directive(self):
        # `size` overrides/removes the time interval in logrotate; only maxsize
        # (rotate at least daily, earlier if bigger) implements the target.
        standalone = re.search(r"^\s*size\s+\S+", self.cfg, re.MULTILINE)
        self.assertIsNone(standalone,
                          "standalone `size` directive must not be present "
                          "(it cancels `daily`)")

    @unittest.skipUnless(shutil.which("logrotate"), "logrotate not installed")
    def test_logrotate_parses_and_dryruns(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ("xray-access.log", "xray-error.log"):
                (Path(td) / name).write_text("x" * 100)
            state = str(Path(td) / "state")
            r = subprocess.run(
                ["logrotate", "-s", state, "--debug", str(DEPLOY / "xray-logs")],
                capture_output=True, text=True,
            )
            out = r.stdout + r.stderr
            # config fully parsed -> logrotate prints the rotation plan
            self.assertIn("rotating pattern:", out)
            self.assertIn("7 rotations", out)
            # as non-root the `su root root` directive cannot switch euid here;
            # that is expected locally and harmless (production runs as root)
            is_root = os.geteuid() == 0
            if not is_root:
                self.assertIn("switching euid", out)
            else:
                self.assertEqual(r.returncode, 0, out)


class TestVerifyBackupScript(unittest.TestCase):
    """Regression: proof of a successful oneshot backup (fresh/non-empty/integrity)."""

    def run_verify(self, bdir, max_age=None):
        env = dict(os.environ)
        if max_age is not None:
            env["VERIFY_MAX_AGE_SEC"] = str(max_age)
        return subprocess.run(
            ["bash", str(SCRIPTS / "verify_backup.sh"), str(bdir)],
            capture_output=True, text=True, env=env,
        )

    @staticmethod
    def make_valid_backup(path):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, fio TEXT)")
        conn.execute("INSERT INTO users (fio) VALUES ('u1')")
        conn.commit(); conn.close()

    def test_syntax(self):
        r = subprocess.run(["bash", "-n", str(SCRIPTS / "verify_backup.sh")])
        self.assertEqual(r.returncode, 0)

    def test_fresh_valid_backup_passes(self):
        with tempfile.TemporaryDirectory() as td:
            self.make_valid_backup(Path(td) / "netlink-20261029-010101.db")
            r = self.run_verify(td)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("integrity_check=ok", r.stdout)

    def test_stale_backup_fails(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "netlink-old.db"
            self.make_valid_backup(f)
            old = f.stat().st_mtime - 7200
            os.utime(f, (old, old))
            r = self.run_verify(td)
            self.assertEqual(r.returncode, 1)
            self.assertIn("stale", r.stderr)

    def test_empty_backup_fails(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "netlink-empty.db").write_bytes(b"")
            r = self.run_verify(td)
            self.assertEqual(r.returncode, 1)
            self.assertIn("empty", r.stderr)

    def test_corrupt_backup_fails_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "netlink-corrupt.db").write_bytes(b"this is not sqlite" * 100)
            r = self.run_verify(td)
            self.assertEqual(r.returncode, 1)
            self.assertIn("integrity_check failed", r.stderr)

    def test_missing_directory_fails(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_verify(Path(td) / "nope")
            self.assertEqual(r.returncode, 1)
            self.assertIn("no netlink-", r.stderr)


class TestOneshotSemanticsInWorkflow(unittest.TestCase):
    """Regression: workflow must not judge a successful oneshot by is-active."""

    @classmethod
    def setUpClass(cls):
        cls.raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()

    def test_uses_result_not_isactive_for_oneshot(self):
        self.assertIn("-p Result", self.raw)
        self.assertIn("-p ExecMainStatus", self.raw)
        self.assertNotIn("is-active netlink-db-backup.service", self.raw)
        self.assertNotIn("is-active --quiet netlink-db-backup", self.raw)
        # after-report must explain inactive as normal for oneshot
        self.assertIn("inactive is expected for oneshot", self.raw)

    def test_backup_proof_uses_verify_script_and_gates(self):
        self.assertIn("verify_backup.sh", self.raw)
        self.assertIn('abort 26 "backup service Result=', self.raw)
        self.assertIn('abort 27 "backup service ExecMainStatus=', self.raw)

    def test_no_remainafterexit_added(self):
        unit = (DEPLOY / "netlink-db-backup.service").read_text()
        self.assertNotIn("RemainAfterExit", unit)


class TestXrayLogrotateHourlyUnits(unittest.TestCase):
    """Regression: hourly enforcement of maxsize between daily logrotate runs."""

    @classmethod
    def setUpClass(cls):
        cls.svc = (DEPLOY / "netlink-xray-logrotate.service").read_text()
        cls.timer = (DEPLOY / "netlink-xray-logrotate.timer").read_text()
        cls.stab = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        cls.deploy = (WORKFLOWS / "deploy.yml").read_text()

    def test_service_execstart_uses_system_logrotate_without_force(self):
        self.assertIn("Type=oneshot", self.svc)
        self.assertIn("ExecStart=/usr/sbin/logrotate /etc/logrotate.d/xray", self.svc)
        self.assertNotIn("--force", self.svc)
        self.assertNotIn(" -f", self.svc)
        # must NOT use a private state file: shared rotation history with
        # the system logrotate run
        self.assertNotIn("-s ", self.svc)
        self.assertNotIn("--state", self.svc)

    def test_service_tolerates_lock_collision_exit_3(self):
        self.assertIn("SuccessExitStatus=3", self.svc)

    def test_timer_hourly_persistent(self):
        self.assertIn("OnCalendar=hourly", self.timer)
        self.assertIn("RandomizedDelaySec=10m", self.timer)
        self.assertIn("Persistent=true", self.timer)

    def test_stabilize_installs_enables_and_proves_units(self):
        for artifact in ("netlink-xray-logrotate.service",
                         "netlink-xray-logrotate.timer"):
            self.assertIn(artifact, self.stab)
        self.assertIn("is-enabled netlink-xray-logrotate.timer", self.stab)
        self.assertIn("systemctl start netlink-xray-logrotate.service", self.stab)
        # judged by Result (SuccessExitStatus=3 makes ExecMainStatus 0 or 3 fine)
        self.assertIn('abort 29 "hourly logrotate service Result=', self.stab)
        # present in the final after-report
        self.assertRegex(self.stab, r"list-timers[^\n]*netlink-xray-logrotate")

    def test_deploy_yml_does_not_know_hourly_units(self):
        self.assertNotIn("netlink-xray-logrotate", self.deploy)

    def test_logrotate_policy_unchanged(self):
        cfg = (DEPLOY / "xray-logs").read_text()
        self.assertIn("daily", cfg)
        self.assertIn("maxsize 100M", cfg)
        self.assertIsNone(re.search(r"^\s*size\s+\S+", cfg, re.MULTILINE))


class TestWorkflowNoPipefailTraps(unittest.TestCase):
    """Regression for run 40888505148: `cmd | grep -q ...` under `set -o pipefail`.

    grep -q exits on first match -> upstream producer can get SIGPIPE (141)
    -> pipeline fails despite the check succeeding -> false ABORT on a valid
    state. The stabilize workflow must not contain piped grep -q/-qw/-qx
    inside its SSH script (here-string grep without a producer pipe is fine).
    """

    PIPE_GREP_Q = re.compile(r"\|\s*grep\s+-\w*q")

    def test_stabilize_has_no_piped_grep_q(self):
        raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        offenders = [line.strip() for line in raw.splitlines()
                     if self.PIPE_GREP_Q.search(line)]
        self.assertEqual(
            offenders, [],
            "piped grep -q* found in stabilize workflow (pipefail+SIGPIPE "
            "false-failure risk):\n" + "\n".join(offenders))


class TestHotfixWorkflowControlFlow(unittest.TestCase):
    """Regression tests for the pipefail/SIGPIPE hotfix (run 40888505148)."""

    @classmethod
    def setUpClass(cls):
        cls.raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()

    def test_both_logrotate_validations_capture_output(self):
        # two separate captures: initial validation + final verification
        self.assertRegex(self.raw, r'LOGROTATE_DEBUG="\$\(logrotate .*2>&1\)"')
        self.assertRegex(self.raw, r'LOGROTATE_DEBUG2="\$\(logrotate .*2>&1\)"')

    def test_logrotate_exit_code_is_really_checked(self):
        self.assertIn('if ! LOGROTATE_DEBUG="$(logrotate', self.raw)
        self.assertIn('if ! LOGROTATE_DEBUG2="$(logrotate', self.raw)

    def test_initial_validation_hard_abort(self):
        self.assertGreaterEqual(self.raw.count("abort 24"), 2)  # parser rc + marker

    def test_final_validation_hard_abort(self):
        self.assertGreaterEqual(self.raw.count("abort 40"), 2)

    def test_ip_gate_preserved_without_pipeline(self):
        self.assertIn('IP_OUTPUT="$(ip -4 addr show)"', self.raw)
        self.assertIn('grep -qw "$EXPECTED_IP" <<<"$IP_OUTPUT"', self.raw)
        self.assertIn('abort 21 "expected IP not present on interfaces"', self.raw)
        self.assertNotIn("ip -4 addr show |", self.raw)

    def test_both_443_gates_preserved_without_pipeline(self):
        self.assertIn('LISTEN443="$(ss -tln)"', self.raw)
        self.assertIn('LISTEN443B="$(ss -tln)"', self.raw)
        self.assertIn('[[ "$LISTEN443" == *":443 "* ]] || abort 31', self.raw)
        self.assertIn('[[ "$LISTEN443B" == *":443 "* ]] || abort 43', self.raw)
        self.assertNotIn("ss -tln |", self.raw)

    def test_no_journalctl_head_pipeline(self):
        self.assertNotIn("journalctl --disk-usage | head", self.raw)
        # plain invocation present (compact output already), one full line
        self.assertIsNotNone(
            re.search(r"^\s*journalctl --disk-usage\s*$", self.raw, re.MULTILINE),
            "journalctl --disk-usage must run without a pipe")

    def test_abort_helper_prints_rollback_material(self):
        self.assertIn("abort() {", self.raw)
        self.assertIn('echo "Rollback material: $SNAP ; runbook: docs/LEGACY_HARDENING.md"',
                      self.raw)
        self.assertIn('echo "ABORT: $* (exit code $code)"', self.raw)

    def test_exit_trap_for_unexpected_failures_only(self):
        self.assertIn("trap 'rc=$?", self.raw)
        self.assertIn("STABILIZE FAILED rc=$rc", self.raw)
        # trap must not fire on success and must not alter rc
        self.assertIn('[ "$rc" -ne 0 ]', self.raw)
        self.assertIn('ABORTED=1', self.raw)

    def test_no_bare_echo_exit_gates(self):
        # controlled aborts must use abort(); the old form
        # '|| { echo "ABORT ..."; exit N; }' is forbidden. Printing extra
        # diagnostics before abort() (e.g. captured logrotate output) is fine.
        self.assertIsNone(re.search(r'\{\s*echo\s+"ABORT', self.raw))
        self.assertIsNone(re.search(r'\|\|\s*\{[^}]*;\s*exit\s+\d+\s*;\s*\}', self.raw))
        # and there are many controlled abort gates
        self.assertGreaterEqual(len(re.findall(r"abort \d+ ", self.raw)), 12)


class TestWorkflowsStatic(unittest.TestCase):
    @staticmethod
    def load(name):
        import yaml
        return yaml.safe_load((WORKFLOWS / name).read_text())

    def test_stabilize_is_manual_dispatch_only(self):
        doc = self.load("stabilize-legacy.yml")
        self.assertIn("workflow_dispatch", doc[True] if True in doc else doc["on"])
        triggers = doc[True] if True in doc else doc["on"]
        self.assertNotIn("push", triggers)
        self.assertNotIn("schedule", triggers)

    def test_stabilize_has_confirmation_gate(self):
        raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        self.assertIn("inputs.confirm == 'stabilize'", raw)

    def test_stabilize_uses_same_ssh_mechanism_and_identity_gates(self):
        raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        self.assertIn("appleboy/ssh-action@v1", raw)
        self.assertIn("secrets.SSH_PRIVATE_KEY", raw)
        self.assertIn("hostname mismatch", raw)
        self.assertIn("egress IP mismatch", raw)
        self.assertIn("ABORT", raw)
        self.assertIn("port: 22847", raw)

    def test_stabilize_never_reboots(self):
        raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        # no reboot/shutdown COMMANDS may appear (comments mentioning the word are fine)
        for cmd in ("systemctl reboot", "systemctl poweroff", "shutdown -r",
                    "shutdown -h", "reboot now", "/sbin/reboot", "/sbin/shutdown"):
            self.assertNotIn(cmd, raw)

    def test_deploy_is_push_main_only(self):
        doc = self.load("deploy.yml")
        triggers = doc[True] if True in doc else doc["on"]
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertEqual(len(triggers), 1, "deploy.yml must not gain new triggers")

    def test_merge_does_not_install_hardening_artifacts(self):
        """Regression: merging this PR must NOT roll out hardening.

        deploy.yml must stay byte-identical in behavior to pre-PR: no backup
        units, no xray logrotate install, no backup timer enable.
        """
        raw = (WORKFLOWS / "deploy.yml").read_text()
        for banned in ("netlink-db-backup.service", "netlink-db-backup.timer",
                       "netlink-db-backup", "/etc/logrotate.d/xray", "deploy/xray-logs"):
            self.assertNotIn(banned, raw,
                             f"deploy.yml must not reference hardening artifact {banned!r}")
        # and it must still do its original job
        for needed in ("netlink-profile.service", "netlink-ip-monitor.timer",
                       "netlink-temp-cleanup.timer"):
            self.assertIn(needed, raw)

    def test_deploy_yml_identical_to_main(self):
        """Strongest form: the file matches main exactly (no diff at all).

        Must FAIL (not silently pass) when origin/main is unavailable: an
        empty diff from a failed git invocation would be a false positive.
        CI checkouts use fetch-depth: 0 so origin/main is present.
        """
        import subprocess as sp
        rr = sp.run(["git", "rev-parse", "--verify", "--quiet", "origin/main"],
                    cwd=REPO, capture_output=True, text=True)
        self.assertEqual(
            rr.returncode, 0,
            "origin/main must resolve for the deploy.yml diff check "
            "(use fetch-depth: 0 in CI); refusing to pass on missing ref")
        r = sp.run(["git", "diff", "origin/main", "--", ".github/workflows/deploy.yml"],
                   cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "", "deploy.yml must not differ from origin/main")

    def test_hardening_installs_live_only_in_stabilize(self):
        stab = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        for artifact in ("netlink-db-backup.service", "netlink-db-backup.timer",
                         "/etc/logrotate.d/xray"):
            self.assertIn(artifact, stab,
                          f"stabilize workflow must install {artifact!r}")
        self.assertIn("setup_swap.sh --apply", stab)
        self.assertIn("install_journald_limits.sh --apply", stab)
        self.assertIn("set_xray_loglevel.py", stab)

    def test_stabilize_verification_section(self):
        raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        for token in ("free -h", "swapon --show", "df -h", "journalctl --disk-usage",
                      "healthz", ":443", "logrotate"):
            self.assertIn(token, raw)

    def test_stabilize_failures_are_hard_not_informational(self):
        """Regression: checks must abort, not just print."""
        raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        # healthz must fail the run (curl -f), :443 checks must abort
        self.assertIn("curl -fs", raw)
        self.assertIn('abort 42 "profile server healthz failed"', raw)
        self.assertIn('abort 43 ":443 not listening in final verification"', raw)
        # logrotate validation gates abort in BOTH places (apply + final verify)
        self.assertGreaterEqual(raw.count("abort 24"), 2)
        self.assertGreaterEqual(raw.count("abort 40"), 2)
        # no validation line may be swallowed by || true
        for line in raw.splitlines():
            if "rotating pattern" in line:
                self.assertNotIn("|| true", line)
        # x-ui liveness beyond the socket: returning client connections
        self.assertIn("established", raw)
        self.assertIn("no client connections returned", raw)

    def test_stabilize_err_trap_points_to_rollback(self):
        raw = (WORKFLOWS / "stabilize-legacy.yml").read_text()
        self.assertIn("trap '", raw)
        self.assertIn("STABILIZE FAILED", raw)
        self.assertIn("Rollback material", raw)


class TestCiWorkflowStatic(unittest.TestCase):
    def test_ci_triggers(self):
        import yaml
        doc = yaml.safe_load((WORKFLOWS / "test.yml").read_text())
        triggers = doc[True] if True in doc else doc["on"]
        self.assertIn("pull_request", triggers)
        self.assertIn("push", triggers)
        self.assertEqual(triggers["push"]["branches-ignore"], ["main"])

    def test_ci_is_safe_no_ssh_no_secrets_no_deploy(self):
        raw = (WORKFLOWS / "test.yml").read_text()
        self.assertIn("run_tests.sh", raw)
        for banned in ("ssh-action", "secrets.", "SSH_PRIVATE_KEY", "scp ",
                       "rsync ", "systemctl", "153.75.85.138", "appleboy"):
            self.assertNotIn(banned, raw, f"CI must not contain {banned!r}")


if __name__ == "__main__":
    unittest.main()
