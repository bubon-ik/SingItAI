/**
 * The command line this service is driven by.
 *
 * Every purchase arrives here first, so this is the narrowest place where a
 * caller's intent can be turned into a different one without anybody noticing:
 * a payout address, an atomic amount and a spending cap all come through as
 * strings on an argv.
 *
 * It lives in its own module because `index.mjs` calls `main()` on import, so
 * anything defined there cannot be tested without running a command — which is
 * why this function went untested long enough to grow the bug below.
 */

/**
 * Parse `--key value` and `--key=value`, returning a plain object of strings.
 *
 * Both spellings are ordinary and callers reach for either. Until this was
 * fixed only the space-separated form worked: `--url=https://…` put the whole
 * `url=https://…` string into the key, so `requiredOption` reported `--url is
 * required` about a command line that plainly contained it. The error pointed
 * at the caller while the fault was here.
 *
 * A flag with no value is `"true"`. An explicitly empty value stays `""`, so
 * `requiredOption` still rejects it rather than receiving a truthy string.
 */
export function parseArgs(args) {
  const options = {};
  for (let index = 0; index < args.length; index += 1) {
    const current = args[index];
    if (!current.startsWith("--")) continue;

    // Split on the first `=` only: JSON bodies and base64 both contain `=`,
    // and splitting anywhere else would corrupt the request being paid for.
    const equals = current.indexOf("=");
    if (equals > 2) {
      // `> 2` rather than `> -1`: `--=x` has no key at all, and inventing an
      // empty one would be worse than leaving the malformed argument alone.
      options[current.slice(2, equals)] = current.slice(equals + 1);
      continue;
    }

    const key = current.slice(2);
    const value = args[index + 1];
    if (!value || value.startsWith("--")) {
      options[key] = "true";
      continue;
    }
    options[key] = value;
    index += 1;
  }
  return options;
}
