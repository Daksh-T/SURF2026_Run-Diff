"""Focused regression tests for state-mode DDL/DML grading and hint evidence."""
from __future__ import annotations

import copy
import sqlite3
import unittest
from unittest import mock

import state_core as sc


EMPTY_GENERATOR = "def populate(conn, seed):\n    pass\n"


class DdlConstraintGradingTests(unittest.TestCase):
    def grade(self, gold: str, student: str):
        baked = sc.bake_gold_state("", gold, EMPTY_GENERATOR, seeds=[1])
        return baked, sc.grade_baked_state(
            "ddl_constraints", "", EMPTY_GENERATOR, baked, student)

    def test_missing_unique_is_wrong_and_hint_names_the_constraint(self):
        gold = "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE)"
        student = "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL)"

        baked, grade = self.grade(gold, student)

        self.assertFalse(grade.correct)
        self.assertEqual(2, baked["seeds"][0]["state"]["snapshot_version"])
        self.assertEqual("wrong constraints", sc.error_category_state(grade))
        self.assertIn("missing required UNIQUE constraint on (email)",
                      sc.render_state_diff_rung(grade))
        payload = sc.diff_payload_state(grade)
        self.assertEqual([["email"]],
                         payload["constraint_diffs"]["users"]["missing_unique"])

    def test_hint_model_receives_constraint_evidence(self):
        gold = "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE)"
        student = "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"
        _, grade = self.grade(gold, student)
        problem = sc.tc.RedactedProblem(
            id="ddl_constraints", title="Create users", difficulty="easy",
            schema="", prompt="Create users with a unique email.", target_clauses=[],
        )

        with mock.patch("model.call", return_value={"text": "Which requirement is missing?"}) as call:
            hint = sc.generate_hint_state(problem, grade, 2, student, model="qwen7b")

        self.assertEqual("Which requirement is missing?", hint)
        prompt = call.call_args.args[1]
        self.assertIn("missing required UNIQUE constraint on (email)", prompt)

    def test_composite_unique_column_order_is_semantically_irrelevant(self):
        gold = "CREATE TABLE pairs (a INTEGER, b INTEGER, UNIQUE(a, b))"
        student = "CREATE TABLE pairs (b INTEGER, a INTEGER, UNIQUE(b, a))"
        _, grade = self.grade(gold, student)
        self.assertTrue(grade.correct)

    def test_missing_foreign_key_is_wrong(self):
        gold = ("CREATE TABLE parent (id INTEGER PRIMARY KEY);"
                "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER, "
                "FOREIGN KEY(parent_id) REFERENCES parent(id) ON DELETE CASCADE)")
        student = ("CREATE TABLE parent (id INTEGER PRIMARY KEY);"
                   "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER)")
        _, grade = self.grade(gold, student)
        self.assertFalse(grade.correct)
        diff = grade.first_fail.diff.constraint_diffs["child"]
        self.assertEqual(1, len(diff["missing_foreign_keys"]))

    def test_missing_check_is_wrong(self):
        gold = "CREATE TABLE adults (id INTEGER, age INTEGER CHECK (age >= 18))"
        student = "CREATE TABLE adults (id INTEGER, age INTEGER)"
        _, grade = self.grade(gold, student)
        self.assertFalse(grade.correct)
        self.assertEqual(["age >= 18"],
                         grade.first_fail.diff.constraint_diffs["adults"]["missing_checks"])

    def test_check_string_literals_remain_case_sensitive(self):
        gold = "CREATE TABLE codes (code TEXT CHECK (code = 'A'))"
        student = "CREATE TABLE codes (code TEXT CHECK (code = 'a'))"
        _, grade = self.grade(gold, student)
        self.assertFalse(grade.correct)

    def test_check_word_inside_default_string_is_not_a_constraint(self):
        gold = "CREATE TABLE notes (body TEXT DEFAULT 'CHECK(nope)')"
        student = "CREATE TABLE notes (body TEXT DEFAULT 'CHECK(nope)')"
        baked, grade = self.grade(gold, student)
        self.assertTrue(grade.correct)
        self.assertEqual([], baked["seeds"][0]["state"]["tables"]["notes"]
                         ["constraints"]["checks"])

    def test_equivalent_generated_expression_formatting_is_correct(self):
        gold = ("CREATE TABLE totals (price REAL, quantity INTEGER, "
                "total REAL GENERATED ALWAYS AS (price * quantity) VIRTUAL)")
        student = ("CREATE TABLE totals (quantity INT, price DOUBLE, "
                   "total REAL AS ( price*quantity ) VIRTUAL)")
        baked, grade = self.grade(gold, student)
        self.assertTrue(grade.correct)
        generated = baked["seeds"][0]["state"]["tables"]["totals"]
        self.assertEqual(
            [{"name": "total", "expression": "price * quantity", "storage": "virtual"}],
            generated["constraints"]["generated"],
        )

    def test_wrong_generated_expression_is_wrong_and_visible_to_hint_model(self):
        gold = ("CREATE TABLE totals (price REAL, quantity INTEGER, "
                "total REAL AS (price * quantity) STORED)")
        student = ("CREATE TABLE totals (price REAL, quantity INTEGER, "
                   "total REAL AS (price + quantity) STORED)")
        _, grade = self.grade(gold, student)
        self.assertFalse(grade.correct)
        text = sc.render_state_diff_rung(grade)
        self.assertIn("generated column(s) total", text)
        self.assertIn("wrong expression/storage mode", text)

        problem = sc.tc.RedactedProblem(
            id="generated", title="Generated total", difficulty="medium", schema="",
            prompt="Create totals with total generated from price and quantity.",
            target_clauses=[],
        )
        with mock.patch("model.call", return_value={"text": "How is total derived?"}) as call:
            sc.generate_hint_state(problem, grade, 2, student, model="qwen7b")
        self.assertIn("generated column(s) total", call.call_args.args[1])

    def test_generated_storage_mode_matters(self):
        gold = "CREATE TABLE t (a INTEGER, doubled INTEGER AS (a * 2) STORED)"
        student = "CREATE TABLE t (a INTEGER, doubled INTEGER AS (a * 2) VIRTUAL)"
        _, grade = self.grade(gold, student)
        self.assertFalse(grade.correct)

    def test_ordinary_column_does_not_replace_generated_column(self):
        gold = "CREATE TABLE t (a INTEGER, doubled INTEGER AS (a * 2))"
        student = "CREATE TABLE t (a INTEGER, doubled INTEGER)"
        _, grade = self.grade(gold, student)
        self.assertFalse(grade.correct)

    def test_legacy_baked_bundle_keeps_pre_v2_behavior(self):
        gold = "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE)"
        student = "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"
        baked = sc.bake_gold_state("", gold, EMPTY_GENERATOR, seeds=[1])
        legacy = copy.deepcopy(baked)
        state = legacy["seeds"][0]["state"]
        state.pop("snapshot_version")
        for table in state["tables"].values():
            table.pop("constraints")

        grade = sc.grade_baked_state("legacy", "", EMPTY_GENERATOR, legacy, student)
        self.assertTrue(grade.correct)


