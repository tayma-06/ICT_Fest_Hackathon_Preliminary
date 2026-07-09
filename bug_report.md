# Bug Report — CoWork: Multi-Tenant Coworking Space Booking API

All bugs listed below were discovered during code review and verified as fixed
in the current codebase. Line numbers below refer to the **current** version of
each file at commit `def860f`.

---

## Authentication

### 1. Access token lifetime used minutes×60, giving 15 hours instead of 15 minutes

- **Location:** `app/auth.py:55-57`, `create_access_token`
- **Problem:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` where
  `ACCESS_TOKEN_EXPIRE_MINUTES=15` produced a lifetime of 900 minutes (15 hours).
  JWT `exp − iat` was 54000 instead of 900.
- **Why it was wrong:** Contract rule 8 requires access tokens to expire in
  exactly 900 seconds (15 minutes). A 15-hour token window violates security
  assumptions and contradicts the documented expiry.
- **Fix:** Changed to `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` →
  `exp − iat == 900`.
- **Verification:** `tests/test_contract_live.py::test_jwt_claims_and_lifetimes`,
  `tests/test_jwt_audit.py::test_created_tokens_use_hs256_required_claims_lifetimes_and_unique_jtis`

---

### 2. Logout stored `jti` but checked `sub`, so revocation never matched

- **Location:** `app/auth.py:146-148`, `get_token_payload`
- **Problem:** `revoke_access_token` (line 118) stores the token's `jti` in
  `_revoked_tokens`, but the request path checked `payload.get("sub") in
  _revoked_tokens`. A user ID (integer-as-string) never equals a jti
  (hex string), so the check always evaluated to False.
- **Why it was wrong:** Contract rule 8 requires that a logged-out access token
  returns 401 on subsequent use. The check was comparing values from two
  different claims and could never match, making logout a no-op.
- **Fix:** Changed check to `payload.get("jti") in _revoked_tokens`.
- **Verification:** `tests/test_contract_live.py::test_logout_blacklists_only_presented_token`,
  `tests/test_logout_refresh_audit.py::test_logout_requires_access_token_and_revokes_only_presented_token`

---

### 3. Refresh tokens were indefinitely reusable (no single-use enforcement)

- **Location:** `app/routers/auth.py:89-103`, `refresh`;
  `app/auth.py:126-135`, `consume_refresh_token`
- **Problem:** `POST /auth/refresh` issued a new token pair but never
  invalidated the presented refresh token. The same refresh token could be
  redeemed an unlimited number of times.
- **Why it was wrong:** Contract rule 8 requires refresh tokens to be single-use.
  Reuse of the same refresh token must return 401 UNAUTHORIZED.
- **Fix:** Added `consume_refresh_token()` which atomically checks the refresh
  token's `jti` against the revocation set and records it under a lock. Called
  from the `refresh` endpoint after token-type validation.
- **Verification:** `tests/test_contract_live.py::test_refresh_rotation_single_use`,
  `tests/test_logout_refresh_audit.py::test_refresh_rotates_tokens_and_consumes_old_refresh_token`

---

### 4. Duplicate registration returned 200 with the existing user's data instead of 409

- **Location:** `app/routers/auth.py:41-47`, `register`
- **Problem:** When the username already existed in the org, the endpoint
  silently returned the existing user's data with HTTP 200. This leaked account
  existence, role, and user_id to any caller.
- **Why it was wrong:** Contract rule 15 specifies duplicate username within the
  same org → 409 USERNAME_TAKEN. Concurrent registration of the same username
  could also crash with a 500 IntegrityError.
- **Fix:** Raise `AppError(409, "USERNAME_TAKEN", ...)`. Wrap org/user commits
  in IntegrityError handlers: concurrent registrations also produce 409, and
  concurrent org creation joins as member instead of crashing.
- **Verification:** `tests/test_contract_live.py::test_register_roles_duplicates_and_cross_org_username`,
  `tests/test_registration_login_audit.py::test_duplicate_username_within_same_org_returns_username_taken`

---

### 5. Malformed JWT subjects caused 500 instead of 401

- **Location:** Various: `app/auth.py:182-189` (`user_id_from_payload`),
  `app/auth.py:85-96` (`decode_token`), `app/auth.py:154-158` (`token_jti_from_payload`),
  `app/routers/auth.py:94` (`refresh`)
- **Problem:** `int(payload["sub"])` raised `ValueError` if a valid-signature
  token contained a non-integer `sub` (e.g., `"abc"`). Tokens missing required
  claims (`jti`, `org`, `role`, etc.) or carrying a malformed `jti` could also
  bypass validation or raise raw exceptions.
- **Why it was wrong:** Contract rule 8 requires that any invalid, missing,
  expired, or blacklisted token returns 401 UNAUTHORIZED. Raw 500 errors leak
  implementation details and break the API contract.
- **Fix:** Added `_REQUIRED_TOKEN_CLAIMS` to `jwt.decode`'s `require` option,
  plus shared helpers `user_id_from_payload` and `token_jti_from_payload` that
  validate `sub` and `jti` before using them. Both access-token and refresh-token
  paths now return 401 for malformed subjects or token IDs.
- **Verification:** `tests/test_jwt_audit.py::test_missing_malformed_or_invalid_authorization_headers_return_401`,
  `tests/test_regressions.py::test_malformed_access_token_subject_returns_401`,
  `tests/test_regressions.py::test_malformed_refresh_token_subject_returns_401`

---

### 6. Token revocation (logout and consumed refresh tokens) was lost after server restart

- **Location:** `app/models.py:72-77` (`RevokedToken` table),
  `app/auth.py:118-123` and `126-135` (database persistence),
  `app/auth.py:168-179` (`_is_revoked_jti`, `_store_revoked_jti`),
  `app/routers/auth.py:107-108` (`logout`)
- **Problem:** Revoked access-token JTIs and consumed refresh-token JTIs lived
  only in the process-local `_revoked_tokens` set. Restarting the server cleared
  the set, so a logged-out access token worked again and a previously used
  refresh token could be reused after restart.
- **Why it was wrong:** Contract rule 8 requires logout to invalidate the
  presented access token for all further use, and refresh tokens to be single-use.
  Restart-induced revocation loss breaks both guarantees.
- **Fix:** Added a `revoked_tokens` table. `revoke_access_token` and
  `consume_refresh_token` now persist each JTI with its expiry. Validation
  checks both the in-memory set and the persisted table.
- **Verification:** `tests/test_restart_persistence.py::test_stats_and_token_revocations_survive_restart`,
  `tests/test_logout_refresh_audit.py::test_logout_revocation_is_persisted_until_access_token_expiry`

---

### 7. JWT claims with incorrect types or enumerated values were accepted as valid

- **Location:** `app/auth.py:85-96` (`decode_token`), `app/auth.py:99-115`
  (`validate_token_claims`)
- **Problem:** After `jwt.decode()` validated the signature, expiry, and
  required-claim presence, the decoded payload was used without checking that
  individual claim values had the correct types or came from the allowed set.
  A token could carry `"org": "abc"` (string instead of int), `"role":
  "superadmin"` (not in `{"admin","member"}`), `"type": "session"` (not in
  `{"access","refresh"}`), `"iat": "now"` (string instead of int), or
  `"jti": 123` (int instead of string). Downstream helpers caught `sub` and
  `jti` type errors but `org`, `role`, `iat`, and `type` were never validated.
- **Why it was wrong:** Contract rule 8 defines exact types and values for every
  JWT claim. Accepting malformed claims could mask bugs, produce confusing
  responses, or allow tokens that violate the intended type semantics.
- **Fix:** Added `validate_token_claims(payload)` called from `decode_token`.
  Validates every required claim for correct type (`sub`: `str`, `org`: `int`,
  `role`: `str` in `_ROLES`, `jti`: non-empty `str`, `iat`: `int`, `exp`:
  `int`, `type`: `str` in `_TOKEN_TYPES`). Invalid claims raise 401 UNAUTHORIZED.
- **Verification:** `tests/test_jwt_audit.py::test_invalid_claim_types_or_values_return_401`
  exercises 9 malformed-claim tokens and asserts each returns 401.

---

## Multi-tenancy

### 8. Members could read other members' bookings

- **Location:** `app/routers/bookings.py:175-176`, `get_booking`
- **Problem:** The detail endpoint scoped only by org (via the Room join), not
  by owner. Any member could fetch another member's booking by ID. The cancel
  endpoint had an owner check but the read endpoint did not.
- **Why it was wrong:** Contract rule 10 states members read only their own
  bookings; another member's booking ID → 404 BOOKING_NOT_FOUND.
- **Fix:** Added `if user.role != "admin" and booking.user_id != user.id →
  404`, matching the check already used in cancel.
- **Verification:** `tests/test_contract_live.py::test_member_visibility_and_admin_access`

---

### 9. CSV export leaked other organizations' bookings

- **Location:** `app/services/export.py:22-26` (`_fetch_scoped`),
  `app/routers/admin.py:73-76` (`export`)
- **Problem:** With `include_all=true&room_id=<id>`, the original code called
  `fetch_bookings_raw(db, room_id)` which filtered only by room ID with no org
  check. An admin of org A could pass a room ID belonging to org B and download
  org B's entire booking history.
- **Why it was wrong:** Contract rule 9 requires cross-org resource IDs to
  behave as non-existent (404) on every code path. This was a cross-tenant data
  leak.
- **Fix:** The export always goes through `_fetch_scoped(db, org_id, ...)` which
  joins through `Room.org_id`. The router now returns 404 ROOM_NOT_FOUND when
  the requested `room_id` doesn't exist in the caller's org.
- **Verification:** `tests/test_contract_live.py::test_multi_tenancy_cross_org_404`,
  `tests/test_contract_live.py::test_export_header_and_scoping`

---

## Booking validation

### 10. Timezone offsets were silently dropped instead of converted to UTC

- **Location:** `app/timeutils.py:11-13`, `parse_input_datetime`
- **Problem:** `dt.replace(tzinfo=None)` stripped the timezone offset without
  normalizing. `10:00+06:00` was stored as `10:00 UTC` instead of `04:00 UTC`.
  Every downstream comparison (future check, overlap, quota window, availability
  date, report ranges) and every response datetime was wrong for any client that
  sent an offset-aware timestamp.
- **Why it was wrong:** Contract rule 1 requires all datetimes to be stored and
  returned in UTC. Dropping offsets without conversion corrupts the timeline.
- **Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)` normalizes to UTC
  first, then stores a naive-UTC value.
