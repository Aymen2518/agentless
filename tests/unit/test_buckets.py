import pytest

from agentless.config.loader import ConfigError, load_project
from agentless.plan.model import Action, DeployOptions

SA = "serviceAccount:sample-dev@proj-dev.iam.gserviceaccount.com"
NAME = "acme-reports-dev"
YES = lambda _cs: True


def change(changeset, resource):
    return next(c for c in changeset.changes if c.resource == resource)


def plan(make_provider):
    return make_provider().plan(DeployOptions())[1]


def deploy(make_provider, options=None):
    return make_provider().deploy(options or DeployOptions(), YES)


def engine_env(gcp):
    configs = [c[2] for c in gcp.calls if c[0] in ("engine_create", "engine_update")]
    env = {}
    for config in configs:
        spec = config.get("spec", {}).get("deployment_spec") or {}
        env = {e["name"]: e["value"] for e in spec.get("env", [])} or env
    return env


@pytest.fixture
def reports(edit, gcp):
    """Declare one reports bucket that does not exist yet."""
    gcp.absent_buckets.add(NAME)

    def declare(**overrides):
        bucket = {"name": "acme-reports-${stage}", "env": "REPORTS_BUCKET", **overrides}
        edit(lambda d: d.update(resources={"buckets": {"reports": bucket}}))

    declare()
    return declare


# --- create -------------------------------------------------------------------------------------------------


def test_creates_bucket_grants_access_and_injects_env(make_provider, gcp, reports):
    cs = plan(make_provider)
    buckets, iam = change(cs, "buckets"), change(cs, "iam")
    assert buckets.action == Action.CREATE and not cs.blocked
    assert buckets.details == [f"+ {NAME} (europe-west1, STANDARD) → $REPORTS_BUCKET"]
    assert any(
        f"roles/storage.objectUser on bucket/{NAME}" in d and "(automatic: resources.buckets)" in d for d in iam.details
    )

    deploy(make_provider)
    created = next(c for c in gcp.calls if c[0] == "bucket_create")
    assert created[1] == NAME
    assert created[2]["location"] == "EUROPE-WEST1" and created[2]["labels"]["agentless-stage"] == "dev"
    kinds = gcp.kinds()
    assert kinds.index("bucket_create") < next(i for i, c in enumerate(gcp.calls) if c[:3] == ("iam", "bucket", NAME))
    assert SA in gcp.policies[("bucket", NAME)]["roles/storage.objectUser"]
    assert engine_env(gcp)["REPORTS_BUCKET"] == NAME
    assert make_provider().store.read().resources["buckets"]["items"][NAME]["created"] is True
    assert not plan(make_provider).pending


def test_settings_are_applied_and_updated_in_place(make_provider, gcp, reports):
    reports(storageClass="NEARLINE", versioning=True, lifecycle={"deleteAfterDays": 90}, access="objectAdmin")
    deploy(make_provider)
    live = gcp.buckets[NAME]
    assert (live["storage_class"], live["versioning"], live["delete_after_days"]) == ("NEARLINE", True, 90)
    assert SA in gcp.policies[("bucket", NAME)]["roles/storage.objectAdmin"]

    reports(storageClass="NEARLINE", versioning=True, lifecycle={"deleteAfterDays": 30}, access="objectViewer")
    cs = plan(make_provider)
    assert change(cs, "buckets").action == Action.UPDATE
    assert change(cs, "buckets").details == [f"~ {NAME} delete_after_days: 90 → 30"]
    deploy(make_provider)
    assert gcp.buckets[NAME]["delete_after_days"] == 30
    assert gcp.kinds().count("bucket_create") == 1
    roles = gcp.policies[("bucket", NAME)]
    assert SA in roles["roles/storage.objectViewer"] and SA not in roles["roles/storage.objectAdmin"]


def test_foreign_labels_are_kept(make_provider, gcp, reports):
    deploy(make_provider)
    gcp.buckets[NAME]["labels"]["cost-center"] = "42"
    assert not plan(make_provider).pending


