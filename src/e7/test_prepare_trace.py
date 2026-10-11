import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import prepare_trace


TIMESTAMP = "utc_timestamp"
INTERPOLATED = "interpolated"
PV = "DE_KN_residential3_pv"
GRID_IMPORT = "DE_KN_residential3_grid_import"
GRID_EXPORT = "DE_KN_residential3_grid_export"
UNRELATED = "DE_KN_residential4_pv"


def upstream_bytes(row_count=prepare_trace.MINIMUM_SOURCE_ROWS, mutation=None):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (TIMESTAMP, INTERPOLATED, PV, GRID_IMPORT, GRID_EXPORT, UNRELATED)
    )
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(row_count):
        rows.append(
            [
                (start + index * timedelta(minutes=15)).isoformat(),
                "",
                str(1 + index / 10000),
                str(0.5 + index / 20000),
                str(0.25 + index / 40000),
                "9",
            ]
        )
    if mutation is not None:
        mutation(rows)
    writer.writerows(rows)
    return output.getvalue().encode()


def select(content):
    return prepare_trace.select_window(
        content,
        timestamp_column=TIMESTAMP,
        pv_column=PV,
        grid_import_column=GRID_IMPORT,
        grid_export_column=GRID_EXPORT,
    )


class PrepareTraceTests(unittest.TestCase):
    def prepare(self, root, content):
        source = root / prepare_trace.UPSTREAM_FILENAME
        trace = root / "opsd_household_15min_e7.csv"
        provenance = root / "provenance.json"
        source.write_bytes(content)
        prepare_trace.prepare_trace(
            source,
            trace,
            provenance,
            timestamp_column=TIMESTAMP,
            pv_column=PV,
            grid_import_column=GRID_IMPORT,
            grid_export_column=GRID_EXPORT,
        )
        return source, trace, provenance

    def test_valid_trace_preparation_demand_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = upstream_bytes()
            source, trace, provenance_path = self.prepare(root, content)
            with trace.open(newline="", encoding="utf-8") as prepared:
                rows = list(csv.DictReader(prepared))
            self.assertEqual(len(rows), 1100)
            self.assertEqual(tuple(rows[0]), prepare_trace.TRACE_FIELDS)
            source_rows = list(
                csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
            )
            index = 77
            previous = source_rows[index]
            current = source_rows[index + 1]
            pv = float(current[PV]) - float(previous[PV])
            grid_import = float(current[GRID_IMPORT]) - float(previous[GRID_IMPORT])
            grid_export = float(current[GRID_EXPORT]) - float(previous[GRID_EXPORT])
            self.assertAlmostEqual(
                float(rows[index]["demand_kwh"]), pv + grid_import - grid_export
            )
            self.assertAlmostEqual(
                float(rows[index]["net_kwh"]), grid_export - grid_import
            )
            provenance = json.loads(provenance_path.read_text())
            self.assertEqual(provenance["dataset"]["name"], prepare_trace.DATASET_NAME)
            self.assertEqual(provenance["dataset"]["version"], "2020-04-15")
            self.assertEqual(
                provenance["dataset"]["licence"],
                "Creative Commons Attribution-International",
            )
            self.assertEqual(
                provenance["selected_source_columns"],
                {
                    "timestamp": TIMESTAMP,
                    "photovoltaic_generation": PV,
                    "grid_import": GRID_IMPORT,
                    "grid_export": GRID_EXPORT,
                },
            )
            self.assertEqual(
                provenance["interpolation"],
                {
                    "column": INTERPOLATED,
                    "policy": "selected E7 source feeds must be non-interpolated",
                    "selected_feed_interpolated_row_count": 0,
                },
            )
            self.assertEqual(
                provenance["transformation"],
                {
                    "upstream_data_representation": "cumulative energy meter readings",
                    "method": (
                        "first difference of consecutive 15-minute cumulative readings"
                    ),
                    "interval_timestamp_convention": (
                        "later/end timestamp of the two cumulative readings"
                    ),
                    "monotonic_counter_reset_policy": prepare_trace.COUNTER_POLICY,
                },
            )
            self.assertEqual(
                provenance["upstream_sha256"], hashlib.sha256(content).hexdigest()
            )
            self.assertEqual(
                provenance["prepared_trace_sha256"],
                hashlib.sha256(trace.read_bytes()).hexdigest(),
            )
            self.assertEqual(provenance["selection"]["source_rows_required"], 1101)
            self.assertEqual(
                provenance["selection"]["prepared_interval_rows"], 1100
            )
            self.assertEqual(
                provenance["selection"]["source_timestamp_start"],
                source_rows[0][TIMESTAMP],
            )
            self.assertEqual(
                provenance["selection"]["prepared_interval_timestamp_start"],
                source_rows[1][TIMESTAMP],
            )
            self.assertEqual(source.name, prepare_trace.UPSTREAM_FILENAME)

    def test_known_cumulative_values_produce_expected_interval(self):
        def mutate(rows):
            for index, row in enumerate(rows):
                row[2:5] = [
                    str(10 + 0.5 * index),
                    str(20 + 0.3 * index),
                    str(4 + 0.1 * index),
                ]

        intervals = select(upstream_bytes(mutation=mutate))
        _, pv, demand, net = intervals[0]
        self.assertAlmostEqual(pv, 0.5)
        self.assertAlmostEqual(demand, 0.7)
        self.assertAlmostEqual(net, -0.2)

    def test_missing_source_or_interpolated_column_rejected(self):
        cases = (
            b"timestamp,interpolated,pv,grid_import\n",
            b"timestamp,pv,grid_import,grid_export\n",
        )
        for content in cases:
            with self.subTest(header=content), self.assertRaisesRegex(
                ValueError, "columns are missing"
            ):
                prepare_trace.select_window(
                    content,
                    timestamp_column="timestamp",
                    pv_column="pv",
                    grid_import_column="grid_import",
                    grid_export_column="grid_export",
                )

    def test_missing_interpolation_cell_rejected_cleanly(self):
        def mutate(rows):
            rows[500] = rows[500][:1]

        with self.assertRaisesRegex(ValueError, "malformed OPSD row"):
            select(upstream_bytes(mutation=mutate))

    def test_missing_selected_numeric_cell_breaks_window(self):
        def mutate(rows):
            rows[500] = rows[500][:4]

        with self.assertRaisesRegex(ValueError, "no contiguous valid"):
            select(upstream_bytes(mutation=mutate))

    def test_duplicate_source_header_rejected(self):
        content = upstream_bytes().replace(
            f",{PV},{GRID_IMPORT},".encode(),
            f",{PV},{PV},{GRID_IMPORT},".encode(),
            1,
        )
        with self.assertRaisesRegex(ValueError, "duplicate CSV header"):
            select(content)

    def test_malformed_nonfinite_or_negative_source_values_break_window(self):
        for column_index in (2, 3, 4):
            for value in ("not-a-number", "nan", "inf", "-1"):
                with self.subTest(column_index=column_index, value=value):
                    content = upstream_bytes(
                        mutation=lambda rows, column_index=column_index, value=value: rows[
                            500
                        ].__setitem__(column_index, value)
                    )
                    with self.assertRaisesRegex(ValueError, "no contiguous valid"):
                        select(content)

    def test_negative_derived_demand_breaks_window(self):
        def mutate(rows):
            previous = rows[499]
            rows[500][2] = str(float(previous[2]) + 0.1)
            rows[500][3] = str(float(previous[3]) + 0.1)
            rows[500][4] = str(float(previous[4]) + 0.3)

        with self.assertRaisesRegex(ValueError, "no contiguous valid"):
            select(upstream_bytes(mutation=mutate))

    def test_selected_feed_interpolation_resets_and_selects_earliest_window(self):
        def mutate(rows):
            rows[500][1] = f"{UNRELATED}; {PV};"

        rows = select(upstream_bytes(row_count=1602, mutation=mutate))
        expected_start = datetime(2020, 1, 1, tzinfo=timezone.utc) + 502 * timedelta(
            minutes=15
        )
        self.assertEqual(rows[0][0], expected_start)
        self.assertEqual(len(rows), 1100)

    def test_selected_feed_interpolation_match_is_case_insensitive(self):
        def mutate(rows):
            rows[500][1] = f"{PV.swapcase()};"

        rows = select(upstream_bytes(row_count=1602, mutation=mutate))
        expected_start = datetime(2020, 1, 1, tzinfo=timezone.utc) + 502 * timedelta(
            minutes=15
        )
        self.assertEqual(rows[0][0], expected_start)

    def test_selected_interpolation_on_previous_endpoint_invalidates_interval(self):
        content = upstream_bytes(
            mutation=lambda rows: rows[0].__setitem__(1, f"{PV};")
        )
        with self.assertRaisesRegex(ValueError, "no contiguous valid"):
            select(content)

    def test_selected_interpolation_on_current_endpoint_invalidates_interval(self):
        content = upstream_bytes(
            mutation=lambda rows: rows[-1].__setitem__(1, f"{PV};")
        )
        with self.assertRaisesRegex(ValueError, "no contiguous valid"):
            select(content)

    def test_unrelated_feed_interpolation_does_not_invalidate_row(self):
        content = upstream_bytes(
            mutation=lambda rows: rows[500].__setitem__(1, f"{UNRELATED};")
        )
        rows = select(content)
        self.assertEqual(len(rows), 1100)
        self.assertEqual(
            rows[0][0],
            datetime(2020, 1, 1, 0, 15, tzinfo=timezone.utc),
        )

    def test_wrong_timestamp_spacing_rejected(self):
        def mutate(rows):
            timestamp = datetime.fromisoformat(rows[500][0]) + timedelta(minutes=1)
            rows[500][0] = timestamp.isoformat()

        with self.assertRaisesRegex(ValueError, "no contiguous valid"):
            select(upstream_bytes(mutation=mutate))

    def test_counter_decrease_resets_candidate_window(self):
        def mutate(rows):
            rows[500][2] = "0"

        intervals = select(upstream_bytes(row_count=1601, mutation=mutate))
        expected_start = datetime(2020, 1, 1, tzinfo=timezone.utc) + 501 * timedelta(
            minutes=15
        )
        self.assertEqual(intervals[0][0], expected_start)

    def test_insufficient_trace_length_rejected(self):
        with self.assertRaisesRegex(ValueError, "no contiguous valid"):
            select(upstream_bytes(row_count=1100))

    def test_wrong_upstream_filename_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "renamed.csv"
            source.write_bytes(upstream_bytes())
            with self.assertRaisesRegex(ValueError, "authoritative OPSD source filename"):
                prepare_trace.prepare_trace(
                    source,
                    root / "trace.csv",
                    root / "provenance.json",
                    timestamp_column=TIMESTAMP,
                    pv_column=PV,
                    grid_import_column=GRID_IMPORT,
                    grid_export_column=GRID_EXPORT,
                )


if __name__ == "__main__":
    unittest.main()
