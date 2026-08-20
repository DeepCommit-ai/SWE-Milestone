/**
 * jest worker IPC guard.
 *
 * jest-runner configures its worker pool with `serialization: 'json'`, so a
 * result containing a cycle makes the worker's own process.send() throw
 * TypeError: Converting circular structure to JSON.  The worker dies, jest
 * retries 4x, and the whole suite is reported absent -- its tests silently
 * become non-achievements even though they ran.
 *
 * Loaded with `node --require`, this runs inside every worker before jest
 * does.  It only intervenes when the payload is genuinely unserializable, and
 * only replaces the offending references; outcomes, names and messages are
 * untouched.  In the parent (no process.send) it is a no-op.
 */
if (typeof process.send === "function") {
  const original = process.send.bind(process);

  const sanitize = (value, seen) => {
    if (value === null || typeof value !== "object") return value;
    if (seen.has(value)) return "[Circular]";
    seen.add(value);
    const out = Array.isArray(value) ? [] : {};
    for (const key of Object.keys(value)) {
      try {
        out[key] = sanitize(value[key], seen);
      } catch (err) {
        out[key] = "[Unserializable]";
      }
    }
    seen.delete(value);
    return out;
  };

  process.send = function (message, ...rest) {
    try {
      JSON.stringify(message);
    } catch (err) {
      try {
        message = sanitize(message, new WeakSet());
        process.stderr.write("[jest-ipc-guard] sanitized an unserializable worker message\n");
      } catch (inner) {
        // fall through and let jest see the original failure
      }
    }
    return original(message, ...rest);
  };
}
