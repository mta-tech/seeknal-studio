from __future__ import annotations

import csv
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "pilot" / "template"


class PilotFixtureTests(unittest.TestCase):
    def test_project_starts_with_sources_and_no_target_transform(self):
        sources = sorted(path.name for path in (PILOT / "seeknal" / "sources").glob("*.yml"))
        transforms = list((PILOT / "seeknal" / "transforms").glob("*")) if (PILOT / "seeknal" / "transforms").exists() else []
        drafts = list((PILOT / ".seeknal" / "drafts").glob("*")) if (PILOT / ".seeknal" / "drafts").exists() else []

        self.assertEqual(["customers.yml", "orders.yml"], sources)
        self.assertEqual([], transforms)
        self.assertEqual([], drafts)

    def test_expected_output_matches_independent_csv_calculation(self):
        with (PILOT / "data" / "customers.csv").open(newline="") as stream:
            customers = {row["customer_id"]: row for row in csv.DictReader(stream)}
        actual: dict[str, dict[str, object]] = {}
        with (PILOT / "data" / "orders.csv").open(newline="") as stream:
            for order in csv.DictReader(stream):
                if order["status"] != "paid":
                    continue
                customer = customers[order["customer_id"]]
                row = actual.setdefault(
                    order["customer_id"],
                    {
                        "customer_id": order["customer_id"],
                        "customer_name": customer["customer_name"],
                        "segment": customer["segment"],
                        "lifetime_value": 0,
                        "orders": 0,
                    },
                )
                row["lifetime_value"] += int(order["amount"])
                row["orders"] += 1

        expected = json.loads((PILOT / "EXPECTED_OUTPUT.json").read_text())["rows"]
        self.assertEqual(expected, [actual[key] for key in sorted(actual)])

    def test_credential_is_not_copied_into_pilot_project(self):
        for path in PILOT.rglob("*"):
            if path.is_file():
                self.assertNotIn("sumopod-ai-api-key", path.read_text(errors="ignore"))


if __name__ == "__main__":
    unittest.main()
