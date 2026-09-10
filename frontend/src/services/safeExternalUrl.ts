const sensitiveQueryKeyParts = [
  "accesstoken",
  "apikey",
  "auth",
  "authorization",
  "bearer",
  "credential",
  "password",
  "passwd",
  "refreshtoken",
  "secret",
  "sessionid",
  "sig",
  "signature",
  "token"
];

function isPrivateIpv4(hostname: string) {
  const octets = hostname.split(".").map(Number);
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
    return false;
  }
  return (
    octets[0] === 0 ||
    octets[0] === 10 ||
    octets[0] === 127 ||
    (octets[0] === 169 && octets[1] === 254) ||
    (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
    (octets[0] === 192 && octets[1] === 168)
  );
}

export function safeExternalHttpUrl(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const raw = value.trim();
  if (!raw || raw.length > 2048) {
    return undefined;
  }
  try {
    const parsed = new URL(raw);
    const hostname = parsed.hostname
      .toLowerCase()
      .replace(/^\[|\]$/g, "")
      .replace(/\.$/, "");
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      !hostname ||
      parsed.username ||
      parsed.password ||
      hostname === "localhost" ||
      hostname.endsWith(".localhost") ||
      hostname.endsWith(".local") ||
      isPrivateIpv4(hostname) ||
      hostname === "::1" ||
      (hostname.includes(":") &&
        (hostname.startsWith("fc") || hostname.startsWith("fd") || hostname.startsWith("fe80:")))
    ) {
      return undefined;
    }
    for (const key of parsed.searchParams.keys()) {
      const normalizedKey = key.toLowerCase().replace(/[^a-z0-9]/g, "");
      if (sensitiveQueryKeyParts.some((part) => normalizedKey.includes(part))) {
        return undefined;
      }
    }
    return raw;
  } catch {
    return undefined;
  }
}
