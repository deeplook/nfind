'use strict';
// Node.js worker for pfind. Reads a JSON request {code, paths} on stdin, defines
// the generated filterPaths(paths) function, runs it, and writes a JSON response
// {ok, results|error} on stdout. The container provides the isolation; this worker
// only enforces the stdin/stdout protocol.
const fs = require('fs');

function run() {
  let request;
  try {
    request = JSON.parse(fs.readFileSync(0, 'utf8'));
  } catch (e) {
    return { ok: false, error: 'invalid request: ' + e.message };
  }
  const code = request && request.code;
  const paths = request && request.paths;
  if (typeof code !== 'string' || !Array.isArray(paths)) {
    return { ok: false, error: 'request must contain code and a list of paths' };
  }

  // Silence stdout writes from generated code so stdout stays a clean JSON protocol.
  const originalWrite = process.stdout.write.bind(process.stdout);
  process.stdout.write = () => true;
  let results;
  try {
    const factory = new Function('require', 'module', 'exports', code + '\n;return filterPaths;');
    const moduleObject = { exports: {} };
    const filterPaths = factory(require, moduleObject, moduleObject.exports);
    if (typeof filterPaths !== 'function') {
      throw new Error('code did not define a filterPaths function');
    }
    results = filterPaths(paths);
  } catch (e) {
    process.stdout.write = originalWrite;
    return { ok: false, error: (e && e.message) ? e.message : String(e) };
  }
  process.stdout.write = originalWrite;

  if (!Array.isArray(results)) {
    return { ok: false, error: 'filterPaths must return an array' };
  }
  // The host re-validates that every result is one of the supplied paths.
  return { ok: true, results: results };
}

const response = run();
process.stdout.write(JSON.stringify(response));