- **Verification:** `tests/test_contract_live.py::test_offset_input_converted_to_utc_and_responses_utc`,
  `tests/test_contract_live.py::test_naive_input_treated_as_utc`

---

### 11. 300-second grace window allowed bookings starting in the past

- **Location:** `app/routers/bookings.py:94`, `create_booking`
- **Problem:** `if start <= now - timedelta(seconds=300)` allowed a booking
  starting up to 5 minutes in the past to succeed.
- **Why it was wrong:** Contract rule 2 requires `start_time` to be strictly in
  the future with no grace window.
- **Fix:** `if start <= now → 400 INVALID_BOOKING_WINDOW`.
- **Verification:** `tests/test_contract_live.py::test_booking_window_validation`

---

### 12. Minimum duration never enforced (zero-hour and negative bookings accepted)

- **Location:** `app/routers/bookings.py:101-102`, `create_booking`
- **Problem:** Only `duration_hours > MAX_DURATION_HOURS` was rejected.
  `end == start` (0 hours) passed and created a zero-price booking. `end` before
  `start` produced a negative price.
- **Why it was wrong:** Contract rule 2 requires duration to be a whole number
  of hours between 1 and 8, with `end_time` strictly after `start_time`.
- **Fix:** `if duration_hours < MIN_DURATION_HOURS or duration_hours >
  MAX_DURATION_HOURS → 400`. Also wrapped datetime parsing in try/except so
  malformed datetime strings return 400 instead of 500.
