# Bug Report — CoWork: Multi-Tenant Coworking Space Booking API

Each entry lists where the bug was, what was wrong and why it produced incorrect
observable behavior, and how it was fixed. Line numbers refer to the **original**
(broken) code.

---

## 1. Access tokens lived 900 minutes instead of 900 seconds

- **File:** `app/auth.py`, line 50 (`create_access_token`)
- **Bug:** `lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`.
  `ACCESS_TOKEN_EXPIRE_MINUTES` is 15, so the lifetime was `timedelta(minutes=900)`
  = 15 **hours**. The spec requires access tokens to expire in exactly 900 seconds,
  and the JWT `exp − iat` was observably 54000 instead of 900.
- **Fix:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` → `exp − iat == 900`.

## 2. Logout never actually revoked the token (checked `sub` against a set of `jti`s)

- **File:** `app/auth.py`, line 97 (`get_token_payload`)
- **Bug:** `revoke_access_token` stores the token's `jti` in `_revoked_tokens`, but the
  request path checked `payload.get("sub") in _revoked_tokens`. A user id never equals
  a jti hex string, so the check never matched: after `POST /auth/logout`, the presented
  access token kept working (spec: subsequent use → 401).
- **Fix:** check `payload.get("jti") in _revoked_tokens`.

## 3. Refresh tokens were reusable (not single-use)

- **File:** `app/routers/auth.py` (`refresh`), with helper added in `app/auth.py`
- **Bug:** `POST /auth/refresh` issued a new pair but never invalidated the presented
  refresh token. The same refresh token could be redeemed indefinitely; the spec
  requires single-use (reuse → 401).
- **Fix:** added `consume_refresh_token()` in `app/auth.py` which atomically (under a
  lock) checks the refresh token's `jti` against the revocation set and records it.
  `refresh` calls it after validating the token type; reuse now returns 401.

## 4. Duplicate registration returned the existing user instead of 409 USERNAME_TAKEN

- **File:** `app/routers/auth.py`, lines 37–43 (`register`)
- **Bug:** when the username already existed in the org, the endpoint silently returned
  the existing user's data with a success status. Spec: duplicate username within the
  org → `409 USERNAME_TAKEN`. (This also leaked account presence/role to anyone.)
- **Fix:** raise `AppError(409, "USERNAME_TAKEN", ...)`. Also wrapped the org/user
  commits in `IntegrityError` handlers so two *concurrent* registrations of the same
  username produce 409 (not a 500), and a concurrent org creation joins as member
  instead of crashing.

## 5. Timezone offsets were dropped instead of converted to UTC

- **File:** `app/timeutils.py`, lines 12–13 (`parse_input_datetime`)
- **Bug:** `dt.replace(tzinfo=None)` throws the offset away, so `10:00+06:00` was stored
  as `10:00 UTC` instead of `04:00 UTC`. Every downstream comparison (future check,
  overlap, quota window, availability date, report ranges) and every response datetime
  was wrong for any client that sent an offset.
- **Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)` — normalize to UTC first,
  then store naive-UTC as the rest of the app expects.

## 6. 300-second grace window for bookings starting in the past

- **File:** `app/routers/bookings.py`, line 86 (`create_booking`)
- **Bug:** `if start <= now - timedelta(seconds=300)` allowed a booking starting up to
  5 minutes in the past. Spec: `start_time` must be *strictly in the future, no grace
  window*.
- **Fix:** `if start <= now: → 400 INVALID_BOOKING_WINDOW`.

## 7. Minimum duration never enforced (0-hour and negative bookings accepted)

- **File:** `app/routers/bookings.py`, lines 89–94 (`create_booking`)
- **Bug:** only `duration > MAX` was rejected. `end == start` (0 hours) passed and
  created a zero-price booking, and `end` a whole number of hours *before* `start`
  produced a **negative** price. Spec: duration whole hours, min 1, max 8, and
  `end_time` strictly after `start_time`.
- **Fix:** `if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS`
  → 400. (Also wrapped datetime parsing in try/except so malformed datetime strings
  return 400 instead of an unhandled 500.)

## 8. Overlap check rejected back-to-back bookings

- **File:** `app/routers/bookings.py`, line 50 (`_has_conflict`)
- **Bug:** `b.start_time <= end and start <= b.end_time` uses inclusive comparisons.
  A booking 12:00–13:00 conflicted with an existing 10:00–12:00, so legal back-to-back
  bookings returned 409. Spec: overlap iff `existing.start < new.end AND new.start <
  existing.end` (strict).
