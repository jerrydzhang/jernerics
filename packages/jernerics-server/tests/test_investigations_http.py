import sqlite3

import pytest
from fastapi.testclient import TestClient
from jernerics_server.http import create_app
from jernerics_server.store import Store

AUTH = {"Authorization": "Bearer secret123"}

SW_A = "0b91e7d1-6f0e-5c1f-9a2b-00000000000a"
SW_B = "0b91e7d1-6f0e-5c1f-9a2b-00000000000b"
SW_C = "0b91e7d1-6f0e-5c1f-9a2b-00000000000c"


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    return TestClient(create_app(store))


@pytest.fixture
def auth_client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    return TestClient(create_app(store, api_key="secret123"))


@pytest.fixture
def seeded_client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    _seed_members(store.path)
    return TestClient(create_app(store))


def _seed_members(path) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executemany(
        "INSERT INTO sweeps (sweep_id, project, name, state, created_ns,"
        " updated_ns) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (SW_A, "p", "roberts_f01_alpha", "completed", 1, 10),
            (SW_B, "p", "roberts_f02_beta", "running", 2, 20),
            (SW_C, "q", "other_project_sweep", "completed", 3, 30),
        ],
    )
    con.executemany(
        "INSERT INTO trials (trial_id, sweep_id, number, state,"
        " retry_root_trial_id, retry_index, created_ns, updated_ns)"
        " VALUES (?, ?, 0, 'completed', ?, 0, 1, 1)",
        [("t-a1", SW_A, "t-a1"), ("t-b1", SW_B, "t-b1")],
    )
    con.executemany(
        "INSERT INTO trial_params (trial_id, kind, key, value_json, updated_ns)"
        " VALUES (?, 'manual', ?, ?, 1)",
        [
            ("t-a1", "dataset", '"mnist"'),
            ("t-a1", "lr", "0.1"),
            ("t-b1", "dataset", '"cifar"'),
        ],
    )
    con.executemany(
        "INSERT INTO executions (execution_id, trial_id, hostname, started_ns,"
        " created_ns, updated_ns) VALUES (?, ?, 'h', 1, 1, 1)",
        [("e-a1", "t-a1"), ("e-b1", "t-b1")],
    )
    con.executemany(
        "INSERT INTO tracked_values (execution_id, key, step, value_type,"
        " scalar_val, recorded_ns) VALUES (?, ?, 0, 'scalar', ?, 1)",
        [
            ("e-a1", "heldout_rmse", 0.5),
            ("e-a1", "accuracy", 0.9),
            ("e-b1", "heldout_rmse", 0.7),
        ],
    )
    con.executemany(
        "INSERT INTO submissions (submission_id, sweep_id, backend, state,"
        " created_ns, updated_ns, git_hash, config_source)"
        " VALUES (?, ?, 'local', 'completed', 1, 1, ?, ?)",
        [
            ("s-a", SW_A, "aaa", "exp/a.py"),
            ("s-b", SW_B, "bbb", "exp/b.py"),
        ],
    )
    con.commit()
    con.close()


def _create(client, **overrides):
    body = {
        "project": "p",
        "name": "baseline",
        "factor": "dataset",
        "outcome": "heldout_rmse",
    }
    body.update(overrides)
    return client.post("/investigations/create", json=body)


