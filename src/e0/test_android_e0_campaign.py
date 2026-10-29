import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from android_e0 import EXPECTED_DEVICE, SCHEME_OPERATIONS, EvidenceError
from android_e0_campaign import (
    CAMPAIGN_ATTEMPT_SCHEMA,
    CAMPAIGN_FILE,
    CAMPAIGN_SCHEMA,
    CAMPAIGN_SOURCE_PATHS,
    LOCK_FILE,
    LOCK_SCHEMA,
    CampaignLock,
    atomic_json,
    cooldown_remaining_seconds,
    load_campaign,
    reconstruct_campaign,
    recover_stale_lock,
    required_resume_cooldowns,
    source_hashes,
    state_snapshot,
    validate_campaign_identity,
)
from run_e0_android import (
    CollectionError,
    _collect_condition,
    _selected_conditions,
    validate_existing_evidence_root,
)


PROVENANCE = {
    "git_commit": "a" * 40,
    "git_tracked_clean": True,
    "benchmark_sha256": "b" * 64,
    "frequency_sampler_sha256": "c" * 64,
    "adb_serial": "test-device",
}


def clock(sequence: int, host="host-boot", android="android-boot") -> dict[str, object]:
    return {
        "host_boot_id": host,
        "host_monotonic_ns": sequence * 1_000_000_000,
        "android_boot_id": android,
        "wall_time_utc": f"2026-01-01T00:00:{sequence:02d}Z",
    }


class CampaignFixture:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir()
        atomic_json(
            root / CAMPAIGN_FILE,
            {
                "schema": CAMPAIGN_SCHEMA,
                "experiment": "E0-Android-v3",
                "scientific": True,
                "registered_conditions": [list(item) for item in SCHEME_OPERATIONS],
                "target_accepted_timings_per_condition": 1000,
                "device": EXPECTED_DEVICE,
                "provenance": PROVENANCE,
                "campaign_manager": "src/e0/run_e0_android.py",
                "source_sha256": {path: "d" * 64 for path in CAMPAIGN_SOURCE_PATHS},
            },
            exclusive=True,
        )

    @staticmethod
    def safe_scheme(scheme: str) -> str:
        return "".join(c if c.isalnum() or c in "_.-" else "_" for c in scheme)

    def attempt(
        self,
        condition: tuple[str, str],
        block: int,
        attempt: int,
        sequence: int,
        status: str,
        *,
        count: int = 0,
        start: int | None = None,
        cooldown: int = 0,
        host="host-boot",
        android="android-boot",
    ) -> Path:
        scheme, operation = condition
        directory = (
            self.root
            / self.safe_scheme(scheme)
            / operation
            / f"block_{block:03d}"
            / f"attempt_{attempt:03d}"
        )
        directory.mkdir(parents=True)
        accepted = status == "accepted"
        start_value = start if accepted else None
        manifest = {
            "status": status,
            "scheme": scheme,
            "operation": operation,
            "block_index": block,
            "attempt_index": attempt,
            "accepted_sample_start": start_value,
            "accepted_sample_end_exclusive": start_value + count if accepted else None,
            "accepted_timing_iterations": count if accepted else 0,
            "cooldown_preceding_seconds": cooldown,
            "provenance": PROVENANCE,
            "device": EXPECTED_DEVICE,
            "files": {},
            "campaign_context": {
                "schema": CAMPAIGN_ATTEMPT_SCHEMA,
                "attempt_sequence": sequence,
                "completion_clock": clock(sequence, host, android),
            },
        }
        atomic_json(directory / "manifest.json", manifest, exclusive=True)
        return directory


def accepted_validator(_directory: Path, manifest: dict[str, object]):
    return [1.0] * int(manifest["accepted_timing_iterations"]), object()


def rejected_validator(_directory: Path, _manifest: dict[str, object]):
    return None


def reconstruct(root: Path):
    return reconstruct_campaign(
        root, accepted_validator=accepted_validator, rejected_validator=rejected_validator
    )


