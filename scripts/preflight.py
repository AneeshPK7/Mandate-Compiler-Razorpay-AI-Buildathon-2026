#!/usr/bin/env python3
"""Pre-recording check: run the entire demo path against a real server.

    python scripts/preflight.py

Every step of the video is exercised end to end — seed, paced SSE stream,
mid-stream revocation, chain verification, all three tamper attacks, and reset
— then the database is left clean and ready to record against.

It runs against a real uvicorn process on a scratch database, deliberately.
Starlette's TestClient buffers streaming responses, so a mid-stream revoke
cannot be exercised through it: the server would finish the whole batch before
the revoke landed. The only way to know the demo works is to do what the demo
does.

Exit code 0 means go.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

DB = ROOT / "preflight.db"
MANDATE = "mandate-demo-001"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    mark = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[status]
    print(f"[{mark}] {name}" + (f"\n           {detail}" if detail else ""))


def check(name: str, fn, soft: bool = False) -> bool:
    """Run one check; a raised exception is a failure, never a crash.

    `soft` downgrades a failure to a warning — for things that degrade the demo
    without breaking it.
    """
    bad = WARN if soft else FAIL
    try:
        detail = fn()
        record(PASS, name, detail or "")
        return True
    except AssertionError as exc:
        record(bad, name, str(exc))
        return soft
    except Exception as exc:  # noqa: BLE001
        record(bad, name, f"{type(exc).__name__}: {exc}")
        return soft


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def server(port: int):
    """Boot uvicorn against a scratch database, with the tamper demo enabled."""
    env = {
        **os.environ,
        "MANDATE_DEMO_TAMPER": "1",
        "MANDATE_DB_URL": f"sqlite:///{DB}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{proc.stdout.read()}")
            try:
                httpx.get(f"{base}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("server did not come up within 15s")
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


def stream_and_revoke(base: str, revoke_after_seconds: float, delay_ms: int, limit: int):
    """Consume the SSE stream, firing a revoke partway through from another thread."""
    steps, summary, revoked_at = [], None, [None]
    t0 = time.monotonic()

    def revoke():
        time.sleep(revoke_after_seconds)
        httpx.post(f"{base}/mandates/{MANDATE}/revoke", timeout=10)
        revoked_at[0] = time.monotonic() - t0

    threading.Thread(target=revoke, daemon=True).start()

    with httpx.Client(timeout=120) as client:
        url = f"{base}/simulate/{MANDATE}?delay_ms={delay_ms}&limit={limit}"
        with client.stream("GET", url) as response:
            event = None
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    payload = json.loads(line[6:])
                    if event == "step":
                        payload["_t"] = time.monotonic() - t0
                        steps.append(payload)
                    elif event == "summary":
                        summary = payload
    return steps, summary, revoked_at[0]


def main() -> int:
    print("Pre-flight check — running the demo end to end\n")
    DB.unlink(missing_ok=True)
    port = free_port()

    # --- things that must be true before the server even starts -------------
    def deps():
        import anthropic  # noqa: F401
        import nacl  # noqa: F401
        import sqlmodel  # noqa: F401
        return None

    check("dependencies importable", deps)

    def signing_key():
        from app.signing import public_key_hex

        key = public_key_hex()
        assert len(bytes.fromhex(key)) == 32, "public key is not 32 bytes"
        return f"public key {key[:16]}…"

    check("Ed25519 signing key available", signing_key)

    def compiler_key():
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY is set"
        raise AssertionError(
            "ANTHROPIC_API_KEY not set — the compile step will report "
            "'unavailable'. Everything else still works."
        )

    # Soft: without a key the compile step reports "unavailable", which is
    # handled behaviour, not a broken demo.
    check("compiler credentials", compiler_key, soft=True)

    ok = True
    with server(port) as base:
        def dashboard():
            r = httpx.get(f"{base}/", timeout=10)
            assert r.status_code == 200, f"dashboard returned {r.status_code}"
            assert "Mandate Compiler" in r.text, "dashboard content missing"
            import re

            refs = re.findall(r'(?:src|href)\s*=\s*["\'](?!#)([^"\']+)', r.text)
            remote = [u for u in refs if u.startswith(("http:", "https:", "//"))]
            assert not remote, f"page loads external resources: {remote}"
            return f"{len(r.text) // 1024} KB, no external resources"

        ok &= check("dashboard loads, fully self-contained", dashboard)

        def tamper_on():
            h = httpx.get(f"{base}/health", timeout=10).json()
            assert h.get("demo_tamper") is True, (
                "tamper demo is OFF — start the server with MANDATE_DEMO_TAMPER=1"
            )
            return "MANDATE_DEMO_TAMPER=1 active"

        ok &= check("tamper demo enabled", tamper_on)

        def seed():
            body = httpx.post(f"{base}/demo/seed", timeout=60).json()
            assert body["transactions_loaded"] >= 250, body
            assert body["mandate"]["signature_valid"] is True, "mandate signature invalid"
            return f"{body['transactions_loaded']} transactions, signature valid"

        ok &= check("seed loads the batch", seed)

        # --- the centrepiece: paced stream + mid-stream revoke --------------
        state = {}

        def revocation():
            steps, summary, revoked_at = stream_and_revoke(
                base, revoke_after_seconds=1.5, delay_ms=40, limit=150
            )
            state["steps"] = steps
            assert steps, "no steps streamed"
            assert revoked_at is not None, "revoke never fired"

            elapsed = steps[-1]["_t"]
            assert elapsed > 2.0, (
                f"stream finished in {elapsed:.1f}s — too fast to hit revoke on "
                "camera; raise delay_ms"
            )

            before = [s for s in steps if s["_t"] < revoked_at]
            after = [s for s in steps if s["_t"] > revoked_at + 0.15]
            assert before, "revoke fired before any decisions"
            assert after, "revoke fired after the stream ended — lower revoke timing"

            leaked = [s for s in after if s["reason_code"] != "MANDATE_REVOKED"]
            assert not leaked, (
                f"{len(leaked)} transactions were NOT blocked after revocation "
                f"(first: {leaked[0]['transaction_id']}) — this is the demo's "
                "central claim"
            )
            assert summary and summary["chain_valid"], "chain invalid after run"
            return (
                f"{len(steps)} steps over {elapsed:.1f}s; revoked at "
                f"{revoked_at:.1f}s; {len(before)} before, {len(after)} after, "
                "all MANDATE_REVOKED"
            )

        ok &= check("mid-stream revocation blocks everything after", revocation)

        def chain_after_run():
            v = httpx.get(f"{base}/audit/verify", timeout=30).json()
            assert v["valid"], v["reason"]
            log = httpx.get(f"{base}/audit/chain?limit=1000", timeout=30).json()
            lifecycle = [e for e in log if e["rule_triggered"] == "mandate_lifecycle"]
            kinds = [e["reason_code"] for e in lifecycle]
            assert "MANDATE_CREATED" in kinds, "creation not recorded in chain"
            assert "MANDATE_REVOKED" in kinds, "revocation not recorded in chain"
            return f"{v['entries_checked']} entries valid; lifecycle: {', '.join(kinds)}"

        ok &= check("audit chain valid and records lifecycle", chain_after_run)

        # --- each attack, on a clean chain ----------------------------------
        expected = {
            "flip": "content altered",
            "delete": "sequence gap",
            "forge": "broken link",
        }
        for attack, signature in expected.items():
            def attempt(attack=attack, signature=signature):
                httpx.post(f"{base}/demo/reset", timeout=30)
                httpx.post(f"{base}/demo/seed", timeout=60)
                httpx.get(
                    f"{base}/simulate/{MANDATE}?delay_ms=0&limit=40", timeout=120
                )
                pre = httpx.get(f"{base}/audit/verify", timeout=30).json()
                assert pre["valid"], "chain was already broken before the attack"

                t = httpx.post(f"{base}/demo/tamper/{attack}", timeout=30)
                assert t.status_code == 200, f"attack failed: {t.text[:200]}"
                body = t.json()
                assert body["detected"], f"{attack} was NOT detected — this is a bug"
                assert signature in body["reason"], (
                    f"expected '{signature}', got '{body['reason']}'"
                )
                return f"seq {body['broken_at_seq']} — {body['reason'][:64]}…"

            ok &= check(f"tamper '{attack}' is caught", attempt)

        def reset():
            body = httpx.post(f"{base}/demo/reset", timeout=30).json()
            assert body["reset"] is True
            v = httpx.get(f"{base}/audit/verify", timeout=30).json()
            assert v["entries_checked"] == 0, "reset left entries behind"
            return "database clean"

        ok &= check("reset returns a clean chain", reset)

    DB.unlink(missing_ok=True)

    # --- verdict ------------------------------------------------------------
    failures = [r for r in results if r[0] == FAIL]
    warnings = [r for r in results if r[0] == WARN]

    print("\n" + "─" * 64)
    if failures:
        print(f"NO-GO — {len(failures)} check(s) failed:")
        for _, name, detail in failures:
            print(f"  · {name}: {detail}")
        return 1

    print("GO — every step of the demo works end to end.")
    if warnings:
        print(f"\n{len(warnings)} warning(s), none blocking:")
        for _, name, detail in warnings:
            print(f"  · {name}: {detail}")
    print(
        "\nRecord with:\n"
        "  MANDATE_DEMO_TAMPER=1 uvicorn app.main:app\n"
        "See DEMO.md for the run of show."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