- **Verification:** `tests/test_contract_live.py::test_booking_window_validation`

---

### 13. Overlap check rejected back-to-back bookings

- **Location:** `app/routers/bookings.py:55`, `_has_conflict`
- **Problem:** `b.start_time <= end and start <= b.end_time` used inclusive
  comparisons. A booking 12:00–13:00 conflicted with an existing 10:00–12:00,
  so legal back-to-back bookings returned 409.
- **Why it was wrong:** Contract rule 3 defines overlap only when
  `existing.start < new.end AND new.start < existing.end` (strict). Adjacent
  intervals are allowed.
- **Fix:** Changed to strict `<` on both comparisons.
- **Verification:** `tests/test_contract_live.py::test_overlap_and_back_to_back`

---

### 14. Pagination used descending order, off-by-one page offset, and hard-coded page size

- **Location:** `app/routers/bookings.py:147-150`, `list_bookings`
- **Problem:** Three violations of contract rule 11:
  1. `order_by(Booking.start_time.desc(), ...)` — spec requires ascending.
  2. `.offset(page * limit)` — page 1 skipped the first `limit` items.
  3. `.limit(10)` — the `limit` query parameter was ignored.
- **Why it was wrong:** The pagination contract (rule 11) specifies ascending
  order by start_time, correct offset (`(page-1) * limit`), and respect for the
  user-supplied `limit` parameter.
