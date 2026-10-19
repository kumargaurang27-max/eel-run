// Vercel serverless function: per-level global leaderboards on Upstash Redis (REST).
// Env vars (set by the Upstash integration): UPSTASH_REDIS_REST_URL / _TOKEN (or KV_REST_API_URL / _TOKEN)
const URL_ = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
const TOKEN = process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;

async function redis(cmds) {
  const r = await fetch(URL_ + '/pipeline', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
    body: JSON.stringify(cmds),
  });
  return r.json();
}

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  if (!URL_ || !TOKEN) return res.status(200).json({ scores: [], note: 'database not configured' });
  try {
    const b = req.method === 'POST' ? (typeof req.body === 'string' ? JSON.parse(req.body) : req.body || {}) : {};
    const lvl = Math.max(1, Math.min(8, parseInt(req.method === 'POST' ? b.lvl : req.query.lvl, 10) || 1));
    const KEY = 'hypercar:L' + lvl;
    if (req.method === 'POST') {
      const et = Number(b.et), trap = Number(b.trap);
      const name = String(b.name || 'Driver').replace(/[^\w \-.]/g, '').trim().slice(0, 12) || 'Driver';
      // Sanity limits only; a determined cheater can still fake a plausible time.
      if (!(et >= 4 && et <= 80) || !(trap >= 60 && trap <= 340)) return res.status(400).json({ error: 'invalid run' });
      const member = JSON.stringify({ name, et, trap, tc: b.tc !== false, id: Date.now().toString(36) + Math.random().toString(36).slice(2, 5) });
      await redis([['ZADD', KEY, et, member], ['ZREMRANGEBYRANK', KEY, 200, -1]]);
      return res.status(200).json({ ok: true });
    }
    const out = await redis([['ZRANGE', KEY, 0, 9]]);
    const scores = (out[0].result || []).map((m) => { try { return JSON.parse(m); } catch (e) { return null; } }).filter(Boolean);
    return res.status(200).json({ scores });
  } catch (e) {
    return res.status(200).json({ scores: [] });
  }
};
