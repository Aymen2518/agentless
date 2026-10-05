# Verified API shapes

These were checked against `google-cloud-aiplatform` 1.165 (`agentplatform._genai.agent_engines`), against the
agents-cli 1.3.1 source (`~/.local/share/uv/tools/google-agents-cli/.../google/agents/cli/deploy/agent_runtime.py`,
`publish/cmd_publish.py`) and by an independent review.

## Agent Runtime (reasoning engines)
- **Client:** `agentplatform.Client(project, location, http_options={"api_version": "v1beta1"})`.
- **Create:** `client.agent_engines._create(config=…)`. Update: `_update(name=…, config={…, "update_mask": "a,b"})`.
  Both return an operation whose `.name` is `projects/…/reasoningEngines/<id>/operations/<op>`. The engine name is
  `name.rpartition("/operations/")[0]`.
- **Polling:** `_get_agent_operation(operation_name=…)` returns `.done` and `.error`.
- **Delete:** `delete(name=…, force=True)` returns an operation; agentless waits for it.
- **Listing:** `list(config={"filter": 'display_name="…"'})`, then `get(name=…)` for the full spec (`list` can omit
  `deployment_spec`).
- **The `spec` dict passes through raw.** The API accepts snake_case keys.
- **Config keys:** `display_name`, `description`, `labels`, `context_spec`, `encryption_spec`, plus:

  ```
  spec: {
    agent_framework, service_account, identity_type,
    deployment_spec: {
      env: [{name, value}], secret_env: [{name, secret_ref: {secret, version}}],
      min_instances, max_instances, resource_limits: {cpu, memory}, container_concurrency,
      agent_server_mode, psc_interface_config: {network_attachment, dns_peering_configs: [...]}
    },
    source_code_spec: {inline_source: {source_archive}, image_spec: {build_args}}
  }
  ```
- **Update masks:**
  - top level: `display_name`, `description`, `labels`, `context_spec`, `encryption_spec`
  - `spec.agent_framework`, `spec.service_account`
  - `spec.deployment_spec.{env, secret_env, min_instances, max_instances, resource_limits, container_concurrency, agent_server_mode, psc_interface_config}`
  - code: `spec.source_code_spec.inline_source.source_archive`, `spec.source_code_spec.image_spec`

  A selected field that is sent with no value is reset.
- **Code archive:** `_create_config(mode="update", source_packages=["./file", …], image_spec={…},
  agent_framework="google-adk")`, run with the working directory set to the project root. It tars and base64-encodes
  the files, and it works offline.
- **Agent Identity:** `agent_engines.create(config={"identity_type": "AGENT_IDENTITY", "display_name", "labels",
  "encryption_spec"?})`. `api_resource.spec.effective_identity` holds the principal, used as `principal://<value>`.
- **Memory Bank:** `context_spec.memory_bank_config` = `{generation_config: {model}, similarity_search_config:
  {embedding_model}, ttl_config: {default_ttl: "86400s"}, customization_configs: [{memory_topics:
  [{managed_memory_topic: {managed_topic_enum}}]}], disable_memory_revisions}`. Models must be full paths:
  `projects/{p}/locations/{r}/publishers/google/models/{m}`.
- **Default runtime SA:** `service-<projectNumber>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`.
- **Query:** `POST https://{region}-aiplatform.googleapis.com/v1/{engine}:query` with
  `{"class_method": "async_create_session", "input": {"user_id"}}`. Then `:streamQuery` with
  `{"class_method": "async_stream_query", "input": {user_id, session_id, message}}`. The response is NDJSON.
- **Logs:** `resource.type="aiplatform.googleapis.com/ReasoningEngine"` and `resource.labels.reasoning_engine_id`.

## IAM
- **resource-manager / secretmanager / iam_admin_v1:** `get_iam_policy(request={"resource": r, "options":
  {"requested_policy_version": 3}})` returns a raw `policy_pb2.Policy`. Mutate `policy.bindings` (`add(role=…)`,
  `members.append/remove`, `bindings.remove(empty)`), then `set_iam_policy(request={"resource": r, "policy":
  policy})`. The etag rides along. Leave conditional bindings untouched.
- **Resource names:** `projects/{id}`, `folders/{id}`, `organizations/{id}`, `projects/{p}/secrets/{s}`,
  `projects/-/serviceAccounts/{email}`.
- **Storage:** `bucket.get_iam_policy(requested_policy_version=3)`. `policy.bindings` is a mutable list of
  `{"role", "members"}`, then `bucket.set_iam_policy(policy)`. An etag conflict returns 412.
- **BigQuery:** `dataset.access_entries` with `AccessEntry(role, entity_type, entity_id)`, written with
  `update_dataset(ds, ["access_entries"])`. SAs and users use `userByEmail`, groups use `groupByEmail`, anything
  else uses `iamMember`.

## Gemini Enterprise (Discovery Engine v1alpha REST)
- **Endpoint:** `https://discoveryengine.googleapis.com` for `global`, else `https://{loc}-discoveryengine.googleapis.com`.
  Send the `X-Goog-User-Project: <project>` header.
- **Agents:** `{app}/assistants/default_assistant/agents`: GET (paged with `nextPageToken`), POST, and
  `PATCH {name}?updateMask=display_name,description,icon,adk_agent_definition,authorization_config`, and DELETE.
- **ADK payload:** `{displayName, description, icon: {uri}, adk_agent_definition: {tool_settings:
  {tool_description}, provisioned_reasoning_engine: {reasoning_engine: <engine>}}, authorization_config:
  {tool_authorizations: [<auth name>]}}`. An existing registration is matched on
  `adkAgentDefinition.provisionedReasoningEngine.reasoningEngine`.
- **Authorizations:** `projects/{n}/locations/{loc}/authorizations/{id}`. POST with `?authorizationId=`, or PATCH
  with `?updateMask=serverSideOauth2`. Body: `{serverSideOauth2: {clientId, clientSecret, authorizationUri (scopes in
  the query string, redirect_uri=https://vertexaisearch.cloud.google.com/static/oauth/oauth.html), tokenUri}}`.
