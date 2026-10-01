"""End-to-end API tests for POST /analyze/environment-matrix."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _matrix(**payload):
    response = client.post("/analyze/environment-matrix", json=payload)
    return response.status_code, response.json()


def _env(name, **marker_env):
    return {"name": name, "marker_env": marker_env}


def test_matrix_reports_versions_and_marker_driven_differences():
    status, body = _matrix(
        packages={
            "app": {
                "1.0": {
                    "dependencies": [
                        "linux-only>=1.0; sys_platform == 'linux'",
                        "win-only>=1.0; sys_platform == 'win32'",
                    ]
                }
            },
            "linux-only": {"1.0": {}, "2.0": {}},
            "win-only": {"1.0": {}, "2.0": {}},
        },
        root=["app"],
        environments=[_env("linux", sys_platform="linux"), _env("windows", sys_platform="win32")],
    )
    assert status == 200
    assert body["summary"] == {"environments": 2, "ok": 2, "conflict": 0, "limit": 0}

    environments = body["environments"]
    assert [env["name"] for env in environments] == ["linux", "windows"]
    assert environments[0]["status"] == "ok"
    assert environments[1]["status"] == "ok"

    matrix = body["matrix"]
    assert matrix["environments"] == ["linux", "windows"]
    assert matrix["successful_count"] == 2

    by_package = {row["package"]: row for row in matrix["packages"]}
    # Canonical-name ordering.
    assert [row["package"] for row in matrix["packages"]] == sorted(
        by_package, key=lambda n: n.lower().replace("_", "-")
    )

    # Absence is null and distinct from a concrete version; both
    # marker-exclusive packages differ across environments.
    assert by_package["app"]["versions"] == [
        {"environment": "linux", "version": "1.0"},
        {"environment": "windows", "version": "1.0"},
    ]
    assert by_package["app"]["differs"] is False
    assert by_package["linux-only"]["versions"] == [
        {"environment": "linux", "version": "2.0"},
        {"environment": "windows", "version": None},
    ]
    assert by_package["linux-only"]["differs"] is True
    assert by_package["win-only"]["versions"] == [
        {"environment": "linux", "version": None},
        {"environment": "windows", "version": "2.0"},
    ]
    assert set(body["matrix"]["differences"]) == {"linux-only", "win-only"}


def test_matrix_detects_concrete_version_differences():
    # The same packages are present everywhere, but the chosen versions
    # differ because of a python_version-marked dependency.
    status, body = _matrix(
        packages={
            "app": {
                "1.0": {
                    "dependencies": [
                        "lib>=2.0; python_version >= '3.11'",
                        "lib<2.0; python_version < '3.11'",
                    ]
                }
            },
            "lib": {"1.0": {}, "2.0": {}},
        },
        root=["app"],
        environments=[
            _env("py310", python_version="3.10", python_full_version="3.10.0"),
            _env("py311", python_version="3.11", python_full_version="3.11.0"),
        ],
    )
    assert status == 200
    by_package = {row["package"]: row for row in body["matrix"]["packages"]}
    assert by_package["lib"]["versions"] == [
        {"environment": "py310", "version": "1.0"},
        {"environment": "py311", "version": "2.0"},
    ]
    assert body["matrix"]["differences"] == ["lib"]


def test_identical_environments_have_no_differences():
    status, body = _matrix(
        packages={
            "app": {"1.0": {"dependencies": ["lib; sys_platform == 'linux'"]}},
            "lib": {"1.0": {}},
        },
        root=["app"],
        environments=[_env("a", sys_platform="linux"), _env("b", sys_platform="linux")],
    )
    assert status == 200
    assert body["matrix"]["differences"] == []
    assert all(row["differs"] is False for row in body["matrix"]["packages"])


def test_conflict_in_one_environment_does_not_affect_others():
    status, body = _matrix(
        packages={
            "a": {
                "1.0": {
                    "dependencies": [
                        "x>=2.0; sys_platform == 'linux'",
                    ]
                }
            },
            "b": {
                "1.0": {
                    "dependencies": [
                        "x<2.0; sys_platform == 'linux'",
                    ]
                }
            },
            "x": {"1.0": {}, "2.0": {}, "3.0": {}},
        },
        root=["a", "b"],
        environments=[
            _env("linux", sys_platform="linux"),
            _env("windows", sys_platform="win32"),
        ],
    )
    assert status == 200
    environments = {env["name"]: env for env in body["environments"]}
    assert environments["linux"]["status"] == "conflict"
    assert environments["linux"]["attempts"] > 0
    assert "conflict" in environments["linux"]
    assert environments["windows"]["status"] == "ok"
    assert environments["windows"]["resolved"] == {"a": "1.0", "b": "1.0"}
    assert body["summary"] == {"environments": 2, "ok": 1, "conflict": 1, "limit": 0}

    # Only the successful environment contributes to the matrix; with a
    # single successful environment there is nothing to compare against.
    assert body["matrix"]["environments"] == ["windows"]
    assert body["matrix"]["packages"] == [
        {
            "package": "a",
            "differs": False,
            "versions": [{"environment": "windows", "version": "1.0"}],
        },
        {
            "package": "b",
            "differs": False,
            "versions": [{"environment": "windows", "version": "1.0"}],
        },
    ]
    assert body["matrix"]["differences"] == []


def test_limit_in_one_environment_is_isolated():
    packages = {}
    for i in range(3):
        packages[f"p{i}"] = {
            f"{major}.0": {"dependencies": {f"p{i + 1}": ""}}
            for major in range(1, 4)
        }
    packages["p3"] = {f"{major}.0": {} for major in range(1, 4)}

    status, body = _matrix(
        packages=packages,
        root={"p0": ""},
        max_attempts=1,
        environments=[_env("a"), _env("b")],
    )
    assert status == 200
    assert body["summary"] == {"environments": 2, "ok": 0, "conflict": 0, "limit": 2}
    for env in body["environments"]:
        assert env["status"] == "limit"
        assert env["error"]["code"] == "search_limit_exceeded"
        assert "conflict" in env
    assert body["matrix"]["environments"] == []
    assert body["matrix"]["packages"] == []


def test_limit_does_not_poison_a_successful_environment():
    # Two identical-marker environments, but the first input environment
    # gets a tiny attempt budget via max_attempts shared by both — instead
    # construct a limit that depends on the marker: the limit environment
    # demands the wide chain, the other demands nothing.
    packages = {
        "app": {
            "1.0": {
                "dependencies": [
                    "p0; sys_platform == 'linux'",
                ]
            }
        },
    }
    for i in range(3):
        packages[f"p{i}"] = {
            f"{major}.0": {"dependencies": {f"p{i + 1}": ""}}
            for major in range(1, 4)
        }
    packages["p3"] = {f"{major}.0": {} for major in range(1, 4)}

    status, body = _matrix(
        packages=packages,
        root={"app": ""},
        max_attempts=2,
        environments=[
            _env("linux", sys_platform="linux"),
            _env("windows", sys_platform="win32"),
        ],
    )
    assert status == 200
    environments = {env["name"]: env for env in body["environments"]}
    assert environments["linux"]["status"] == "limit"
    assert environments["windows"]["status"] == "ok"
    assert body["matrix"]["environments"] == ["windows"]


def test_environments_stay_in_input_order():
    status, body = _matrix(
        packages={"x": {"1.0": {}}},
        root={"x": ""},
        environments=[_env("zeta"), _env("alpha"), _env("middle")],
    )
    assert status == 200
    assert [env["name"] for env in body["environments"]] == [
        "zeta",
        "alpha",
        "middle",
    ]
    assert body["matrix"]["environments"] == ["zeta", "alpha", "middle"]
    x_row = next(row for row in body["matrix"]["packages"] if row["package"] == "x")
    assert [v["environment"] for v in x_row["versions"]] == [
        "zeta",
        "alpha",
        "middle",
    ]


def test_duplicate_environment_names_rejected_with_400():
    status, body = _matrix(
        packages={"x": {"1.0": {}}},
        root={"x": ""},
        environments=[_env("same"), _env("same")],
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert "unique" in body["error"]["message"]


def test_empty_environment_name_rejected():
    response = client.post(
        "/analyze/environment-matrix",
        json={
            "packages": {"x": {"1.0": {}}},
            "root": {"x": ""},
            "environments": [{"name": " ", "marker_env": {}}, _env("b")],
        },
    )
    assert response.status_code == 400
    assert "empty" in response.json()["error"]["message"]


def test_environment_count_must_be_between_2_and_8():
    response = client.post(
        "/analyze/environment-matrix",
        json={
            "packages": {"x": {"1.0": {}}},
            "root": {"x": ""},
            "environments": [_env("only-one")],
        },
    )
    assert response.status_code == 422

    response = client.post(
        "/analyze/environment-matrix",
        json={
            "packages": {"x": {"1.0": {}}},
            "root": {"x": ""},
            "environments": [_env(f"e{i}") for i in range(9)],
        },
    )
    assert response.status_code == 422


def test_eight_environments_accepted():
    status, body = _matrix(
        packages={"x": {"1.0": {}}},
        root={"x": ""},
        environments=[_env(f"e{i}") for i in range(8)],
    )
    assert status == 200
    assert len(body["environments"]) == 8


def test_marker_env_is_optional_per_environment():
    # Missing marker_env defaults to the resolver's fixed default env
    # (linux), so the linux-marked dependency is present.
    status, body = _matrix(
        packages={
            "app": {"1.0": {"dependencies": ["lib; sys_platform == 'linux'"]}},
            "lib": {"1.0": {}},
        },
        root=["app"],
        environments=[{"name": "default"}, _env("win", sys_platform="win32")],
    )
    assert status == 200
    by_name = {env["name"]: env for env in body["environments"]}
    assert by_name["default"]["resolved"] == {"app": "1.0", "lib": "1.0"}
    assert by_name["win"]["resolved"] == {"app": "1.0"}


def test_request_validation_reuses_resolve_rules():
    # Bad package version -> same 400 shape as /resolve.
    status, body = _matrix(
        packages={"x": {"not-a-version": {}}},
        root={"x": ""},
        environments=[_env("a"), _env("b")],
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"

    # Extra fields are rejected just like /resolve.
    response = client.post(
        "/analyze/environment-matrix",
        json={
            "packages": {},
            "root": {},
            "environments": [_env("a"), _env("b")],
            "marker_env": {},
        },
    )
    assert response.status_code == 422

    response = client.post(
        "/analyze/environment-matrix",
        json={
            "packages": {},
            "root": {},
            "environments": [
                {"name": "a", "marker_env": {}, "unexpected": 1},
                _env("b"),
            ],
        },
    )
    assert response.status_code == 422


def test_each_environment_carries_own_attempts_and_locks():
    status, body = _matrix(
        packages={"x": {"1.0": {}, "2.0": {}}},
        root={"x": ">=1.0"},
        locks={"x": "1.0"},
        environments=[_env("a"), _env("b")],
    )
    assert status == 200
    for env in body["environments"]:
        assert env["status"] == "ok"
        assert env["attempts"] == 1
        assert env["resolved"]["x"] == "1.0"
        assert env["locks"]["x"] == "applied"
        assert env["allow_prereleases"] is False