def test_location_change_is_blocked_never_replaced(make_provider, gcp, reports):
    deploy(make_provider)
    reports(location="EU")
    cs = plan(make_provider)
    assert "location can't change" in change(cs, "buckets").blocked
    with pytest.raises(Exception, match="location"):
        deploy(make_provider, DeployOptions(allow_replace=True))
    assert gcp.kinds().count("bucket_delete") == 0


def test_bucket_deleted_outside_is_recreated(make_provider, gcp, reports):
    deploy(make_provider)
    gcp.buckets.pop(NAME)
    cs = plan(make_provider)
    assert "(missing, recreated empty)" in change(cs, "buckets").details[0]
    deploy(make_provider)
    assert gcp.kinds().count("bucket_create") == 2


# --- existing buckets ---------------------------------------------------------------------------------------


def _existing(gcp, project_number="123456", **extra):
    gcp.absent_buckets.discard(NAME)
    gcp.buckets[NAME] = {
        "location": "EUROPE-WEST1",
        "storage_class": "STANDARD",
        "versioning": False,
        "delete_after_days": None,
        "labels": {},
        "kms_key": None,
        "project_number": project_number,
        **extra,
    }


def test_existing_bucket_in_project_is_adopted_and_kept_on_remove(make_provider, gcp, reports):
    reports(deletionPolicy="delete")
    _existing(gcp)
    cs = plan(make_provider)
    assert change(cs, "buckets").action == Action.UPDATE
    assert change(cs, "buckets").details[0] == f"⇐ adopt {NAME} (exists in this project, kept on remove)"
    deploy(make_provider)
    assert "bucket_create" not in gcp.kinds() and gcp.buckets[NAME]["labels"]["agentless-service"] == "sample-agent"
    assert make_provider().store.read().resources["buckets"]["items"][NAME]["created"] is False
    make_provider().remove(YES)
    assert NAME in gcp.buckets, "an adopted bucket is never deleted"


def test_bucket_of_another_project_is_blocked(make_provider, gcp, reports):
    _existing(gcp, project_number="999")
    assert "belongs to another project" in change(plan(make_provider), "buckets").blocked


def test_unreadable_bucket_name_is_blocked(make_provider, gcp, reports):
    gcp.bucket_get = lambda name: {"name": name, "forbidden": True}
    assert "cannot read it" in change(plan(make_provider), "buckets").blocked


# --- removal never deletes data -------------------------------------------------------------------------------


def test_remove_retains_bucket_by_default(make_provider, gcp, reports):
    deploy(make_provider)
    shown = []
    assert make_provider().remove(lambda cs: shown.append(change(cs, "buckets")) or True)
    assert shown[0].details == [f"= {NAME}: kept with its data"]
    assert NAME in gcp.buckets and "bucket_delete" not in gcp.kinds()
    assert SA not in gcp.policies[("bucket", NAME)]["roles/storage.objectUser"], "agentless's grant is revoked"


def test_remove_deletes_only_an_empty_bucket(make_provider, gcp, reports):
    reports(deletionPolicy="delete")
    deploy(make_provider)
    gcp.bucket_objects[NAME] = 3
    assert make_provider().remove(YES)
    assert NAME in gcp.buckets, "a bucket with objects is kept"

    deploy(make_provider)  # adopts the kept bucket: no longer created by agentless
    assert make_provider().store.read().resources["buckets"]["items"][NAME]["created"] is False


def test_remove_deletes_empty_bucket_with_delete_policy(make_provider, gcp, reports):
    reports(deletionPolicy="delete")
    deploy(make_provider)
    assert make_provider().remove(YES)
    assert NAME not in gcp.buckets and "bucket_delete" in gcp.kinds()


