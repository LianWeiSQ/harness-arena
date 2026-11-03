import os
import tempfile
import unittest
from pathlib import Path

from evo_arena.scenario import DockerScenario


@unittest.skipUnless(os.environ.get("EVOARENA_DOCKER_TESTS") == "1", "Set EVOARENA_DOCKER_TESTS=1 after building the fixture image")
class DockerTests(unittest.TestCase):
    def test_real_business_oracle_and_variant_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            for seed in [41, 73, 97]:
                with self.subTest(seed=seed), DockerScenario(Path(tmp), "test-" + str(seed), seed) as env:
                    self.assertFalse(env.verify()["passed"])
                    self.assertTrue(env.calibrate()["healthy_passed"])
                    self.assertFalse(env.verify()["passed"])
                    inspected = env.tool({"action": "inspect"})
                    port = inspected["services"][1]["listen"]
                    env.tool({"action": "patch_config", "upstream": "http://" + port})
                    self.assertTrue(env.verify()["passed"])
                    with self.assertRaises(ValueError):
                        env.tool({"action": "patch_config", "upstream": "http://external.invalid:80"})


if __name__ == "__main__":
    unittest.main()