- **Fix:** `order_by(start_time.asc(), id.asc()).offset((page - 1) * limit).limit(limit)`.
- **Verification:** `tests/test_contract_live.py::test_pagination_ordering_and_no_skips`

---

## Concurrency

### 15. Double-booking and quota races (check-then-act with no synchronization)

- **Location:** `app/routers/bookings.py:27-29` (`_booking_lock`),
  `108-131` (`create_booking` critical section)
- **Problem:** The conflict check (`_has_conflict`), quota check
  (`_check_quota`), and INSERT were not atomic. Deliberate `time.sleep` calls
  (`_pricing_warmup`, `_quota_audit`) widened the race window so two concurrent
  requests for the same slot both passed `_has_conflict` and both committed,
  producing two confirmed overlapping bookings.
- **Why it was wrong:** Contract rules 3 and 4 must hold under concurrent
  requests. Without synchronization, exactly-one semantics for a time slot and
  quota caps are violated.
- **Fix:** Added a module-level `threading.Lock`. Conflict check, quota check,
  insert/commit, stats update, and cache invalidation run inside
  `with _booking_lock:`.
- **Verification:** `tests/test_contract_live.py::test_concurrent_double_booking_exactly_one_wins`

---

### 16. Concurrent cancels produced two refunds

- **Location:** `app/routers/bookings.py:196-231`, `cancel_booking`
- **Problem:** The status check and the status update were separated by
  `log_refund` and a deliberate `_settlement_pause()` sleep. Two concurrent
  cancels of the same booking both saw `status == "confirmed"`, both logged a
  RefundLog, and both returned 200.
- **Why it was wrong:** Contract rule 6 requires exactly one RefundLog per
  cancellation; concurrent cancels must yield exactly one 200 with the rest 409
  ALREADY_CANCELLED.
- **Fix:** The entire cancel critical section (fetch → status check → refund log
  → status update/commit) runs under the same `_booking_lock`.
- **Verification:** `tests/test_contract_live.py::test_concurrent_cancel_one_success_one_refund_log`

---

### 17. Room stats lost updates under concurrency

- **Location:** `app/services/stats.py:22-27` (`record_create`),
  `30-38` (`record_cancel`)