def test_dropping_a_bucket_from_yaml_releases_it(make_provider, gcp, edit, reports):
    deploy(make_provider)
    edit(lambda d: d.pop("resources"))
    cs = plan(make_provider)
    assert change(cs, "buckets").action == Action.DELETE
    assert change(cs, "buckets").details == [f"- {NAME}: stop managing; bucket and data kept"]
    deploy(make_provider)
    assert NAME in gcp.buckets
    assert "buckets" not in make_provider().store.read().resources
    assert SA not in gcp.policies[("bucket", NAME)]["roles/storage.objectUser"]
    assert "REPORTS_BUCKET" not in engine_env(gcp)
    assert not plan(make_provider).pending


def test_renaming_creates_new_bucket_and_releases_old(make_provider, gcp, reports):
    reports(deletionPolicy="delete")
    deploy(make_provider)
    gcp.bucket_objects[NAME] = 1
    reports(name="acme-reports-v2-${stage}", deletionPolicy="delete")
    gcp.absent_buckets.add("acme-reports-v2-dev")
    deploy(make_provider)
    assert "acme-reports-v2-dev" in gcp.buckets and NAME in gcp.buckets, "old bucket had data, so it is kept"
    assert engine_env(gcp)["REPORTS_BUCKET"] == "acme-reports-v2-dev"


# --- identities, partial failures, code-only ------------------------------------------------------------------


def test_agent_identity_principal_gets_bucket_access(make_provider, gcp, edit, reports):
    edit(lambda d: d.update(identity={"type": "agentIdentity"}))
    deploy(make_provider)
    [engine] = gcp.engines.values()
    assert f"principal://{engine['effective_identity']}" in gcp.policies[("bucket", NAME)]["roles/storage.objectUser"]


def test_access_none_grants_nothing(make_provider, gcp, reports):
    reports(access="none")
    deploy(make_provider)
    assert not any(gcp.policies[("bucket", NAME)].values())


def test_bucket_is_tracked_even_if_a_later_step_fails(make_provider, gcp, reports):
    def fail(*_args, **_kwargs):
        raise RuntimeError("engine boom")

    gcp.engine_create = fail
    with pytest.raises(RuntimeError, match="boom"):
        deploy(make_provider)
    assert make_provider().store.read().resources["buckets"]["items"][NAME]["created"] is True
    assert change(plan(make_provider), "buckets").action == Action.NOOP


def test_code_only_defers_bucket_creation(make_provider, gcp, reports):
    deploy(make_provider, DeployOptions(code_only=True))
    assert "bucket_create" not in gcp.kinds()


# --- agent.yaml validation ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bucket", "message"),
    [
        ({"name": "Bad_Name"}, "invalid bucket name"),
        ({"name": "goog-reports"}, "invalid bucket name"),
        ({"name": "ok-name", "access": "owner"}, "access"),
        ({"name": "ok-name", "env": "lower"}, "env"),
        ({"name": "ok-name", "lifecycle": {"deleteAfterDays": 0}}, "deleteAfterDays"),
    ],
)
def test_invalid_bucket_config(agent_dir, edit, bucket, message):
    edit(lambda d: d.update(resources={"buckets": {"reports": bucket}}))
    with pytest.raises(ConfigError, match=message):
        load_project(agent_dir / "agent.yaml")


def test_duplicate_names_and_env_collisions_are_rejected(agent_dir, edit):
    edit(lambda d: d.update(resources={"buckets": {"a": {"name": "same-bk"}, "b": {"name": "same-bk"}}}))
    with pytest.raises(ConfigError, match="duplicate bucket name"):
        load_project(agent_dir / "agent.yaml")
    edit(lambda d: d.update(resources={"buckets": {"a": {"name": "bk-one", "env": "BUCKET"}}}))
    with pytest.raises(ConfigError, match="agent.environment also sets"):
        load_project(agent_dir / "agent.yaml")


def test_bucket_name_can_be_referenced_with_self(agent_dir, edit, reports):
    edit(lambda d: d["agent"]["environment"].update(OTHER="${self:resources.buckets.reports.name}"))
    config = load_project(agent_dir / "agent.yaml").config
    assert config.agent.environment["OTHER"] == NAME
