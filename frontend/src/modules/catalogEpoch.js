// A new request supersedes older work, including responses from an earlier grant.
export function catalogEpoch() {
  let epoch = 0;
  let closed = false;
  return {
    next() { const request = ++epoch; return () => !closed && request === epoch; },
    close() { closed = true; epoch += 1; },
  };
}