- **Fix:** strict `<` on both comparisons.

## 9. Double-booking and quota races (check-then-act with no synchronization)

- **File:** `app/routers/bookings.py` (`create_booking`, `_has_conflict`, `_check_quota`)
- **Bug:** the conflict check, quota check, and INSERT were not atomic; the deliberate
  `time.sleep` calls (`_pricing_warmup`, `_quota_audit`) widen the window so two
  concurrent requests for the same slot both passed `_has_conflict` and both committed
  — two confirmed overlapping bookings (and 4+ bookings within the quota window).
  Spec rules 3 and 4 must hold under concurrent requests.
- **Fix:** added a module-level `threading.Lock` (`_booking_lock`); the conflict check,
  quota check, insert/commit, stats update and cache invalidation now run as one
  critical section. Endpoints are sync (FastAPI threadpool) and there is a single
  worker, so the lock fully serializes booking mutations: exactly one of N concurrent
  same-slot requests succeeds (others 409 ROOM_CONFLICT), and the quota cap holds.

## 10. Refund tiers wrong at both boundaries (≥48h paid 50%, <24h paid 50% instead of 0%)

- **File:** `app/routers/bookings.py`, lines 199–206 (`cancel_booking`)
- **Bug (a):** `notice_hours = int(notice.total_seconds() // 3600)` floors to whole
  hours, and `if notice_hours > 48` requires *more than* 48 whole hours — so any notice
  in `[48h, 49h)` was floored to 48 and paid 50% instead of the specified 100%
  (spec: notice ≥ 48h → 100%).
- **Bug (b):** the `else` branch (notice < 24h) set `refund_percent = 50` instead of 0 —
  last-minute cancellations were refunded half instead of nothing.
- **Fix:** compare the timedelta directly:
  `notice >= 48h → 100`, `elif notice >= 24h → 50`, `else → 0`.

## 11. Refund amount: banker's rounding in the response, truncation in the ledger (mismatch)