- **Problem:** `record_create` and `record_cancel` did read → sleep → write on
  a shared `_stats` dict. Two concurrent bookings both read `count=N` and both
  wrote `N+1`, permanently desynchronizing `/rooms/{id}/stats` from the real
  bookings.
- **Why it was wrong:** Contract rule 14 requires stats to always be consistent,
  including after concurrent bursts.
- **Fix:** Read-modify-write is now atomic under a `threading.Lock`. The
  `_aggregate_pause()` sleep was moved outside the critical section.
- **Verification:** `tests/test_contract_live.py::test_stats_consistent_after_concurrent_burst`

---

### 18. Rate limiter lost updates under concurrency

- **Location:** `app/services/ratelimit.py:20-29`, `record_and_check`
- **Problem:** Same read → sleep → write pattern on the per-user bucket list.
  Concurrent requests each appended to their own copy and the last write won,
  undercounting requests so a user could exceed 20 requests in 60 seconds
  without ever seeing 429.
- **Why it was wrong:** Contract rule 5 requires the rate limit to hold under
  concurrent requests.
- **Fix:** Trim + append + count now execute atomically under a
  `threading.Lock`. The `_settle_pause()` was moved outside the critical section.
- **Verification:** `tests/test_contract_live.py::test_rate_limit_25_requests_exactly_5_rejected`,
  `tests/test_contract_live.py::test_rate_limit_is_per_user`

---

### 19. Duplicate reference codes under concurrency

- **Location:** `app/services/reference.py:34-40`, `next_reference_code`
- **Problem:** `next_reference_code` read the counter, slept 0.12 s, then wrote
  back `current + 1`. Concurrent creations read the same counter value and
  produced identical reference codes, violating the unique constraint and
  potentially returning a database 500.
- **Why it was wrong:** Contract rule 7 requires unique reference codes,
  including under concurrent creation.
- **Fix:** Read-and-increment is atomic under a `threading.Lock`. The formatting
  pause (`_format_pause()`) happens outside the critical section.
- **Verification:** `tests/test_contract_live.py::test_reference_codes_unique_under_concurrent_creation`,
  `tests/test_contract_live.py::test_zz_all_reference_codes_globally_unique`

---

### 20. Deadlock between booking-created and booking-cancelled notifications

- **Location:** `app/services/notifications.py:24-28` (`notify_created`),
  `31-37` (`notify_cancelled`)
- **Problem:** Classic lock-order inversion: `notify_created` acquired
  `_email_lock` then `_audit_lock`, while `notify_cancelled` acquired
  `_audit_lock` then `_email_lock`. A concurrent create + cancel could each
  hold one lock and wait forever on the other, hanging both requests and every
  subsequent create/cancel.
- **Why it was wrong:** Contract rule 16 requires liveness under concurrent
  mixed workloads. A deadlock violates this.
- **Fix:** Both functions now acquire locks in the same global order
  (email → audit), making deadlock impossible.
- **Verification:** `tests/test_contract_live.py::test_liveness_mixed_creates_and_cancels`

---

## Cancellation and refunds

### 21. Refund tiers wrong at both boundaries

- **Location:** `app/routers/bookings.py:212-218`, `cancel_booking`
- **Problem (a):** `notice_hours = int(notice.total_seconds() // 3600)` floored
  to whole hours; `if notice_hours > 48` required *more than* 48 whole hours.
  Notice in `[48h, 49h)` was floored to 48 and paid 50% instead of the
  specified 100%.
- **Problem (b):** The `else` branch (notice < 24 h) set `refund_percent = 50`
  instead of 0 — last-minute cancellations were refunded half the price.
- **Why it was wrong:** Contract rule 6 specifies: notice ≥ 48 h → 100%,
  ≥ 24 h → 50%, < 24 h → 0%.
- **Fix:** Compare the timedelta directly: `notice >= 48h → 100`,
  `elif notice >= 24h → 50`, `else → 0`.
- **Verification:** `tests/test_contract_live.py::test_refund_tiers_and_half_cent_rounding`

---

### 22. Refund amount: banker's rounding in the response, truncation in the ledger

