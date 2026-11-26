// Date calculation only: this process never installs a timer or invokes a workflow.
const fs = require('node:fs');
const { Cron } = require('./vendor/croner.cjs');
let cron;
try {
  const buffer = Buffer.alloc(8193);
  let length = 0;
  while (length < buffer.length) {
    const read = fs.readSync(0, buffer, length, buffer.length - length, null);
    if (!read) break;
    length += read;
  }
  if (length > 8192) throw Error('Calendar request exceeds 8 KiB.');
  const input = JSON.parse(buffer.subarray(0, length).toString('utf8'));
  const fields = ['expression', 'timezone', 'after', 'count'];
  if (!input || Array.isArray(input) || Object.keys(input).length !== fields.length ||
      !fields.every(key => Object.hasOwn(input, key))) throw Error('Choose expression, timezone, after and count.');
  if (typeof input.expression !== 'string' || !input.expression.trim() || input.expression.length > 256)
    throw Error('Choose a cron expression of at most 256 characters.');
  if (typeof input.timezone !== 'string' || !input.timezone || input.timezone.length > 100)
    throw Error('Choose an IANA time zone.');
  new Intl.DateTimeFormat('en-US', { timeZone: input.timezone });
  if (!Number.isFinite(input.after) || input.after < 0 || input.after > 4102444800)
    throw Error('Choose a reference date between 1970 and 2100.');
  if (!Number.isInteger(input.count) || input.count < 1 || input.count > 8)
    throw Error('Preview one to eight occurrences.');
  cron = new Cron(input.expression, { timezone: input.timezone, paused: true });
  let cursor = new Date(input.after * 1000);
  const occurrences = [];
  for (let i = 0; i < input.count; i++) {
    const next = cron.nextRun(cursor);
    if (!next) break;
    if (!(next.getTime() > cursor.getTime()))
      throw Error('The calendar engine did not produce a later occurrence.');
    occurrences.push(next.getTime() / 1000);
    cursor = next;
  }
  if (!occurrences.length) throw Error('Cron expression produces no future occurrences.');
  process.stdout.write(JSON.stringify({ ok: true, engine: 'Croner 11.0.1', expression: input.expression,
    timezone: input.timezone, occurrences }));
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(error.message || error).slice(0, 500) }));
} finally {
  if (cron) cron.stop();
}
