import unittest

import config


class ConfigurationTests(unittest.TestCase):
    def test_exact_configuration_registry(self):
        self.assertEqual(
            config.CONFIG_NAMES,
            ("l1_only", "sets_reimpl", "classical_l2", "layer_aware_l2"),
        )
        self.assertEqual(tuple(config.CONFIGURATIONS), config.CONFIG_NAMES)
        config.validate_registry()

    def test_sets_reimplementation_semantics(self):
        sets = config.get_configuration("sets_reimpl")
        self.assertTrue(sets.sets_reimplementation)
        self.assertEqual(sets.cryptography, "ECDSA")
        self.assertIn("Layer 1", sets.settlement_path)
        self.assertFalse(sets.uses_layer2)
        self.assertEqual(
            sets.completion_definition, "committed/validated Fabric transaction status"
        )
        self.assertEqual(
            config.SETS_REIMPL_BEHAVIOR,
            (
                "producer_offer",
                "consumer_request",
                "controller_validate_eligibility_and_balance",
                "record_and_settle_on_layer_1_ledger",
            ),
        )

    def test_project_configuration_crypto_semantics(self):
        self.assertEqual(config.get_configuration("l1_only").cryptography, "ML-DSA-65")
        self.assertIn("E2 classical", config.get_configuration("classical_l2").cryptography)
        layer_aware = config.get_configuration("layer_aware_l2").cryptography
        self.assertIn("Falcon-padded-512", layer_aware)
        self.assertIn("ML-DSA-65", layer_aware)
        self.assertTrue(config.get_configuration("classical_l2").uses_layer2)
        self.assertTrue(config.get_configuration("layer_aware_l2").uses_layer2)

    def test_common_trace_and_latency_semantics(self):
        semantics = config.COMMON_WORKLOAD_SEMANTICS
        self.assertEqual(semantics["settlement_interval_minutes"], 15)
        self.assertFalse(semantics["replay_waits_wall_clock_interval"])
        self.assertFalse(semantics["summary_metric_composition"])
        self.assertIn("same prepared trace", semantics["trace_policy"])
        self.assertIn("processing/submitting", semantics["latency_start"])

    def test_unknown_and_misspelled_config_rejected(self):
        for name in ("unknown", "sets-reimpl", "SETS_reimpl"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unknown"):
                config.get_configuration(name)


if __name__ == "__main__":
    unittest.main()