- **Location:** `app/services/refunds.py:14-17`, `log_refund`
- **Problem:** The cancel response used Python `round()` (banker's rounding:
  50.5 → 50), while `log_refund` computed the stored amount independently via
  floats and `int()` truncation. For an odd price at 50% (e.g., 333 → 166.5¢)
  the response and the RefundLog could disagree or both be wrong.
- **Why it was wrong:** Contract rule 6 requires half-cents to round **up**, and
  the response amount must equal the RefundLog amount.
- **Fix:** Single source of truth in `log_refund` with exact integer half-up
  rounding: `amount_cents = (price_cents * percent + 50) // 100`. The route
  returns `entry.amount_cents` from the created RefundLog row.
- **Verification:** `tests/test_contract_live.py::test_refund_tiers_and_half_cent_rounding`
  (checks response and GET /bookings/{id} refunds list agree)

---

### 23. GET /bookings/{id} returned `created_at` as `start_time`

- **Location:** `app/routers/bookings.py:178-186`, `get_booking`
- **Problem:** `response["start_time"] = iso_utc(booking.created_at)` overwrote
  the correct serialized start time with the creation timestamp. The
  single-booking view showed the wrong time.
- **Why it was wrong:** The booking detail response must show the actual booking
  start time, not the creation timestamp.
- **Fix:** Removed the overwrite line.
- **Verification:** Observed during contract test assertions on booking detail
  fields.

---

## Pagination and export

### 24. Admin export filtered by caller's own user_id when `include_all=False`

- **Location:** `app/services/export.py:29-35` (`generate_export`),
  `app/routers/admin.py:77` (`export`)
- **Problem:** The original `generate_export` accepted a `user_id` parameter and
  filtered bookings by it when `include_all=False`. The router passed `admin.id`,
  so an admin calling `/admin/export` without `include_all=true` saw only their
  own bookings instead of all bookings in their organization.
- **Why it was wrong:** Contract rule 9 specifies the export returns all
  org-scoped bookings. The `include_all` parameter was intended to scope rooms,
  not to toggle between "all users" and "just me."
- **Fix:** Removed `user_id` from the export signature and query. The export now
  always returns all org-scoped bookings (optionally filtered by `room_id`).
- **Verification:** `tests/test_contract_live.py::test_export_header_and_scoping`,
  `tests/test_regressions.py::test_admin_export_uses_exact_header_and_all_org_bookings_by_default`

---

## Reports and caching

### 25. Reports not invalidated on booking create; availability not invalidated on cancel

- **Location:** `app/routers/bookings.py:129-131` (`create_booking`),
  `227-229` (`cancel_booking`)
- **Problem:** `create_booking` invalidated only the availability cache, so a
  cached `/admin/usage-report` kept serving old counts after a new booking.
  `cancel_booking` invalidated only the report cache, so `/rooms/{id}/availability`
  kept showing a cancelled booking as busy.
- **Why it was wrong:** Contract rules 12 and 13 require both caches to reflect
  the current state immediately after any booking mutation.
- **Fix:** Create now also calls `cache.invalidate_report(user.org_id)`. Cancel
  now also calls `cache.invalidate_availability(room_id, start_date)`.
- **Verification:** `tests/test_contract_live.py::test_usage_report_zero_rooms_ranges_and_freshness`,
  `tests/test_contract_live.py::test_availability_sorted_and_fresh`

---

### 26. Usage-report cache not invalidated when a room is created

- **Location:** `app/routers/rooms.py:57`, `create_room`
- **Problem:** `/admin/usage-report` caches results per `(org, from, to)`. Only
  booking create/cancel invalidated that cache. Creating a room did not, so a
  previously cached report for the same range kept being served without the new
  room.
- **Why it was wrong:** Contract rule 12 requires the report to list every room
  in the org, including zero-booking rooms, and "must reflect the current state
  immediately."
- **Fix:** `create_room` now calls `cache.invalidate_report(admin.org_id)` after
  commit.
