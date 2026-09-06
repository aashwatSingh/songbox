import path from "node:path";
import type { NextConfig } from "next";

// SONGBOX_DEV_ORIGIN: the hostname (no protocol, no port) this dev server is reached on besides
// localhost -- e.g. a Tailscale IP or MagicDNS name. Not NEXT_PUBLIC_* on purpose: this is read
// once by the Next.js dev server process itself at startup, never shipped to the client.
//
// Needed because Next's dev server refuses cross-origin requests for its own internal resources
// by default (DNS-rebinding protection) -- confirmed by reading
// node_modules/next/dist/server/lib/router-utils/block-cross-site-dev.js, since this Next version
// has real breaking changes and AGENTS.md says not to trust training data here. Without a matching
// host in allowedDevOrigins, a subset of the page's own /_next/static chunks 403, and the app
// hangs on "Loading..." forever with nothing more specific in the UI -- confirmed by loading this
// app from its own Tailscale IP and finding exactly that.
const devOrigin = process.env.SONGBOX_DEV_ORIGIN;

const nextConfig: NextConfig = {
  turbopack: {
    root: path.join(__dirname),
  },
  allowedDevOrigins: devOrigin ? [devOrigin] : undefined,
};

export default nextConfig;