- **Files:** `app/routers/bookings.py` line 208, `app/services/refunds.py` lines 14–17
- **Bug:** the cancel response used Python `round()` (banker's rounding: 50.5 → 50),
  while `log_refund` computed the stored amount independently via floats and `int()`
  truncation. For an odd price at 50% (e.g. 333 → 166.5¢) the response said 166 and
  the RefundLog stored 166 (truncated) or they disagreed (e.g. 103 → 52 vs 51).
  Spec: half-cents round **up**, and the response amount must **equal** the RefundLog
  amount.
- **Fix:** single source of truth in `log_refund` with exact integer half-up rounding:
  `amount_cents = (price_cents * percent + 50) // 100`; the route returns
  `entry.amount_cents` from the created RefundLog row.

## 12. Concurrent cancels produced two refunds

- **File:** `app/routers/bookings.py` (`cancel_booking`)
- **Bug:** the status check and the status update were separated by `log_refund` and a
  deliberate `_settlement_pause()` sleep, so two concurrent cancels of the same booking
  both saw `status == "confirmed"`, both logged a RefundLog, and both returned 200.
  Spec: exactly one RefundLog; concurrent cancels → one 200, the rest 409
  ALREADY_CANCELLED.
- **Fix:** the entire cancel critical section (fetch → status check → refund log →
  status update/commit) runs under the same `_booking_lock`.

## 13. GET /bookings/{id} returned `created_at` as `start_time`

- **File:** `app/routers/bookings.py`, line 166 (`get_booking`)
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` overwrote the correct
  serialized start time with the creation timestamp — the single-booking view showed a
  wrong start_time.
- **Fix:** removed the line.

## 14. Members could read other members' bookings

- **File:** `app/routers/bookings.py` (`get_booking`)
- **Bug:** the detail endpoint only scoped by org, not by owner. Any member could fetch
  another member's booking by id (cancel had the owner check; read did not). Spec rule
  10: members read only their own bookings; another member's id → 404 BOOKING_NOT_FOUND.
- **Fix:** added the same owner-or-admin check used by cancel:
  `if user.role != "admin" and booking.user_id != user.id → 404`.

## 15. Pagination: descending order, off-by-one page offset, hard-coded page size

- **File:** `app/routers/bookings.py`, lines 136–140 (`list_bookings`)
- **Bug:** three deviations from rule 11:
  1. `order_by(Booking.start_time.desc(), ...)` — spec requires **ascending** start_time;
  2. `.offset(page * limit)` — page 1 skipped the first `limit` items (pages skipped
     items and page 1 was actually page 2);
  3. `.limit(10)` — the `limit` query param was ignored (always 10 per page), so
     e.g. `limit=5` pages skipped/repeated items.
- **Fix:** `order_by(start_time.asc(), id.asc()).offset((page - 1) * limit).limit(limit)`.

## 16. Stale caches: reports not invalidated on create, availability not invalidated on cancel

- **File:** `app/routers/bookings.py` (`create_booking`, `cancel_booking`) with
  `app/cache.py`
- **Bug:** `create_booking` invalidated only the availability cache, so a cached
  `/admin/usage-report` kept serving old counts after a new booking (spec 12: must
  reflect current state immediately). Symmetrically, `cancel_booking` invalidated only
  the report cache, so `/rooms/{id}/availability` kept showing a cancelled booking as
  busy (spec 13).
- **Fix:** create now also calls `cache.invalidate_report(org_id)`; cancel now also
  calls `cache.invalidate_availability(room_id, start_date)`.

## 17. Room stats lost updates under concurrency

- **File:** `app/services/stats.py`
- **Bug:** `record_create`/`record_cancel` did read → sleep → write on a shared dict.
  Two concurrent bookings both read count=N and both wrote N+1, permanently
  desynchronizing `/rooms/{id}/stats` from the real bookings (spec 14: always
  consistent, including after concurrent bursts).
- **Fix:** the read-modify-write is now atomic under a `threading.Lock` (the pause was
  moved outside the critical section).

## 18. Rate limiter lost updates under concurrency

- **File:** `app/services/ratelimit.py`
- **Bug:** same read → sleep → write pattern on the per-user bucket list: concurrent
  requests each appended to their own copy and the last write won, undercounting
  requests so a user could exceed 20 requests/60s without ever seeing 429 (spec 5:
  must hold under concurrent requests).
- **Fix:** trim + append + count now execute atomically under a `threading.Lock`.

## 19. Duplicate reference codes under concurrency

- **File:** `app/services/reference.py`
- **Bug:** `next_reference_code` read the counter, slept 0.12s (`_format_pause`), then
  wrote back `current + 1`. Concurrent creations read the same counter value and got
  identical reference codes (spec 7: unique, including under concurrent creation).
- **Fix:** read-and-increment is atomic under a `threading.Lock`; the formatting pause
  happens outside the critical section.

## 20. Deadlock between booking-created and booking-cancelled notifications

- **File:** `app/services/notifications.py`
- **Bug:** classic lock-order inversion: `notify_created` acquired `_email_lock` then
  `_audit_lock`, while `notify_cancelled` acquired `_audit_lock` then `_email_lock`.
  A concurrent create + cancel could each grab their first lock and wait forever on the
  other — both requests hang, and every subsequent create/cancel piles up behind the
  held locks (violates spec 16, liveness).
- **Fix:** both functions acquire the locks in the same global order
  (email → audit), which makes deadlock impossible.

## 21. CSV export leaked other organizations' bookings

- **Files:** `app/services/export.py` lines 48–52, `app/routers/admin.py` (`export`)
- **Bug:** with `include_all=true&room_id=<id>`, `generate_export` called
  `fetch_bookings_raw`, which filters **only by room id with no org check**. An admin
  of org A could pass a room id belonging to org B and download org B's entire booking
  history — a cross-tenant data leak (spec 9: cross-org IDs behave as non-existent → 404,
  on every code path).
- **Fix:** the export always goes through the org-scoped query
  (`_fetch_scoped(db, org_id, ...)`), and the router returns `404 ROOM_NOT_FOUND` when
  the requested `room_id` doesn't exist in the caller's org.

## 22. Usage-report cache not invalidated when a room is created

- **File:** `app/routers/rooms.py` (`create_room`), with `app/routers/admin.py` lines 25–27
- **Bug:** `/admin/usage-report` caches results per `(org, from, to)` and only booking
  create/cancel invalidated that cache. Creating a **room** did not, so a previously
  cached report for the same range kept being served without the new room. Spec rule 12:
  the report must list every room in the org **including zero-booking rooms** and
  "must reflect the current state immediately."
- **Fix:** `create_room` now calls `cache.invalidate_report(admin.org_id)` after commit.

## 23. Admin export filtered by caller's own user_id when `include_all=False`

- **Files:** `app/services/export.py` lines 36–40; `app/routers/admin.py` line 52
- **Bug:** `generate_export` passed `admin.id` as `user_id` and filtered bookings by it when `include_all=False`. An admin calling `/admin/export` without `include_all=true` would only see their own bookings instead of all bookings in their organization. The `include_all` parameter was intended to control scope (e.g., all rooms), not to toggle between "all users" and "just me."
- **Fix:** Removed `user_id` filtering from the export query. The export now always returns all org-scoped bookings (optionally filtered by `room_id`).

## 24. Malformed JWT subjects could return 500 instead of 401

- **Files:** `app/auth.py`, line 117; `app/routers/auth.py`, line 94
- **Bug:** `int(payload["sub"])` raised `ValueError` if a valid-signature access or refresh token contained a non-integer `sub` (e.g., `"abc"`). Tokens missing required claims, or carrying a malformed `jti`, could also bypass part of validation or raise raw exceptions. The spec requires missing/invalid/expired/blacklisted tokens to return 401, not 500.
- **Fix:** Added required-claim validation in `decode_token` plus shared helpers that validate `sub` and `jti` before using them. Both access-token user lookup and refresh-token rotation now return `401 UNAUTHORIZED` for malformed subjects or token IDs.

## 25. Reference-code counter reset after restart

- **Files:** `app/services/reference.py`; `app/routers/bookings.py`
- **Bug:** reference codes were generated from an in-memory counter initialized to `1000` on every process start. With a persistent SQLite database, the next booking after a restart could reuse an existing code such as `CW-001000`, violating the uniqueness rule and potentially returning a database 500.
- **Fix:** `next_reference_code()` now receives the active database session, seeds the counter from the highest existing `CW-<number>` code, and then issues the next value under the existing lock.

## 26. Room stats reset after server restart

- **Files:** `app/services/stats.py`; `app/routers/rooms.py`
- **Bug:** `/rooms/{id}/stats` returned values from an in-memory `_stats` dictionary. Bookings are persisted in SQLite, but `_stats` resets on process restart. After restart, a room with confirmed bookings could report `0` bookings and `0` revenue; cancelling one of those persisted bookings could also drive the in-memory revenue negative. Spec rule 14 says stats must always equal the values derivable from bookings.
- **Fix:** The stats endpoint now derives the current count and revenue directly from confirmed `Booking` rows in the database. The incremental in-memory helper is still updated for compatibility, but the API response is database-backed and restart-safe.

## 27. Logout and used refresh-token revocation were lost after restart

- **Files:** `app/models.py`; `app/auth.py`; `app/routers/auth.py`
- **Bug:** revoked access-token JTIs and consumed refresh-token JTIs lived only in the process-local `_revoked_tokens` set. Restarting the server cleared the set, so a logged-out access token worked again and a previously used refresh token could be reused. Spec rule 8 requires logout to invalidate the presented access token for all further use and refresh tokens to be single-use.
- **Fix:** Added a `revoked_tokens` table and store revoked/consumed JTIs with their expiration. Access-token validation and refresh-token rotation now check both the in-memory set and the persisted table, so invalidation survives process restarts.

## 28. Report and availability caches could store stale data after invalidation

- **File:** `app/cache.py`, with callers in `app/routers/admin.py` and `app/routers/rooms.py`
- **Bug:** cache reads, writes, and invalidations had no synchronization or version check. A slow report/availability request could compute old data, a booking mutation could invalidate the cache, and then the slow request could write the old result back into the cache after invalidation. Later reads would see stale data even though rules 12 and 13 require immediate freshness.
- **Fix:** Added cache locks and per-key/per-org generation counters. Routes capture the generation before computing uncached data; `set_*` only writes if no invalidation has happened since that computation began.

---

## Verification

Verified with the project smoke test, focused regression tests, and a 30-test
live HTTP contract suite (`tests/test_contract_live.py`) that starts uvicorn on
a fresh SQLite database and exercises all business rules, including concurrent
double-booking, quota enforcement, rate limiting, concurrent cancellation,
stats consistency, cache freshness, multi-tenancy, export scoping, and liveness.
Restart regressions additionally verify database-backed stats and persisted token
revocation across process restarts. Full local run: `39 passed`.