- **Verification:** `tests/test_contract_live.py::test_usage_report_zero_rooms_ranges_and_freshness`
  (the newly-created R3 appears immediately in the report)

---

### 27. Report and availability caches could store stale data after invalidation (write-after-invalidate race)

- **Location:** `app/cache.py:26-29` (`set_report`), `49-53` (`set_availability`),
  `32-36` (`invalidate_report`), `56-60` (`invalidate_availability`)
- **Problem:** Cache reads, writes, and invalidations had no synchronization or
  version check. A slow report/availability request could compute old data, a
  booking mutation could invalidate the cache, and then the slow request could
  write the old result back into the cache after invalidation. Later reads would
  see stale data.
- **Why it was wrong:** Contract rules 12 and 13 require immediate freshness. A
  write-after-invalidate race breaks this guarantee.
- **Fix:** Added a `_cache_lock` and per-key/per-org generation counters.
  Routes capture the generation before computing uncached data; `set_*` only
  writes if no invalidation has happened since that computation began.
- **Verification:** `tests/test_regressions.py::test_cache_rejects_stale_set_after_invalidation`

---

## Persistence and restart safety

### 28. Reference-code counter reset after server restart

- **Location:** `app/services/reference.py:25-31` (`_next_after_existing_bookings`),
  `34-40` (`next_reference_code`)
- **Problem:** Reference codes were generated from an in-memory counter
  initialized to `1000` on every process start. With a persistent SQLite
  database, the next booking after a restart could reuse an existing code such
  as `CW-001000`, violating the unique constraint.
- **Why it was wrong:** Contract rule 7 requires unique reference codes across
  the entire lifetime of the service, including after restarts.
- **Fix:** `next_reference_code()` now receives the database session, seeds the
  counter from the highest existing `CW-<number>` code, and then issues the next
  value under the existing lock.
- **Verification:** `tests/test_regressions.py::test_reference_code_continues_after_existing_database_values`

---

### 29. Room stats reset after server restart

- **Location:** `app/services/stats.py:45-51` (`get_live`),
  `app/routers/rooms.py:111-112` (`room_stats`)
- **Problem:** `/rooms/{id}/stats` returned values from an in-memory `_stats`
  dictionary. After restart, a room with persisted confirmed bookings reported 0
  bookings and 0 revenue.
- **Why it was wrong:** Contract rule 14 requires stats to always equal the
  values derivable from bookings. In-memory-only tracking loses this property
  on restart.
- **Fix:** Added `get_live(db, room_id)` which queries confirmed `Booking` rows
  from the database using `func.count()` and `func.sum()`. The API response is
  database-backed and restart-safe.
- **Verification:** `tests/test_restart_persistence.py::test_stats_and_token_revocations_survive_restart`

---

## Verification

All fixes are verified by a multi-layer test suite:

- **Live contract tests** (`tests/test_contract_live.py`, 30 tests) — spawn
  uvicorn on a fresh SQLite database and exercise every business rule over real
  HTTP, including concurrent double-booking, quota enforcement, rate limiting,
  concurrent cancellation, stats consistency, cache freshness, multi-tenancy,
  export scoping, and liveness.
- **Regression tests** (`tests/test_regressions.py`, 7 tests) — verify specific
  edge cases: export header correctness, malformed JWT subjects and claims,
  reference-code continuity with existing database values, and cache generation
  freshness.
- **Dedicated audit suites** — `tests/test_jwt_audit.py` (5 tests) for JWT
  claim validation and token lifecycles; `tests/test_logout_refresh_audit.py`
  (6 tests) for logout semantics, refresh rotation, and concurrent refresh races;
  `tests/test_registration_login_audit.py` (6 tests) for registration contract,
  duplicate handling, login scoping, and password hashing.
- **Restart persistence tests** (`tests/test_restart_persistence.py`, 1 test) —
  start the server, create data, kill it, restart, and verify stats and
  revocation survive.

Full local run: **54 passed**.
