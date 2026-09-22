# SET-1250 — Implementation Plan

**Ticket:** [SET-1250 — Update IGV Proxy to validate user request and proxy data](https://cpg-populationanalysis.atlassian.net/browse/SET-1250)
**Epic:** SET-1248 — IGV Desktop Proxy External User Access
**Branch:** `SET-1250` · **HEAD:** `b864840` (teaching materials only, on top of `28c6146` = tip of `main`)
**Code citations:** every `file:line` below is valid against both — `b864840` changed no source file
**Written:** 2026-09-22, from a design session over lessons 0001–0005
**Status:** every axis decided. Ready to implement.

This file is the implementation contract. Lessons 0002–0005 explain *why*;
`reference/set-1250-change-map.html` was the previous *what* and is now partly superseded —
see [Supersedes](#supersedes). Where this document and any other workspace file
disagree, **this document wins**.

---

## 1. What the proxy is for

Worth stating, because it sets the rollout risk and was not written down before.

Most CPG people will never use this proxy. IGV Desktop has native Google/GCS support, so
anyone holding personal IAM on a `-main` bucket can point IGV straight at GCS.

**The proxy exists for users who deliberately have no personal IAM** — external
collaborators who should be able to *view* data in IGV without being granted general
read on a main bucket. The proxy is the mechanism that makes "IGV viewing access" a
narrower grant than "bucket read access".

Consequence for this ticket: cutover risk is **low**. An internal user who happens to be
going through the proxy today and is not in the access map will get a 403, but they have
a working alternative that requires no PR from anyone — use IGV's native Google access.
Nobody is stranded.

> **Correction to `learning-records/0002-set-1250-design-decisions.md`.** That record
> calls this "a breaking change" and says "every current user of the proxy must appear in
> `igv-desktop-access` before the proxy is upgraded, including internal users who work
> fine today on personal IAM." That is overstated, for the reason above. Pre-populating
> the access map is **not** a gate on the deploy.

---

## 2. The contract (fixed by SET-1249)

Set by [cpg-infrastructure PR #399](https://github.com/populationgenomics/cpg-infrastructure/pull/399).
The proxy has no design latitude here — only over how it consumes it.

| | |
|---|---|
| Secret name | `igv-proxy-config`, one per stack, in that stack's own GCP project |
| Resource path | `projects/<project>/secrets/igv-proxy-config/versions/latest` |
| Payload | `{"users": {"<email>": ["<bucket>", …]}}`, keys and lists both sorted |
| Key format | The user's GCP account id — `member.clouds['gcp'].id`, an **email**. Never a `sub` |
| Value format | Full bucket names including the storage prefix, e.g. `cpg-fewgenomes-main`. Directly comparable to the parsed path segment |
| Bucket scope | `-main` only; **both stacks receive an identical payload** |
| Opt-in | An `igv-desktop-access` key in a dataset's `members.yaml` |
| IAM | Granted by cpg-infra, not by this repo: `secretAccessor` on the secret, plus direct read on each participating main bucket |

**Dev sees production buckets, and that is accepted as intended.** PR #399's *description*
claims per-stack `-test` scoping via `include_test_buckets`; the *diff* contains no such
logic and a test asserts `-test` never appears. Ruling taken in this session: leave it. A
dev user gets exactly the access they would have in prod, no more. This closes NOTES.md
question 6. See [§9](#9-deploy-order) for the one thing to verify because of it.

---

## 3. Decisions

### Carried forward from the 2026-09-18 design session

| Axis | Decision |
|---|---|
| Users not in the map who hold their own IAM | **Allow-list is the only path**; 403 on a miss. No fallback to forwarding the user's token, no feature-flagged dual path |
| Reading the secret | **Secret Manager client + in-memory TTL cache**. Not a Cloud Run env var, not a volume mount, not Redis |
| Refresh failure with a map in hand | **Serve the last good copy**, log, retry |
| Refactor depth for identity | **Add email to `DownloadRateLimiter`** and call it from `HEAD` too, rather than extracting a standalone resolver |

### Taken in this session

| # | Decision | Why |
|---|---|---|
| D1 | Swap `userinfo` → **`tokeninfo`** (`https://oauth2.googleapis.com/tokeninfo?access_token=…`) | One call returns `sub`, `email`, `email_verified`, `aud`, `scope` and `expires_in` — everything below, for the same round trip |
| D2 | **Verify `aud`** against a configured list of IGV OAuth client ids; reject with 401 otherwise | See [§4](#4-why-the-audience-check-is-in-scope). This is the security regression the ticket did not mention |
| D3 | **Require `email_verified is True`**; `false` and absent both refuse | One boolean, and it closes the obvious key-confusion attack on an email-keyed map. Closes NOTES.md question 5 |
| D4 | `sub` present but **no `email`** → **401** with an unmistakable log line | Google permits subset consent, so the per-user case is real and re-consent fixes it. The global-misconfiguration case is then instantly diagnosable in logs rather than hiding inside ordinary 403s |
| D5 | Cache `{sub, email}` as **JSON** under a **bumped** prefix `token_hash_v2`; TTL `min(expires_in, 3600)` | Bumping makes the rolling-deploy format incompatibility impossible rather than handled |
| D6 | Fix the latent `redis.set(key, None)` crash in `rate_limiter.py:76` | Pre-existing, but `tokeninfo` returns HTTP 400 `{"error": "invalid_token"}` for a bad token, so this path is reached far more often |
| D7 | **Authorize before metering** | A refused request must never consume budget |
| D8 | **Eager fetch of the access map in `lifespan`**, bounded retry, **raise on total failure** | Cloud Run then fails the revision and keeps the previous one serving. A broken secret or missing IAM binding becomes a failed deploy, not a 503 for every user. The lazy refresh and the 503 path stay exactly as lesson 0004 designed them — eager loading just makes 503 near-unreachable |
| D9 | TTL **300 s**, a constant, constructor-injectable for tests. **No env var** | A redeploy restarts every instance and refetches immediately, so deploying *is* the emergency fast path — the TTL never needs urgent tuning. Keeping it a constant means this security/availability dial moves only through a reviewed diff |
| D10 | Upstream **404 and 416 pass through** with opaque bodies; upstream **401/403/5xx → 502** | 404 and 416 are legitimate user-facing answers and IGV acts on 416. An upstream 401/403 now means the proxy's own IAM is broken — a gateway fault that must be alertable and must not look like a user denial |
| D11 | **403 body is actionable** | 403 is the *normal* response until a user is added, so it must route them to the fix |
| D12 | **Config project id is an explicit env var**; secret name stays a constant | On a laptop, ADC resolves to the developer's own gcloud project — silently the wrong secret. Explicit is greppable and testable |
| D13 | Client ids arrive as a **comma-separated env var**, required at startup | A client id is public, not a secret. A list allows rotation in the provisioning URL without an outage window |
| D14 | `HEAD` and range-less index `GET`s still pass **`rate_limiter=None`** to `GCSStreamer` | The limiter exists on those paths purely to call `resolve_user()`. Handing it over would call `record_download_stats` with `request_bytes=0`, writing zero-valued `dl_stats` keys that the nightly CSV exporter would emit as empty rows |
| D15 | **One PR** | Matches the ticket |
| D16 | **No pre-enumeration of proxy users** before cutover | Per §1. Absence from the map is a valid outcome with a self-service fix |

---

## 4. Why the audience check is in scope

Not in the ticket, not in any lesson. It is the reason D1 and D2 exist.

A Google access token records **who** it is for (`sub`/`email`), **which app** it was issued
to (`aud`), and **what it may do** (`scope`). It is not bound to this proxy — any app the
user signs into with Google holds its own token for them.

**Today**, the proxy forwards the user's token and GCS asks two questions: does this token
carry storage scope, and does this user hold IAM on the bucket? A token from some unrelated
"Sign in with Google" integration carries `openid email` only, so **GCS rejects it at the
first question**. It is inert against the proxy.

**After SET-1250**, the user's token is used for exactly one purpose: learning their email
address. It never goes upstream, so nothing checks what it was permitted to do or which app
it belongs to. `scope=openid email` becomes fully sufficient. That same unrelated
integration's token becomes a working key to genomic data.

This is a genuine regression, not a pre-existing hole. A stolen *IGV* token works in both
designs; what changes is how many tokens in the world are dangerous — from "those carrying
storage scope, essentially only IGV's" to "every token the user has, from every Sign-in-with-
Google integration they have ever used". SET-1252 removes the storage scope from IGV's own
config, confirming that `email` becomes the only scope that matters anywhere in this system.

**A check was removed from GCS and must be replaced.** `aud` says which OAuth client the
token was issued to. `userinfo` does not return it; `tokeninfo` does — along with `email`,
`email_verified` and `sub` — so one endpoint swap covers D1, D2 and D3 together. Volume is a
non-issue because the result is cached per token hash for an hour.

---

## 5. New configuration

Both are plain Cloud Run env vars added to the existing `envs` list in
`infrastructure/__main__.py`, beside `REDIS_HOST`. **No IAM changes in this repo** —
cpg-infra owns the `secretAccessor` and bucket read bindings.

| Env var | Required | Example | Notes |
|---|---|---|---|
| `IGV_PROXY_CONFIG_PROJECT` | yes | `cpg-igv-proxy-prod` | The stack's own project, where `igv-proxy-config` lives |
| `IGV_OAUTH_CLIENT_IDS` | yes | `1234-abc.apps.googleusercontent.com` | Comma-separated. Normally one value. Whitespace-trimmed, empty entries dropped |

**Both must fail fast at startup** if unset or empty — a missing config is a deploy failure,
never a silent loss of a security control and never a 401 for every user.

New constants in `server/utils/constants.py`:

```python
IGV_PROXY_CONFIG_SECRET_ID = 'igv-proxy-config'   # fixed by SET-1249, not a deployment choice
ACCESS_LIST_TTL_SECS = 300                        # revocation latency; see D9
TOKEN_HASH_PREFIX = 'token_hash_v2'               # BUMPED — see D5
TOKENINFO_URL = 'https://oauth2.googleapis.com/tokeninfo'
GCS_READ_SCOPE = 'https://www.googleapis.com/auth/devstorage.read_only'
PROXY_TOKEN_REFRESH_MARGIN_SECS = 300
```

New runtime dependencies in `pyproject.toml`: `google-cloud-secret-manager`, `google-auth`,
and `anyio` (already present transitively via starlette — declare it because it is imported
directly).

**Network:** Cloud Run runs with `vpc_access.egress='PRIVATE_RANGES_ONLY'`, so traffic to
Secret Manager, `oauth2.googleapis.com` and GCS takes the default internet path rather than
the VPC. No connector or Private Google Access change is needed.

---

## 6. File-by-file

| File | Change |
|---|---|
| `server/services/access_list.py` | **New.** `IgvProxyAccessList` — see [§7.1](#71-igvproxyaccesslist) |
| `server/services/proxy_credentials.py` | **New.** `ProxyCredentials` — see [§7.2](#72-proxycredentials) |
| `server/services/rate_limiter.py` | Split `check_user_limit` (`:36`) into `resolve_user()` and the existing `evaluate_download_limits()`. Swap `fetch_user_info` (`:88`) to `tokeninfo`. Add `aud`, `email_verified` and email checks. Cache `{sub, email}` as JSON. Guard the `None` write at `:76`. Retry decorator (`:51`) and single-flight lock (`:65`) **unchanged** |
| `server/utils/constants.py` | Bump `TOKEN_HASH_PREFIX` (`:19`). Add the constants in §5 |
| `server/utils/validation.py` | Identity resolution moves out of `rate_limit_if_applicable` (`:43`). Add the authorize step raising 403. Range/index logic (`:70`–`:77`) unchanged |
| `server/utils/helpers.py` | Keep `get_headers` (`:15`) as the inbound view. Add `build_gcs_headers(inbound, token)` |
| `server/utils/connections.py` | Add `get_access_list` and `get_proxy_credentials` beside the existing getters (`:11`, `:18`) |
| `server/main.py` | Construct both services in `lifespan` (`:30`), inject via `Depends`. Both handlers: resolve → authorize → (GET only) meter → swap headers → stream. Document why `HEAD` builds a limiter |
| `server/services/gcs_streamer.py` | Replace the verbatim relay at `:66` with the mapping in D10 |
| `infrastructure/__main__.py` | Add the two env vars to the container `envs` list. No IAM |
| `pyproject.toml` | Add the three dependencies |
| `tests/conftest.py` | Fixtures and `dependency_overrides` for both new services, mirroring `:46` |
| `tests/test_main.py` | Rewrite `test_proxy_success_index_file`; add the header-swap and authorization cases |
| `tests/test_rate_limiter.py` | Update for `tokeninfo`, the JSON cache value, and the split methods |
| `tests/test_validations.py` | Update for the changed `rate_limit_if_applicable` signature |

---

## 7. The two new services

Both follow the lifecycle already used for httpx and Redis: construct in `lifespan`, hang off
`app.state`, expose a getter in `connections.py`, inject with `Depends`. That is what makes
them overridable via `app.dependency_overrides` in tests. **Do not use module-level globals** —
they cannot be overridden and you will end up monkey-patching.

### 7.1 `IgvProxyAccessList`

Public surface is one predicate: `is_allowed(email: str, bucket: str) -> bool`.

- **Holds** `dict[str, frozenset[str]]`, email → bucket set, **keys lower-cased on parse**.
  Lower-case the incoming claim too: the secret derives from hand-written YAML, so
  `Alice@example.com` must not silently fail to match.
- **Models absent separately from empty.** `None` (never loaded) is not `{}` (loaded, nobody
  listed). Collapsing them makes a failed first fetch 403 every legitimate user while looking
  exactly like correct enforcement. Absent → **503**. This distinction is the single most
  important property of this class.
- **Eager load in `lifespan`** with bounded retry; raise if it never succeeds (D8).
- **Lazy refresh on read:** if `now > expires_at`, refetch. Guard with an `asyncio.Lock` so a
  TTL lapse under load does not start one fetch per in-flight request — the same stampede the
  Redis lock already prevents for identity resolution.
- **On refresh failure holding a map:** log an error, keep the last good copy, retry next tick.
  A Secret Manager blip must not take IGV down. The cost is that revocation is not guaranteed
  within the TTL under sustained failure, which is what the error log is for.
- Do **not** use a background timer: Cloud Run runs `min_instance_count=0` and CPU is not
  guaranteed outside a request, so a timer fires unreliably or on instances about to be
  reclaimed. Refresh-on-read ties the work to traffic.
- Use `SecretManagerServiceAsyncClient` to stay on the event loop without a thread hop.

Rejected: `cpg_utils.read_secret`. It is synchronous, its `fail_gracefully` default returns
`None` on error so it cannot distinguish absent from stale, and it is a large dependency for
one API call in a container whose job is copying bytes. Note this in the PR so a reviewer
asking "why not `cpg_utils`?" gets an answer.

### 7.2 `ProxyCredentials`

```python
credentials, _ = google.auth.default(scopes=[GCS_READ_SCOPE])
```

- Token via `credentials.refresh(Request())`, then read `credentials.token`.
- **Refresh inside `anyio.to_thread.run_sync`** — `google-auth`'s transport is synchronous and
  a blocking refresh on the event loop stalls every concurrent stream in the instance.
- Cache until `PROXY_TOKEN_REFRESH_MARGIN_SECS` before `credentials.expiry`.
- Guard the refresh with an `asyncio.Lock`, for the same reason as the access list: tokens
  expire at a single instant and every concurrent request notices simultaneously.
- Scopes are honoured for local ADC and key-file credentials. On the metadata server the
  instance's own scopes govern, so treat the narrow scope as belt-and-braces for local runs,
  not as the security control.

---

## 8. Request flow, after

```
                        ┌─ HEAD ─────────────────────────────┐
  parse path  →  validate_auth  →  resolve_user  →  authorize │→  swap headers  →  stream
  (400)          (401, shape)      (401)            (403/503) │
                                                              │
                        └─ GET ──→ …authorize → meter (429) ──┘
```

**`resolve_user()`** — the existing lock/cache/`tokeninfo` dance, unchanged in mechanism.
Sets `self.user_sub` and `self.user_email`; raises 401 if either is unavailable. Callable
from both handlers. Its checks, in order:

1. `aud` ∈ `IGV_OAUTH_CLIENT_IDS` → else **401**, log the rejected `aud`
2. `email` present → else **401**, log `userinfo/tokeninfo returned no email claim`
3. `email_verified is True` → else **401**, log whether it was `false` or absent
4. cache `{"sub": …, "email": …}` as JSON, TTL `min(expires_in, 3600)`

Two things that look like they will bite and do not. The `@retry(retry=retry_if_result(is_none), …)`
decorator survives untouched — `is_none` takes `Any` and tests identity, so widening the return
from `str` to a pair is invisible to it. The `SET … NX EX 10` single-flight lock survives
untouched — adding a second field to the cached value does not affect it, and the compare-and-
delete release is unchanged.

**Both request shapes gain a check they never had.** Today a range-less `.crai` returns from
`rate_limit_if_applicable` at `validation.py:74` before any identity call, and `HEAD` never
constructs a rate limiter at all (`main.py:51`). Both are served with the proxy's own
credentials after this change, so both must authorize — otherwise a `HEAD` leaks object
existence and size for any bucket the service account can reach, and index files (which live
in the same bucket as the data) are readable unattributed.

Per D14 and the carried-forward decision, the `HEAD` handler constructs a
`DownloadRateLimiter` with `request_bytes=0` **purely to call `resolve_user()`**, never meters
with it, and never passes it to `GCSStreamer`. **Say this in the handler's docstring** — it
reads oddly six months from now.

### 403 message (D11)

403 is the expected response for a new external collaborator until their PR lands, so it must
be self-service. Name the bucket the caller supplied — this discloses nothing, since they typed
it, and it says nothing about whether the bucket exists:

> Not authorized to read bucket `<bucket>`. If you hold personal IAM on this bucket, use IGV's
> native Google access instead of the proxy. Otherwise, request access by opening a PR against
> `cpg-infrastructure-private` adding your email to the `igv-desktop-access` list for the
> dataset that owns this bucket.

Do **not** derive the dataset name from the bucket: the storage prefix is configurable and a
wrong dataset name in an error message is worse than none.

---

## 9. Status codes, after

| Code | Meaning | Source |
|---|---|---|
| 400 | Malformed path, or data file with no `Range` | `validation.py:20`, `validation.py:77` |
| 401 | Identity unknown or untrusted — header missing/malformed, `tokeninfo` gave nothing, **`aud` mismatch**, **no `email`**, **`email_verified` not true** | `validation.py:33`, `rate_limiter.py` |
| 403 | **New.** Identity known and trusted, bucket not permitted | authorize hop |
| 404 | Object not found — passed through from GCS, **body replaced** | `gcs_streamer.py` |
| 416 | Range not satisfiable — passed through from GCS, **body replaced** | `gcs_streamer.py` |
| 429 | Permitted, but out of budget this window | `validation.py:90` |
| 502 | **New.** Upstream 401/403/5xx — the proxy's own access is broken | `gcs_streamer.py` |
| 503 | **New.** No access map has ever loaded — fail closed | `access_list.py` |

Gone: a 403 relayed from GCS on the user's behalf. That failure mode no longer exists, and an
upstream refusal is now an operational alarm about the proxy's own IAM.

**Never relay `exc.response.text`** (`gcs_streamer.py:66`). Log the upstream body in full;
return something opaque. After the credential swap the only principal GCS sees is the proxy's
service account, so its error XML names that service account and confirms the bucket exists.
Before this ticket that line was merely unhelpful; now it is a disclosure.

---

## 10. Test plan

Rewrite:

- `test_proxy_success_index_file` — asserts a bare `.crai` GET with `Bearer token123` returns
  200. It encodes the old boundary. **A plan that leaves this test untouched has not moved the
  boundary.**
- `tests/test_validations.py` — `rate_limit_if_applicable`'s signature and responsibilities change.
- `tests/test_rate_limiter.py` — `tokeninfo` response shape, JSON cache value, split methods.

Extend `conftest.py` with fixtures and `dependency_overrides` for both new services, mirroring
the existing pair at `:46`.

New cases. `mock_httpx_client` is built on `httpx.MockTransport` (`conftest.py:34`) whose handler
receives the **outgoing** request — capture it and assert on what the proxy actually sent:

- [ ] Upstream `Authorization` **is** the proxy's token and **is not** the caller's `Bearer token123`
- [ ] `Range` and `Accept` cross over unchanged
- [ ] `build_gcs_headers` returns a fresh dict — the inbound dict is not mutated
- [ ] Listed user + unlisted bucket → 403 **and no upstream request is made**
- [ ] Unlisted email → 403, no upstream request
- [ ] No map ever loaded → **503**, not 403
- [ ] Refresh fails with a cached map → still serves
- [ ] `aud` not in `IGV_OAUTH_CLIENT_IDS` → 401, no upstream request
- [ ] `email_verified` false → 401; `email_verified` absent → 401
- [ ] `sub` present, `email` absent → 401
- [ ] `tokeninfo` returns `{"error": "invalid_token"}` → 401, **no Redis write of `None`**
- [ ] Index-file GET is authorized (the old 200-without-identity behaviour is gone)
- [ ] `HEAD` is authorized but **not** metered, and writes no `dl_stats` key
- [ ] Upstream 404 → 404 with an opaque body; upstream 403 → **502**; upstream 416 → 416
- [ ] Budget behaviour unchanged for a listed user: deduct, refund on upstream error, stats recorded
- [ ] A 403 from the authorize hop does **not** deduct budget (D7)
- [ ] `lifespan` raises when the first secret fetch never succeeds (D8)

The "no upstream request was made" assertions are the valuable ones. "Returns 403" and "returns
403 without touching GCS" are different guarantees, and only the second demonstrates that the
boundary actually moved into this codebase.

---

## 11. Deploy order

1. **cpg-infrastructure #399 merged and deployed.** Creates the secret, its version, the
   accessor binding, and the bucket read bindings. *Blocks everything* — enforcing an absent
   secret 503s every user. With D8, it would actually fail the revision instead, which is the
   intended safety net.
2. **Verify the `igv_proxy` config.** Each stack's `project` and `server_machine_account` must
   match the SA this repo creates in `infrastructure/__main__.py`. A typo surfaces as an
   unreadable secret, not a deploy error.
3. **Verify the dev SA's bucket grants.** Because dev is intentionally handed the prod `-main`
   payload (§2), cpg-infra must grant the **dev** service account read on those prod buckets.
   If it only grants prod's SA, dev will authorize a user and then fail the fetch — which
   under D10 surfaces as a 502 and reads as a broken proxy rather than a missing grant.
4. **Deploy the proxy.** Enforcement begins. Revocation latency from here equals `ACCESS_LIST_TTL_SECS`
   (300 s). Plan the rollback — redeploy the previous image — before this step.
5. **Populate `igv-desktop-access` as needed.** Parallel work, **not a gate** (§1, D16).
   cpg-infrastructure-private #780 covers fewgenomes, sandbox and thousand-genomes.

**Alerting.** The private stack alerts on 4xx/5xx rates. This change moves that signal: new
authorize-hop 403s appear in normal traffic, and upstream IAM problems now arrive as 502s
rather than passed-through 403s. Check the thresholds before step 4 so the cutover does not
trip an alert that means nothing, or — worse — so a real 502 is not lost in expected 403 noise.

**Verify before cutover:** that IGV's tokens actually carry the `email` scope. Today's code only
ever reads `sub`, so this has never been exercised; if the scope is absent, every request 401s
and no test in this repo would catch it. One `tokeninfo` call with a real IGV-issued token
settles it. This is a **release gate, not a code gate** — the implementation is identical either
way, and the fix if it fails lives in the OAuth provisioning URL (`README.md:87`), outside this
repo. Note that **SET-1252 edits that same config** to drop the cloud-storage scope, so whoever
picks it up must know `email` has become load-bearing.

---

## 12. Recorded, deliberately not built

Not in this ticket. Written down so they are decisions rather than oversights.

- **Index-file and `HEAD` attribution.** Those requests now resolve an identity but record no
  stats. Doing it properly means taking the byte count from `Content-Length` after the response
  headers arrive, which changes the metering model and the reserve-then-refund invariant, and
  alters what the nightly CSV exporter emits. Its own ticket. Valuable once external
  collaborators arrive under SET-1248.
- **Mid-stream disconnect is billed in full.** `record_download_stats` is called before the body
  streams (`gcs_streamer.py:50`) and `refund()` only fires on exceptions raised before the
  response is returned. NOTES.md question 2, still unanswered.
- **The 1 GiB / 1 hour cap is hard-coded** (`constants.py:10`) with no per-user or per-bucket
  override. NOTES.md question 3. External users under SET-1248 may need one.
- **No negative caching of rejected tokens.** Every request bearing a token that fails the `aud`,
  `email` or `email_verified` checks costs one `tokeninfo` call. Pre-existing in shape — the same
  is true of `userinfo` today, made worse by the five-attempt tenacity loop — and Cloud Armor
  throttles per source IP at the edge. A short negative cache under the same token-hash key would
  close it cheaply if it ever bites.

---

## Supersedes

This session changed things these files still assert. **None of them were edited** — another
agent can assume the rest of the tree is exactly as it was.

| File | What is now stale |
|---|---|
| `NOTES.md` | Q4 (email scope) → answered as a *release gate*, build proceeds. Q5 (`email_verified`) → **require it**. Q6 (dev sees prod buckets) → **accepted as intended**, not a defect |
| `GLOSSARY.md` | `Access map`, `Allow-list-only`, `Proxy credentials`, `Fail closed`, `Revocation latency` are all now decided, so the **(provisional)** markers can drop. `User token` says the proxy "exchanges it at Google's `userinfo` endpoint" — now `tokeninfo`. `Proxy` should carry the purpose in §1. Needs new terms for the audience check and the access-map TTL |
| `reference/set-1250-change-map.html` | Its decision table, file-by-file table, status-code table, test checklist and deploy order all predate D1–D16. The §2 contract and the general shape still hold |
| `lessons/0003-identity-sub-and-email.html` | Teaches `userinfo`; the implementation uses `tokeninfo`. Its `email_verified` and case-normalisation recommendations are now rulings, not recommendations |
| `lessons/0005-the-header-swap.html` | Says upstream errors return "something opaque" without pinning codes — now D10. Its deploy order makes populating `igv-desktop-access` a hard gate — it is not |
| `learning-records/0002-set-1250-design-decisions.md` | The "breaking change / every current user must be listed first" framing is overstated. See §1 |

Every `file:line` reference above is valid against commit `28c6146`. Re-pin after the code moves —
`linkify.py` is the helper, and `NOTES.md` describes the procedure.
