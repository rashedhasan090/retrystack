import json, os, subprocess, sys, unittest
from retrystack.core import load, analyze, amplification, worst_latency, Edge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rules(fs):
    return {f.rule for f in fs}


class T(unittest.TestCase):
    def test_amplification_multiplies(self):
        g = load({"calls": [
            {"from": "a", "to": "b", "attempts": 3, "timeout_ms": 1000, "backoff_ms": 10, "jitter": True},
            {"from": "b", "to": "c", "attempts": 4, "timeout_ms": 100, "backoff_ms": 10, "jitter": True}]})
        self.assertEqual(amplification(g, "a")["c"], 12)
        self.assertIn("AMPLIFICATION", rules(analyze(g)))
        self.assertIn("LAYERED_RETRY", rules(analyze(g)))

    def test_timeout_inversion(self):
        g = load({"services": [{"name": "b", "latency_ms": 500}],
                  "calls": [{"from": "a", "to": "b", "timeout_ms": 200}]})
        fs = analyze(g)
        inv = [f for f in fs if f.rule == "TIMEOUT_INVERSION"]
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0].severity, "medium")

    def test_no_inversion_when_timeout_covers(self):
        g = load({"services": [{"name": "b", "latency_ms": 100}],
                  "calls": [{"from": "a", "to": "b", "timeout_ms": 200}]})
        self.assertNotIn("TIMEOUT_INVERSION", rules(analyze(g)))

    def test_backoff_exponential_capped(self):
        e = Edge("a", "b", attempts=4, backoff_ms=100, backoff="exponential", max_backoff_ms=250)
        self.assertEqual(e.backoff_total(), 100 + 200 + 250)

    def test_worst_latency_and_deadline(self):
        g = load({"services": [{"name": "a", "entry": True, "deadline_ms": 500},
                               {"name": "b", "latency_ms": 300}],
                  "calls": [{"from": "a", "to": "b", "attempts": 2, "timeout_ms": 400,
                             "backoff_ms": 50, "jitter": True}]})
        self.assertEqual(worst_latency(g)["a"], 2 * 300 + 50)
        self.assertIn("DEADLINE_OVERRUN", rules(analyze(g)))

    def test_cycle_and_no_timeout(self):
        g = load({"services": [{"name": "a", "entry": True}],
                  "calls": [{"from": "a", "to": "b", "timeout_ms": 10},
                            {"from": "b", "to": "a", "attempts": 2, "backoff_ms": 5, "jitter": True}]})
        r = rules(analyze(g))
        self.assertIn("CYCLE", r)
        self.assertIn("NO_TIMEOUT", r)

    def test_zero_backoff_and_no_jitter(self):
        g = load({"calls": [{"from": "a", "to": "b", "attempts": 3, "timeout_ms": 10},
                            {"from": "a", "to": "c", "attempts": 2, "timeout_ms": 10, "backoff_ms": 5}]})
        r = rules(analyze(g))
        self.assertIn("ZERO_BACKOFF", r)
        self.assertIn("NO_JITTER", r)

    def test_bad_attempts(self):
        with self.assertRaises(ValueError):
            load({"calls": [{"from": "a", "to": "b", "attempts": 0}]})


class CLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "retrystack", *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_example_fails_gate(self):
        r = self.run_cli("examples/checkout.toml", "--fail-on", "high")
        self.assertEqual(r.returncode, 1)
        self.assertIn("AMPLIFICATION", r.stdout)
        self.assertIn("36 requests to ledger-db", r.stdout)

    def test_healthy_passes(self):
        r = self.run_cli("examples/healthy.json", "--format", "json", "--fail-on", "medium")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(json.loads(r.stdout), [])

    def test_md(self):
        r = self.run_cli("examples/checkout.toml", "--format", "md")
        self.assertTrue(r.stdout.startswith("| severity"))


if __name__ == "__main__":
    unittest.main()