class TestAuth:
    @pytest.mark.parametrize(
        "path",
        [
            "/investigations",
            "/investigations/get",
            "/investigations/create",
            "/investigations/members/set",
            "/investigations/members/add",
            "/investigations/members/remove",
            "/investigations/archive",
            "/investigations/restore",
            "/investigations/preview",
        ],
    )
    def test_missing_auth_returns_401(self, auth_client, path):
        response = auth_client.post(path, json={})
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "path",
        [
            "/investigations",
            "/investigations/create",
            "/investigations/archive",
            "/investigations/preview",
        ],
    )
    def test_invalid_key_returns_401(self, auth_client, path):
        response = auth_client.post(
            path, json={"project": "p"}, headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 401

    def test_valid_bearer_passes(self, auth_client):
        response = auth_client.post(
            "/investigations", json={"project": "p"}, headers=AUTH
        )
        assert response.status_code == 200

    def test_no_auth_needed_when_key_unset(self, client):
        response = client.post("/investigations", json={"project": "p"})
        assert response.status_code == 200


class TestCreateAndIdempotency:
    def test_create_returns_record(self, seeded_client):
        response = _create(seeded_client, members=[SW_A])
        assert response.status_code == 200
        record = response.json()
        assert record["project"] == "p"
        assert record["name"] == "baseline"
        assert record["factor"] == "dataset"
        assert record["outcome"] == "heldout_rmse"
        assert record["replicate_factor"] is None
        assert record["archived_ns"] is None
        assert record["members"] == [SW_A]

    def test_create_replay_returns_existing_record(self, client):
        first = _create(client)
        second = _create(client)
        assert second.status_code == 200
        assert second.json() == first.json()
        listed = client.post("/investigations", json={"project": "p"}).json()
        assert len(listed["records"]) == 1

    def test_conflicting_body_returns_409(self, client):
        _create(client)
        response = _create(client, outcome="accuracy")
        assert response.status_code == 409
        body = response.json()
        assert body["error"]["code"] == "investigation_conflict"

    def test_conflicting_replicate_factor_returns_409(self, client):
        _create(client)
        response = _create(client, replicate_factor="seed")
        assert response.status_code == 409

    def test_create_with_unknown_sweep_returns_404(self, client):
        response = _create(client, members=["ghost"])
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "sweep_not_found"

    def test_create_with_cross_project_sweep_returns_422(self, seeded_client):
        response = _create(seeded_client, members=[SW_A, SW_C])
        assert response.status_code == 422
        assert (
            seeded_client.post("/investigations", json={"project": "p"}).json()[
                "records"
            ]
            == []
        )

    def test_same_name_in_different_projects_is_distinct(self, seeded_client):
        first = _create(seeded_client).json()
        second = _create(seeded_client, project="q").json()
        assert first["id"] != second["id"]


class TestListAndGet:
    def test_list_empty_project(self, client):
        response = client.post("/investigations", json={"project": "p"})
        assert response.status_code == 200
        assert response.json() == {"records": [], "next_token": None}

    def test_list_scoped_to_project(self, seeded_client):
        _create(seeded_client)
        _create(seeded_client, project="q", name="elsewhere")
        records = seeded_client.post("/investigations", json={"project": "p"}).json()[
            "records"
        ]
        assert [record["name"] for record in records] == ["baseline"]

    def test_archived_excluded_from_list_until_requested(self, seeded_client):
        record = _create(seeded_client).json()
        seeded_client.post(
            "/investigations/archive", json={"investigation_id": record["id"]}
        )
        assert (
            seeded_client.post("/investigations", json={"project": "p"}).json()[
                "records"
            ]
            == []
        )
        listed = seeded_client.post(
            "/investigations", json={"project": "p", "include_archived": True}
        ).json()["records"]
        assert [item["name"] for item in listed] == ["baseline"]
        assert listed[0]["archived_ns"] is not None

    def test_get_by_id_returns_coverage_facts(self, seeded_client):
        record = _create(seeded_client, members=[SW_A, SW_B]).json()
        response = seeded_client.post(
            "/investigations/get", json={"investigation_id": record["id"]}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["investigation"]["id"] == record["id"]
        coverage = body["coverage"]
        assert coverage["members"] == 2
        assert coverage["with_outcome"] == 2
        assert coverage["completed"] == 1
        assert coverage["invalid"] == 0
        assert coverage["last_activity_ns"] == 20

    def test_with_outcome_counts_only_outcome_carriers(self, seeded_client):
        record = _create(seeded_client, outcome="accuracy", members=[SW_A, SW_B]).json()
        body = seeded_client.post(
            "/investigations/get", json={"investigation_id": record["id"]}
        ).json()
        assert body["coverage"]["with_outcome"] == 1

    def test_get_by_project_and_name(self, seeded_client):
        record = _create(seeded_client).json()
        response = seeded_client.post(
            "/investigations/get", json={"project": "p", "name": "baseline"}
        )
        assert response.status_code == 200
        assert response.json()["investigation"]["id"] == record["id"]

    def test_get_without_locator_returns_400(self, client):
        response = client.post("/investigations/get", json={"name": "baseline"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

    def test_get_unknown_id_returns_404(self, client):
        response = client.post(
            "/investigations/get", json={"investigation_id": "ghost"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "investigation_not_found"

    def test_get_unknown_name_returns_404(self, client):
        response = client.post(
            "/investigations/get", json={"project": "p", "name": "ghost"}
        )
        assert response.status_code == 404


class TestMembers:
    def _create(self, client, members=(SW_A,)):
        return _create(client, members=list(members)).json()

    def test_set_replaces_membership(self, seeded_client):
        record = self._create(seeded_client)
        response = seeded_client.post(
            "/investigations/members/set",
            json={"investigation_id": record["id"], "sweep_ids": [SW_B]},
        )
        assert response.status_code == 200
        assert response.json()["members"] == [SW_B]

    def test_set_replay_is_a_no_op(self, seeded_client):
        record = self._create(seeded_client, members=(SW_A, SW_B))
        first = seeded_client.post(
            "/investigations/members/set",
            json={"investigation_id": record["id"], "sweep_ids": [SW_A, SW_B]},
        ).json()
        second = seeded_client.post(
            "/investigations/members/set",
            json={"investigation_id": record["id"], "sweep_ids": [SW_B, SW_A]},
        ).json()
        assert second == first == record

    def test_add_is_idempotent(self, seeded_client):
        record = self._create(seeded_client)
        body = {"investigation_id": record["id"], "sweep_ids": [SW_A, SW_B]}
        first = seeded_client.post("/investigations/members/add", json=body).json()
        second = seeded_client.post("/investigations/members/add", json=body).json()
        assert first["members"] == [SW_A, SW_B]
        assert second == first

    def test_remove_non_member_is_a_no_op(self, seeded_client):
        record = self._create(seeded_client)
        body = {"investigation_id": record["id"], "sweep_ids": [SW_B]}
        response = seeded_client.post("/investigations/members/remove", json=body)
        assert response.status_code == 200
        assert response.json() == record

    def test_remove_drops_member(self, seeded_client):
        record = self._create(seeded_client, members=(SW_A, SW_B))
        response = seeded_client.post(
            "/investigations/members/remove",
            json={"investigation_id": record["id"], "sweep_ids": [SW_A]},
        )
        assert response.json()["members"] == [SW_B]

    def test_cross_project_member_returns_422(self, seeded_client):
        record = self._create(seeded_client)
        response = seeded_client.post(
            "/investigations/members/add",
            json={"investigation_id": record["id"], "sweep_ids": [SW_C]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "cross_project_sweep"

    def test_unknown_sweep_returns_404(self, seeded_client):
        record = self._create(seeded_client)
        response = seeded_client.post(
            "/investigations/members/add",
            json={"investigation_id": record["id"], "sweep_ids": ["ghost"]},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "sweep_not_found"

    def test_unknown_investigation_returns_404(self, client):
        response = client.post(
            "/investigations/members/set",
            json={"investigation_id": "ghost", "sweep_ids": [SW_A]},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "investigation_not_found"


class TestArchiveRestore:
    def test_archive_and_restore_round_trip(self, seeded_client):
        record = _create(seeded_client).json()
        archived = seeded_client.post(
            "/investigations/archive", json={"investigation_id": record["id"]}
        ).json()
        assert archived["archived_ns"] is not None
        restored = seeded_client.post(
            "/investigations/restore", json={"investigation_id": record["id"]}
        ).json()
        assert restored["archived_ns"] is None

    def test_archive_replay_keeps_first_timestamp(self, seeded_client):
        record = _create(seeded_client).json()
        first = seeded_client.post(
            "/investigations/archive", json={"investigation_id": record["id"]}
        ).json()
        second = seeded_client.post(
            "/investigations/archive", json={"investigation_id": record["id"]}
        ).json()
        assert second["archived_ns"] == first["archived_ns"]
        assert second == first

    def test_restore_active_is_a_no_op(self, seeded_client):
        record = _create(seeded_client).json()
        response = seeded_client.post(
            "/investigations/restore", json={"investigation_id": record["id"]}
        )
        assert response.status_code == 200
        assert response.json() == record

    def test_archive_unknown_returns_404(self, client):
        response = client.post(
            "/investigations/archive", json={"investigation_id": "ghost"}
        )
        assert response.status_code == 404


class TestPreview:
    def _preview(self, client, sweep_ids):
        return client.post(
            "/investigations/preview", json={"project": "p", "sweep_ids": sweep_ids}
        )

    def test_deterministic_byte_identical_responses(self, seeded_client):
        sweep_ids = [SW_A, SW_B, "ghost", SW_C]
        first = self._preview(seeded_client, sweep_ids)
        second = self._preview(seeded_client, sweep_ids)
        assert first.status_code == 200
        assert first.content == second.content

    def test_factor_candidates_kinds_and_ordering(self, seeded_client):
        body = self._preview(seeded_client, [SW_A, SW_B]).json()
        factors = [
            (factor["kind"], factor["name"], factor["members"])
            for factor in body["factors"]
        ]
        assert factors == [
            ("manual_param", "dataset", 2),
            ("config_source", "config_source", 2),
            ("name_token", "alpha", 1),
            ("name_token", "beta", 1),
            ("name_token", "f01", 1),
            ("name_token", "f02", 1),
            ("name_token", "roberts", 2),
        ]
        assert body["member_count"] == 2

    def test_numeric_manual_params_are_not_factors(self, seeded_client):
        body = self._preview(seeded_client, [SW_A]).json()
        names = [factor["name"] for factor in body["factors"]]
        assert "lr" not in names
        assert "dataset" in names

    def test_outcome_candidates_ordered_by_coverage_then_name(self, seeded_client):
        body = self._preview(seeded_client, [SW_A, SW_B]).json()
        outcomes = [(item["key"], item["members"]) for item in body["outcomes"]]
        assert outcomes == [("heldout_rmse", 2), ("accuracy", 1)]

    def test_warnings_report_divergence_and_bad_refs(self, seeded_client):
        body = self._preview(seeded_client, [SW_A, SW_B, "ghost", SW_C]).json()
        warnings = [(w["kind"], w["detail"]) for w in body["warnings"]]
        assert warnings == [
            ("unknown_sweep", "no sweep with id ghost"),
            ("cross_project_sweep", f"sweep {SW_C} belongs to project 'q', not 'p'"),
            ("git_hash_divergence", "differing git_hash across members: aaa, bbb"),
            (
                "config_source_divergence",
                "differing config_source across members: exp/a.py, exp/b.py",
            ),
        ]

    def test_no_warnings_when_members_agree(self, seeded_client):
        response = seeded_client.post(
            "/investigations/preview", json={"project": "p", "sweep_ids": [SW_A, SW_A]}
        )
        body = response.json()
        assert body["warnings"] == []
        assert body["member_count"] == 1

    def test_empty_member_list_has_no_candidates(self, client):
        response = client.post(
            "/investigations/preview", json={"project": "p", "sweep_ids": []}
        )
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "project": "p",
            "member_count": 0,
            "factors": [],
            "outcomes": [],
            "warnings": [],
        }