class CampaignSelectionTests(unittest.TestCase):
    def test_one_registered_condition_is_selected(self):
        args = Namespace(
            scheme="ECDSA-P-256", operation="keygen", all_conditions=False
        )
        self.assertEqual(_selected_conditions(args), [("ECDSA-P-256", "keygen")])

    def test_unknown_and_implicit_all_conditions_are_rejected(self):
        with self.assertRaises(CollectionError):
            _selected_conditions(Namespace(scheme="unknown", operation="keygen", all_conditions=False))
        with self.assertRaises(CollectionError):
            _selected_conditions(Namespace(scheme=None, operation=None, all_conditions=False))

    def test_all_conditions_requires_explicit_flag_and_preserves_order(self):
        selected = _selected_conditions(
            Namespace(scheme=None, operation=None, all_conditions=True)
        )
        self.assertEqual(selected, list(SCHEME_OPERATIONS))

    def test_resume_root_must_remain_external_and_non_symlinked(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            project.mkdir()
            external = base / "campaign"
            external.mkdir()
            self.assertEqual(validate_existing_evidence_root(external, project), external)
            with self.assertRaises(CollectionError):
                validate_existing_evidence_root(project, project)
            link = base / "campaign-link"
            link.symlink_to(external, target_is_directory=True)
            with self.assertRaises(CollectionError):
                validate_existing_evidence_root(link, project)


class CampaignReconstructionTests(unittest.TestCase):
    condition = ("SPHINCS+-SHA2-128s", "sign")

    def test_fresh_campaign_and_state_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            state = reconstruct(fixture.root)
            progress = state.conditions[self.condition]
            self.assertEqual((progress.accepted_count, progress.remaining), (0, 1000))
            self.assertEqual((progress.next_block_index, progress.next_attempt_index), (1, 1))
            snapshot = state_snapshot(state)
            self.assertEqual(snapshot["next_campaign_sequence"], 1)
            selected = next(
                value for value in snapshot["conditions"]
                if (value["scheme"], value["operation"]) == self.condition
            )
            self.assertEqual(selected["state"], "incomplete")
            self.assertEqual(selected["pending_cooldown_seconds_without_live_boot_proof"], 0)

    def test_resume_42_then_84_timings_across_invocations(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            fixture.attempt(self.condition, 1, 1, 1, "accepted", count=42, start=0)
            first = reconstruct(fixture.root).conditions[self.condition]
            self.assertEqual((first.accepted_count, first.remaining), (42, 958))
            self.assertEqual((first.next_block_index, first.next_attempt_index), (2, 1))
            fixture.attempt(
                self.condition, 2, 1, 2, "accepted", count=42, start=42, cooldown=180
            )
            second = reconstruct(fixture.root).conditions[self.condition]
            self.assertEqual((second.accepted_count, second.remaining), (84, 916))
            self.assertEqual((second.accepted_blocks, second.next_block_index), (2, 3))

    def test_rejected_timings_never_count_and_terminal_failure_is_sticky(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            fixture.attempt(self.condition, 1, 1, 1, "rejected", cooldown=0)
            fixture.attempt(self.condition, 1, 2, 2, "rejected", cooldown=180)
            fixture.attempt(self.condition, 1, 3, 3, "rejected", cooldown=360)
            progress = reconstruct(fixture.root).conditions[self.condition]
            self.assertEqual(progress.accepted_count, 0)
            self.assertEqual(progress.rejected_attempts, 3)
            self.assertTrue(progress.terminal_failure)
            with self.assertRaises(EvidenceError):
                required_resume_cooldowns(reconstruct(fixture.root), self.condition)

    def test_accepted_block_resets_rejection_counter(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            fixture.attempt(self.condition, 1, 1, 1, "rejected")
            fixture.attempt(self.condition, 1, 2, 2, "accepted", count=42, start=0, cooldown=180)
            progress = reconstruct(fixture.root).conditions[self.condition]
            self.assertEqual(progress.consecutive_rejections, 0)
            self.assertFalse(progress.terminal_failure)

    def test_complete_condition_is_recognized_without_extra_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            fixture.attempt(self.condition, 1, 1, 1, "accepted", count=1000, start=0)
            progress = reconstruct(fixture.root).conditions[self.condition]
            self.assertTrue(progress.complete)
            self.assertEqual(progress.remaining, 0)
            with mock.patch(
                "run_e0_android.reconstruct_campaign", return_value=reconstruct(fixture.root)
            ), mock.patch("run_e0_android.collect_attempt") as collect:
                _collect_condition(Namespace(), object(), fixture.root, PROVENANCE, self.condition)
            collect.assert_not_called()

    def test_overshoot_and_noncontiguous_samples_are_rejected(self):
        for count, start in ((1001, 0), (42, 1)):
            with self.subTest(count=count, start=start), tempfile.TemporaryDirectory() as temporary:
                fixture = CampaignFixture(Path(temporary) / "campaign")
                fixture.attempt(self.condition, 1, 1, 1, "accepted", count=count, start=start)
                with self.assertRaises(EvidenceError):
                    reconstruct(fixture.root)

    def test_interrupted_attempt_is_preserved_and_blocks_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            interrupted = (
                fixture.root / fixture.safe_scheme(self.condition[0]) / self.condition[1]
                / "block_001" / "attempt_001"
            )
            interrupted.mkdir(parents=True)
            (interrupted / "partial.log").write_text("preserve\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceError, "explicit recovery"):
                reconstruct(fixture.root)
            self.assertTrue((interrupted / "partial.log").exists())

    def test_unreceipted_attempt_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            directory = fixture.attempt(
                self.condition, 1, 1, 1, "accepted", count=42, start=0
            )
            (directory / "unexpected.txt").write_text("ambiguous\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceError, "inventory"):
                reconstruct(fixture.root)

    def test_duplicate_or_missing_campaign_sequence_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            fixture.attempt(self.condition, 1, 1, 1, "accepted", count=42, start=0)
            other = ("ECDSA-P-256", "keygen")
            fixture.attempt(other, 1, 1, 1, "accepted", count=42, start=0, cooldown=180)
            with self.assertRaisesRegex(EvidenceError, "sequence"):
                reconstruct(fixture.root)

    def test_unknown_operation_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            (fixture.root / "ECDSA-P-256" / "unknown").mkdir(parents=True)
            with self.assertRaisesRegex(EvidenceError, "unknown campaign operations"):
                reconstruct(fixture.root)

    def test_corrupted_cooldown_history_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            fixture.attempt(self.condition, 1, 1, 1, "rejected")
            fixture.attempt(self.condition, 1, 2, 2, "accepted", count=42, start=0, cooldown=0)
            with self.assertRaisesRegex(EvidenceError, "cooldown history"):
                reconstruct(fixture.root)


class CampaignCooldownTests(unittest.TestCase):
    def test_elapsed_cooldown_is_credited_only_on_same_host_and_android_boot(self):
        recorded = clock(10)
        self.assertEqual(
            cooldown_remaining_seconds(180, recorded, "host-boot", "android-boot", 100_000_000_000),
            90,
        )
        self.assertEqual(
            cooldown_remaining_seconds(180, recorded, "new-host", "android-boot", 100_000_000_000),
            180,
        )
        self.assertEqual(
            cooldown_remaining_seconds(180, recorded, "host-boot", "new-android", 100_000_000_000),
            180,
        )
        self.assertEqual(cooldown_remaining_seconds(180, None, None, None, None), 180)

    def test_cross_condition_restart_requires_normal_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            first = ("ECDSA-P-256", "keygen")
            second = ("ML-KEM-768", "keygen")
            fixture.attempt(first, 1, 1, 1, "accepted", count=1000, start=0)
            constraints = required_resume_cooldowns(reconstruct(fixture.root), second)
            self.assertEqual(constraints, [(180, clock(1))])

    def test_rejection_escalation_and_global_cooldown_both_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            fixture.attempt(
                ("ECDSA-P-256", "keygen"), 1, 1, 1, "accepted", count=42, start=0
            )
            fixture.attempt(
                ("ML-KEM-768", "keygen"), 1, 1, 2, "rejected", cooldown=180
            )
            state = reconstruct(fixture.root)
            constraints = required_resume_cooldowns(state, ("ML-KEM-768", "keygen"))
            self.assertEqual([required for required, _ in constraints], [180, 180])


class CampaignIdentityAndLockTests(unittest.TestCase):
    def test_campaign_source_key_set_cannot_be_missing_or_extended(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            path = fixture.root / CAMPAIGN_FILE
            original = json.loads(path.read_text(encoding="utf-8"))
            for mutation in ("missing", "extra"):
                value = json.loads(json.dumps(original))
                if mutation == "missing":
                    value["source_sha256"].pop(CAMPAIGN_SOURCE_PATHS[0])
                else:
                    value["source_sha256"]["src/e0/not-real.py"] = "f" * 64
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(mutation=mutation), self.assertRaises(EvidenceError):
                    load_campaign(fixture.root)
            path.write_text(json.dumps(original), encoding="utf-8")

    def test_resume_rejects_changed_commit_binary_or_device_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            campaign = load_campaign(fixture.root)
            project = Path(temporary) / "project"
            project.mkdir()
            for key, value in (
                ("git_commit", "d" * 40),
                ("benchmark_sha256", "e" * 64),
                ("adb_serial", "other-device"),
            ):
                changed = dict(PROVENANCE)
                changed[key] = value
                with self.subTest(key=key), self.assertRaises(EvidenceError):
                    validate_campaign_identity(campaign, changed, project)

    def test_changed_or_missing_campaign_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            project = Path(temporary) / "project"
            project.mkdir()
            for relative in CAMPAIGN_SOURCE_PATHS:
                source = project / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(f"{relative}\n", encoding="utf-8")
            campaign = load_campaign(fixture.root)
            campaign["source_sha256"] = source_hashes(project, CAMPAIGN_SOURCE_PATHS)
            validate_campaign_identity(campaign, PROVENANCE, project)
            source = project / CAMPAIGN_SOURCE_PATHS[0]
            source.write_text("two\n", encoding="utf-8")
            with self.assertRaises(EvidenceError):
                validate_campaign_identity(campaign, PROVENANCE, project)
            source.unlink()
            with self.assertRaises(EvidenceError):
                validate_campaign_identity(campaign, PROVENANCE, project)

    def test_concurrent_campaign_writer_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            first = CampaignLock(fixture.root)
            first.acquire()
            try:
                with self.assertRaises(EvidenceError):
                    CampaignLock(fixture.root).acquire()
            finally:
                first.release()

    def test_explicit_stale_lock_recovery_preserves_audit_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary) / "campaign")
            lock = fixture.root / LOCK_FILE
            atomic_json(lock, {
                "schema": LOCK_SCHEMA,
                "pid": 12345,
                "host_boot_id": "previous-host-boot",
            }, exclusive=True)
            destination = recover_stale_lock(fixture.root)
            self.assertFalse(lock.exists())
            self.assertTrue(destination.is_file())
            self.assertEqual(json.loads(destination.read_text()), {
                "schema": LOCK_SCHEMA,
                "pid": 12345,
                "host_boot_id": "previous-host-boot",
            })


if __name__ == "__main__":
    unittest.main()
