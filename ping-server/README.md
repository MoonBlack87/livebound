# Livebound ping receiver

This is the receiver for Livebound's optional usage ping. When a user has
given consent, Livebound posts one stable installation UUID to `/ping` at each
start as `{"id":"<uuid>"}`. Distinct UUIDs show how many installations exist;
the count for each UUID shows how often it starts.

With the ping off, Livebound sends nothing. Before the first opt-in, it creates
no UUID. Turning the ping off after an opt-in stops future pings and retains the
UUID locally.

The receiver accepts only that JSON field and a well-formed UUID. It forwards
the UUID to the configured analytics endpoint and returns `204 No Content`.
The image published from this project is built from exactly `main.go`,
`main_test.go`, `go.mod`, and `Dockerfile` in this directory.

`UMAMI_ENDPOINT` and `UMAMI_WEBSITE_ID` are required when the container starts.
The maintained deployment supplies an endpoint separately; this published copy
intentionally has no default endpoint, so starting it for inspection cannot
send an event to that deployment's analytics service.