class UpdateWorkflowTests(unittest.TestCase):
    SCHEMA = "CREATE TABLE employees (id INTEGER PRIMARY KEY, dept TEXT, salary REAL)"
    GENERATOR = """\
def populate(conn, seed):
    conn.executemany(
        "INSERT INTO employees(id, dept, salary) VALUES (?, ?, ?)",
        [(1, "sales", 100.0), (2, "engineering", 200.0)],
    )
"""

    def test_update_is_state_graded_and_bad_where_gets_row_hint(self):
        gold = "UPDATE employees SET salary = salary * 1.1 WHERE dept = 'sales'"
        baked = sc.bake_gold_state(self.SCHEMA, gold, self.GENERATOR, seeds=[1, 2])
        correct = sc.grade_baked_state(
            "update", self.SCHEMA, self.GENERATOR, baked, gold)
        wrong = sc.grade_baked_state(
            "update", self.SCHEMA, self.GENERATOR, baked,
            "UPDATE employees SET salary = salary * 1.1")

        self.assertTrue(correct.correct)
        self.assertFalse(wrong.correct)
        self.assertEqual("rows", sc.family_for_state(wrong))
        self.assertIn("should not be there", sc.render_state_diff_rung(wrong))


class SchemaObjectGradingTests(unittest.TestCase):
    SCHEMA = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, active INTEGER)"

    def grade(self, gold: str, student: str):
        baked = sc.bake_gold_state(self.SCHEMA, gold, EMPTY_GENERATOR, seeds=[1])
        return sc.grade_baked_state(
            "schema_object", self.SCHEMA, EMPTY_GENERATOR, baked, student)

    def test_missing_view_is_wrong(self):
        grade = self.grade(
            "CREATE VIEW active_users AS SELECT id, name FROM users WHERE active = 1", "")
        self.assertFalse(grade.correct)
        self.assertIn("missing required VIEW `active_users`", sc.render_state_diff_rung(grade))

    def test_wrong_view_definition_is_wrong(self):
        grade = self.grade(
            "CREATE VIEW active_users AS SELECT id FROM users WHERE active = 1",
            "CREATE VIEW active_users AS SELECT id FROM users WHERE active = 0",
        )
        self.assertFalse(grade.correct)
        self.assertEqual("wrong schema object", sc.error_category_state(grade))

    def test_missing_explicit_index_is_wrong(self):
        grade = self.grade("CREATE INDEX users_name_idx ON users(name)", "")
        self.assertFalse(grade.correct)
        self.assertIn("missing required INDEX `users_name_idx`", sc.render_state_diff_rung(grade))

    def test_missing_trigger_is_wrong(self):
        gold = ("CREATE TRIGGER deactivate AFTER UPDATE OF active ON users BEGIN "
                "UPDATE users SET name = name WHERE id = NEW.id; END")
        grade = self.grade(gold, "")
        self.assertFalse(grade.correct)
        self.assertIn("missing required TRIGGER `deactivate`", sc.render_state_diff_rung(grade))


class SelectPermutationRegressionTests(unittest.TestCase):
    def test_permutation_quotes_numeric_and_keyword_identifiers(self):
        conn = sqlite3.connect(":memory:")
        conn.execute('CREATE TABLE "stats" ("2B" INTEGER, "select" TEXT)')
        conn.executemany('INSERT INTO "stats" ("2B", "select") VALUES (?, ?)',
                         [(2, "first"), (3, "second")])
        sc.grader.permute_db(conn)
        rows = conn.execute('SELECT "2B", "select" FROM "stats"').fetchall()
        conn.close()
        self.assertEqual([(3, "second"), (2, "first")], rows)


if __name__ == "__main__":
    unittest.main()
