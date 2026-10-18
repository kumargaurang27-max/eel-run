// Vercel serverless function: global leaderboard backed by Upstash Redis (REST).
// Env vars (set automatically by the Upstash integration, or add manually):
//   UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN  (or KV_REST_API_URL / KV_REST_API_TOKEN)
const URL_ = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
const TOKEN = process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;
const KEY = 'hypercar:quarter-mile';

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
    if (req.method === 'POST') {
      const b = typeof req.body === 'string' ? JSON.parse(req.body) : req.body || {};
      const et = Number(b.et), trap = Number(b.trap);
      const name = String(b.name || 'Driver').replace(/[^\w \-.]/g, '').trim().slice(0, 12) || 'Driver';
      // Sanity limits: a 750 kW, 1.9 t car cannot physically beat ~7 s or exceed ~330 km/h trap.
      if (!(et >= 7 && et <= 40) || !(trap >= 100 && trap <= 330)) return res.status(400).json({ error: 'invalid run' });
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
