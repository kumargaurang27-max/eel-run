"""Vercel Python function: server-authoritative leaderboard.

The browser never sends a finishing time. It sends the player's throttle/boost
input log; this function replays it through the same physics model the game
uses and stores the time it computes itself. Faked times are therefore not
possible without also faking a physically valid input log.

Env vars (set by the Upstash integration):
  UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN  (or KV_REST_API_URL / KV_REST_API_TOKEN)
"""
import json
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

URL = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")

# ---- physics constants (must match the game) --------------------------------
M, G, PMAX, FMAX = 1900.0, 9.81, 750e3, 30e3
CD, AF, CL, CRR = 0.28, 2.0, 0.18, 0.012
DT = 1.0 / 240.0
MAX_STEPS = 240 * 120  # 2 minutes of simulated time

# level -> (distance m, road grip, headwind m/s, traction control allowed)
LEVELS = {
    1: (201.2, 1.25, 0, True),
    2: (402.3, 1.25, 0, True),
    3: (402.3, 1.20, 0, True),
    4: (402.3, 0.80, 0, False),
    5: (201.2, 0.50, 0, False),
    6: (804.6, 1.15, 6, False),
    7: (402.3, 0.55, 4, False),
    8: (804.6, 1.00, 10, False),
}


def simulate(dist, mu, wind, tc, log):
    """Replay an input log. log = [[step, kind], ...] with kind 1=throttle down,
    0=throttle up, 2=thruster. Returns (elapsed time s, trap speed km/h) or None."""
    v = x = t = thr = spin = 0.0
    boost, boost_on, boost_used, keys = 1.5, False, False, False
    i, n, step = 0, len(log), 0
    while x < dist:
        if step > MAX_STEPS:
            return None
        while i < n and log[i][0] <= step:
            kind = log[i][1]
            if kind == 1:
                keys = True
            elif kind == 0:
                keys = False
            elif kind == 2 and not boost_used:
                boost_used = boost_on = True
            i += 1
        thr = min(1.0, max(0.0, thr + (3 if keys else -6) * DT))
        q = 0.5 * 1.2 * v * v
        qa = 0.5 * 1.2 * (v + wind) * (v + wind)
        fm = min(FMAX, PMAX / max(v, 1.0))
        ft = mu * (M * G + q * AF * CL)
        want = thr * fm
        if tc and want > ft * 0.97:
            want = ft * 0.97
        if want > ft:
            force = ft * 0.8
            spin = min(1.0, spin + DT * 4)
        else:
            force = want
            spin = max(0.0, spin - DT * 5)
        if boost_on:
            force += 15e3
            boost -= DT
            if boost <= 0:
                boost_on = False
        force -= qa * CD * AF + (CRR * M * G if v > 0 else 0.0)
        v = max(0.0, v + force / M * DT)
        x += v * DT
        t += DT
        step += 1
    et = t - (x - dist) / max(v, 1.0)
    return et, v * 3.6


def valid_log(log):
    if not isinstance(log, list) or len(log) > 600:
        return False
    last = -1
    for e in log:
        if not (isinstance(e, list) and len(e) == 2):
            return False
        a, b = e
        if not (isinstance(a, int) and isinstance(b, int)) or isinstance(a, bool) or isinstance(b, bool):
            return False
        if a < last or a < 0 or a > MAX_STEPS or b not in (0, 1, 2):
            return False
        last = a
    return True


def redis(cmds):
    req = Request(
        URL + "/pipeline",
        data=json.dumps(cmds).encode(),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=8) as r:
        return json.loads(r.read())


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        try:
            lvl = max(1, min(8, int(q.get("lvl", ["1"])[0])))
        except ValueError:
            lvl = 1
        if not (URL and TOKEN):
            return self._send(200, {"scores": [], "note": "database not configured"})
        try:
            rows = redis([["ZRANGE", "hypercar:L%d" % lvl, 0, 9]])[0].get("result") or []
            scores = []
            for m in rows:
                try:
                    scores.append(json.loads(m))
                except ValueError:
                    pass
            self._send(200, {"scores": scores})
        except Exception:
            self._send(200, {"scores": []})

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size > 20000:
                return self._send(413, {"error": "too large"})
            b = json.loads(self.rfile.read(size) or b"{}")
            lvl = int(b.get("lvl", 0))
            if lvl not in LEVELS:
                return self._send(400, {"error": "bad level"})
            dist, mu, wind, tc_ok = LEVELS[lvl]
            tc = bool(b.get("tc")) and tc_ok  # server decides whether TC was allowed
            log = b.get("log")
            if not valid_log(log):
                return self._send(400, {"error": "bad input log"})
            res = simulate(dist, mu, wind, tc, log)
            if res is None:
                return self._send(400, {"error": "run did not finish"})
            et, trap = res
            name = re.sub(r"[^\w \-.]", "", str(b.get("name") or "Driver")).strip()[:12] or "Driver"
            out = {"ok": True, "et": round(et, 3), "trap": round(trap, 1), "tc": tc, "stored": False}
            if URL and TOKEN:
                ip = (self.headers.get("x-forwarded-for") or "ip").split(",")[0].strip()
                rl = redis([["INCR", "rl:" + ip], ["EXPIRE", "rl:" + ip, 60]])[0].get("result", 0)
                if rl > 8:
                    return self._send(429, {"error": "too many submissions, wait a minute"})
                key = "hypercar:L%d" % lvl
                member = json.dumps({"name": name, "et": out["et"], "trap": out["trap"], "tc": tc, "id": secrets.token_hex(3)})
                r = redis([["ZADD", key, out["et"], member], ["ZREMRANGEBYRANK", key, 200, -1], ["ZRANK", key, member]])
                rank = r[2].get("result")
                out["stored"] = True
                out["rank"] = (rank + 1) if isinstance(rank, int) else None
            self._send(200, out)
        except Exception:
            self._send(400, {"error": "invalid request"})
